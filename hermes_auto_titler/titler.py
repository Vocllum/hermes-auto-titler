"""核心：标题评估、模型生成、provenance 正确的写回。

来源规则（对齐 Hermes SessionDB）：
- 用户手改的标题（source=user）→ 写前反复复核并优先保护
- 无标题 → set_auto_title(source=llm)
- 自动标题（llm/derived）→ 权威写入后恢复 llm 来源，保持可升级

评估纪律：
- Hermes 内部执行（bg-review 后台回顾线程、platform=cron|subagent，大小写
  不敏感）不计轮数、不评估
- 被打断/失败/未完成（completed=False / failed / interrupted，即使标志
  不一致）的轮次不是完整前台轮次，不计轮数
- 真实关闭由 on_session_finalize（或带非空 reason 的 on_session_end）表达，
  走正常节流（force=False）且不与进行中的自动评估重复；关闭 hook 刻意禁网络调用，
  只对已有 worker 等待最多 100ms，随后将 typed finalize intent 原子持久化，由后续正常生命周期续跑
- 轮次评估在 daemon worker 中执行（携带当前 profile Context），hook 立即
  返回；同一会话 in-flight 去重
- early_turn_eval 的早期评估只对无标题或 derived 来源的会话触发
  （llm/user/legacy 不提前调用模型；正常 every_n_turns 边界不受影响）
"""

from __future__ import annotations

import collections
import contextvars
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_state import SessionDB

try:
    # 宿主提供 profile home 解析；极老宿主没有时退回 HERMES_HOME 环境变量
    from hermes_state import get_hermes_home
except ImportError:  # pragma: no cover
    def get_hermes_home():  # type: ignore[misc]
        import os
        from pathlib import Path
        return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))

from .messages import display_width, load_context_with_summary, truncate_to_width
from .state import StateStore

log = logging.getLogger(__name__)

# 按 hermes home 路径缓存 SessionDB：同一 profile 复用句柄（含双重检查锁），
# 不同 profile 各建各的，避免模块级单例把首个 profile 的 DB 泄露给后续 profile。
_dbs: Dict[str, SessionDB] = {}
_db_lock = threading.Lock()

# Hermes 内部平台：cron（定时任务）与 subagent（子代理）的轮次不参与标题评估
_INTERNAL_PLATFORMS = frozenset({"cron", "subagent"})


def get_db() -> SessionDB:
    key = str(get_hermes_home())
    db = _dbs.get(key)
    if db is not None:
        return db
    with _db_lock:
        db = _dbs.get(key)
        if db is None:
            db = SessionDB()
            _dbs[key] = db
    return db


def _wrap_with_context(target):
    """把 hook 所在线程的 Hermes Context（profile/gateway session 等 contextvars，
    以及 approval/sudo 回调）传播进 daemon worker 线程。

    优先使用宿主提供的 tools.thread_context.propagate_context_to_thread；老宿主
    /测试环境没有该模块时退回标准库 contextvars.copy_context()（同样在父线程
    快照当前 Context）。在父线程调用，把返回的可调用对象作为线程 target。
    """
    try:
        from tools.thread_context import propagate_context_to_thread
    except Exception:
        ctx = contextvars.copy_context()

        def _fallback(*args: Any, **kwargs: Any) -> Any:
            return ctx.run(target, *args, **kwargs)

        return _fallback
    return propagate_context_to_thread(target)


def _safe_audit_action(text: str) -> str:
    """Return only the parsed action for logs; never log the raw model body."""
    action, _ = _parse_decision(text)
    return action


def _safe_audit_title(text: str) -> Optional[str]:
    """Return a bounded title field for logs, with no prompt or raw response."""
    _, title = _parse_decision(text)
    return title[:80] if title else None


def _audit_input_shape(
    current: Optional[str],
    recent: List[Tuple[str, str]],
    all_user: List[Tuple[str, str]],
    opening: List[Tuple[str, str]],
    earlier_summary: Optional[str],
) -> str:
    """Log shape only, keeping conversation text out of the log file."""
    return (
        f"current={'yes' if current else 'no'} "
        f"opening={len(opening)} recent={len(recent)} users={len(all_user)} "
        f"summary={'yes' if earlier_summary else 'no'} "
        f"chars={sum(len(t) for _, t in opening + recent + all_user) + len(earlier_summary or '')}"
    )


_CAPACITY_MARKERS = (
    "429",
    "503",
    "overloaded",
    "quota",
    "rate limit",
    "ratelimit",
    "no available targets",
    "resource_exhausted",
    "too many requests",
)


def _is_capacity_error(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _CAPACITY_MARKERS)


class AutoTitler:
    def __init__(self, ctx, cfg: dict[str, Any], db: SessionDB | None = None):
        self.ctx = ctx
        self.cfg = cfg
        self._db = db
        self._turns: Dict[str, int] = {}
        self._last_eval: Dict[str, float] = {}
        self._current_session: Optional[str] = None
        # 滞后机制：session_id -> {"title": 待审候选}（评审协议，见 evaluate）
        self._pending: Dict[str, Dict[str, Any]] = {}
        # Ordinary model failures and close/finalize intent have separate budgets.
        # A stale worker may clear its ordinary failure, but never a newer close intent.
        self._failed_sessions: Dict[str, Dict[str, Any]] = {}
        self._finalize_intents: Dict[str, Dict[str, Any]] = {}
        # State mutations that participate in persistence are serialized by the
        # single barrier below.  Keep the legacy attribute names as aliases so
        # integrations/tests that introspect them do not acquire a second lock
        # domain accidentally.
        self._retry_lock: threading.RLock
        # 可选的自动改名次数门控；0 表示关闭，不改变默认行为。
        self._rename_counts: Dict[str, int] = {}
        self._rename_count_lock: threading.RLock
        # 结构化审计环形日志：固定容量（最多 50 条），供 /autotitler status 或排障审查
        self._audit_log: collections.deque = collections.deque(maxlen=50)
        # 同一会话同一时刻只允许一个进行中的模型评估（hook 并发去重），记录关联的完成 Event
        self._inflight: Dict[str, threading.Event] = {}
        self._dirty_sessions: set[str] = set()
        self._pre_llm_snapshots: Dict[str, Any] = {}
        self._eager_pre_sessions: set[str] = set()
        self._dirty_override_intents: set[str] = set()
        self._active_override_sessions: set[str] = set()
        self._last_generate_errors: Dict[str, str] = {}
        self._last_generate_error: str = ""
        self._inflight_lock = threading.Lock()
        # One barrier covers every durable-state mutation and snapshot.  File
        # replacement alone is atomic but cannot make a mixed in-memory snapshot
        # consistent while pending/retry/counter fields are changing.
        self._state_lock = threading.RLock()
        self._retry_lock = self._state_lock
        self._rename_count_lock = self._state_lock
        self._closing_epochs: Dict[str, int] = {}
        self._completed_epochs: Dict[str, int] = {}
        self._worker_epochs: Dict[str, int] = {}
        self._closing_fenced: set[str] = set()
        self._unresolved_disk_records: Dict[str, Dict[str, Any]] = {}
        self._state_path = Path(get_hermes_home()) / "plugins" / "hermes-auto-titler" / "state.json"

    @property
    def db(self) -> SessionDB:
        return self._db or get_db()

    # -- hook 入口 ----------------------------------------------------------

    @staticmethod
    def _is_internal_payload(**payload: Any) -> bool:
        """Hermes 内部执行（bg-review 后台回顾线程 / cron / subagent 平台）
        不参与标题评估。platform 大小写不敏感（cron/scheduler 构造
        platform='cron'，subagent 平台同理）。"""
        if threading.current_thread().name.startswith("bg-review"):
            return True
        platform = str(payload.get("platform") or "").strip().lower()
        return platform in _INTERNAL_PLATFORMS

    def on_pre_llm_call(self, **payload: Any) -> None:
        """首回合开局触发：第一回合用户刚发消息时立即异步生成第一版标题，不等整个回合结束。"""
        if not self.cfg.get("enabled", True):
            return
        if self._is_internal_payload(**payload):
            return
        session_id = payload.get("session_id") or ""
        if not session_id:
            return
        self._current_session = session_id

        # 仅对首轮接入且当前无标题或 derived 临时截断标题的会话触发
        if self._early_enabled() and self._early_eligible(session_id):
            # 仅在真实首轮开局时武装覆写标记，避免后续回合重新武装
            if payload.get("is_first_turn") is not False and self._turns.get(session_id, 0) == 0:
                with self._state_lock:
                    self._eager_pre_sessions.add(session_id)
            # 捕获首轮快照，解决 Hermes 持久化落盘慢于 worker 启动的真实 DB 竞态
            u_msg = payload.get("user_message") or ""
            if u_msg:
                self._pre_llm_snapshots[session_id] = u_msg
            log.info("auto-titler pre_llm_call: eager evaluation for new session %s", session_id[:12])
            self._submit_eval(session_id)

    def on_session_end(self, **payload: Any) -> None:
        if not self.cfg.get("enabled", True):
            return
        if self._is_internal_payload(**payload):
            return
        session_id = payload.get("session_id") or ""
        if not session_id:
            return
        self._current_session = session_id
        # 非空 reason = 真实关闭信号（CLI 退出 safety net），对可见平台最先处理；
        # 打断本身不算关闭
        if payload.get("reason"):
            self._close_eval(session_id)
            return
        # 裸打断/失败/未完成轮次不是完整前台轮次：不计轮数、不评估。
        # failed/interrupted/completed 任一标志指示非完成即不计——即使标志互相
        # 不一致（如 completed=True 但 failed=True）也不会计数。
        if payload.get("failed") or payload.get("interrupted") or not payload.get("completed"):
            return
        n = self._turns.get(session_id, 0) + 1
        self._turns[session_id] = n
        every = max(1, int(self.cfg.get("every_n_turns", 2)))
        with self._state_lock:
            first_turn_override = (session_id in self._eager_pre_sessions) and (n == 1)
            if first_turn_override or n > 1:
                self._eager_pre_sessions.discard(session_id)
        if first_turn_override:
            log.info("auto-titler on_session_end: overriding eager pre-title for turn 1 of %s", session_id[:12])
            self._submit_eval(session_id, override_intent=True)
        elif n % every == 0:
            self._submit_eval(session_id)
        elif (
            self._early_enabled()
            and n < every
            and self._early_eligible(session_id)
        ):
            self._submit_eval(session_id)
        self._retry_failed_sessions()

    def on_session_finalize(self, **payload: Any) -> None:
        """真实会话关闭/终局（CLI 退出、TUI 关闭、gateway 过期、/new 切换）。"""
        if not self.cfg.get("enabled", True):
            return
        if self._is_internal_payload(**payload):
            return
        session_id = payload.get("session_id") or ""
        if not session_id:
            return
        self._current_session = session_id
        try:
            self._close_eval(session_id)
        except Exception:
            log.warning(
                "auto-titler finalize failed; failing open for session %s",
                session_id[:12],
                exc_info=True,
            )

    # -- 评估调度 -----------------------------------------------------------

    def _retry_failed_sessions(self) -> None:
        """Claim typed ordinary-retry and finalize intents with global backpressure."""
        if not self.cfg.get("enabled", True):
            return
        now = time.monotonic()
        candidates: List[str] = []
        expired: List[str] = []

        with self._state_lock:
            session_ids = set(self._failed_sessions) | set(self._finalize_intents)
            if not session_ids:
                return
            for sid in session_ids:
                finalize = self._finalize_intents.get(sid)
                retry = self._failed_sessions.get(sid)
                meta = finalize or retry or {}
                attempts = int(meta.get("attempts", 0))
                capacity = bool(meta.get("capacity"))
                if finalize is None and attempts >= 5 and not capacity:
                    expired.append(sid)
                elif now >= float(meta.get("next_retry_at", 0)):
                    candidates.append(sid)
            for sid in expired:
                self._failed_sessions.pop(sid, None)

        if expired:
            self._persist_state()
        if not candidates:
            return

        to_retry: List[Tuple[str, Optional[int]]] = []
        to_remove: List[str] = []
        deferred: List[str] = []
        for sid in candidates:
            with self._inflight_lock:
                if sid in self._inflight:
                    continue
            finalize_claim: Optional[int] = None
            with self._state_lock:
                fin = self._finalize_intents.get(sid)
                if fin:
                    finalize_claim = int(fin.get("close_epoch", 0))
            try:
                src = self.db.get_session_title_source(sid)
                if finalize_claim is None:
                    if src == SessionDB.TITLE_SOURCE_USER:
                        to_remove.append(sid)
                        continue
                    cur_title = self.db.get_session_title(sid)
                    if (
                        cur_title
                        and src == SessionDB.TITLE_SOURCE_LLM
                        and not self._pending.get(sid)
                    ):
                        to_remove.append(sid)
                        continue
                else:
                    cur_title = self.db.get_session_title(sid)
            except Exception:
                # DB unknown is not proof of staleness; leave the intent queued.
                continue
            # An intent whose anchor was unknown at close time cannot be settled
            # by an epoch claim alone: the terminal snapshot was never observed,
            # so any claimant could "cover" it tautologically.  Now that the DB
            # is readable, re-validate the anchor and defer consumption to a
            # later sweep.  That keeps "re-validate the base, then consume" a
            # two-phase operation instead of one self-certifying pass.
            with self._state_lock:
                fin = self._finalize_intents.get(sid)
                if fin is not None and not fin.get("base_title_known", True):
                    fin["base_title_known"] = True
                    fin["base_title"] = cur_title
                    fin["expected_title"] = cur_title
                    self._persist_state_locked()
                    deferred.append(sid)
                    continue
            to_retry.append((sid, finalize_claim))
            if len(to_retry) >= 2:
                break

        if to_remove:
            with self._state_lock:
                for sid in to_remove:
                    self._failed_sessions.pop(sid, None)
            self._persist_state()

        for sid, finalize_claim in to_retry:
            log.info("auto-titler retry: triggering compensation eval for %s", sid[:12])
            try:
                self._submit_eval(sid, finalize_claim=finalize_claim)
            except TypeError:
                self._submit_eval(sid)

    def _requeue_untitled_sessions(self) -> None:
        """Recover untitled/derived sessions after restart so a quota outage can drain later."""
        try:
            rows = self.db.list_sessions_rich(
                limit=200, include_children=False, order_by_last_active=True
            )
        except TypeError:
            try:
                rows = self.db.list_sessions_rich(limit=200, include_children=False)
            except Exception:
                return
        except Exception:
            return
        now = time.monotonic()
        queued = 0
        with self._retry_lock:
            for row in rows or []:
                sid = str((row or {}).get("id") or "")
                if not sid or sid in self._failed_sessions:
                    continue
                try:
                    src = self.db.get_session_title_source(sid)
                    title = self.db.get_session_title(sid)
                except Exception:
                    continue
                if src == SessionDB.TITLE_SOURCE_USER:
                    continue
                if src is None and title:
                    continue
                if title and src == SessionDB.TITLE_SOURCE_LLM:
                    continue
                if title and src != SessionDB.TITLE_SOURCE_DERIVED:
                    continue
                self._failed_sessions[sid] = {
                    "attempts": 0,
                    "next_retry_at": now,
                    "capacity": False,
                }
                queued += 1
        if queued:
            log.info("auto-titler retry: requeued %d untitled/derived session(s)", queued)

    def start_retry_loop(self) -> None:
        """Independent sweep so quota recovery does not wait for the next user turn."""
        existing = getattr(self, "_retry_thread", None)
        if existing is not None and existing.is_alive():
            return
        self._retry_stop = threading.Event()

        def run() -> None:
            try:
                self._requeue_untitled_sessions()
            except Exception:
                log.warning("auto-titler untitled requeue failed", exc_info=True)
            while not self._retry_stop.wait(30.0):
                try:
                    self._retry_failed_sessions()
                except Exception:
                    log.warning("auto-titler retry sweep failed", exc_info=True)

        self._retry_thread = threading.Thread(
            target=_wrap_with_context(run),
            name="autotitler-retry",
            daemon=True,
        )
        self._retry_thread.start()

    def _early_enabled(self) -> bool:
        """首轮命名开关：plugin=插件第 1 轮即评估；builtin（默认）=首轮让给内建。"""
        return str(self.cfg.get("first_title_mode", "builtin")).lower() == "plugin"

    def _early_eligible(self, session_id: str) -> bool:
        """early_turn_eval 的来源门：只对无标题或 derived 来源的会话提前评估。

        source=llm/user 与 legacy（NULL 来源 + 已有标题）在早期不额外提交
        （不产生模型调用）；正常 every_n_turns 边界不受此门影响。来源发现
        失败时保守跳过（fail safe）。
        """
        try:
            src = self.db.get_session_title_source(session_id)
        except Exception:
            return False
        if src == SessionDB.TITLE_SOURCE_USER:
            return False
        if src == SessionDB.TITLE_SOURCE_DERIVED:
            return True
        if src is None:
            try:
                current = self.db.get_session_title(session_id)
            except Exception:
                return False
            return not current  # 无标题 → early 可评估；legacy 已有标题 → 保护
        return False  # llm 等其余来源不提前评估

    def _submit_eval(
        self,
        session_id: str,
        *,
        finalize_claim: Optional[int] = None,
        override_intent: bool = False,
    ) -> None:
        """Submit one coalescing worker.

        Ordinary submissions are rejected once a closing fence is established for
        the session.  Only an explicit claimant providing a recorded close_epoch
        from an existing finalize intent may evaluate a closed session.
        """
        with self._state_lock:
            if session_id in self._closing_fenced and finalize_claim is None:
                # The fence expresses an unresolved terminal intent, never
                # "this session is closed forever".  If no intent remains the
                # fence is stale by definition, so release it and continue.
                if session_id not in self._finalize_intents:
                    self._closing_fenced.discard(session_id)
                else:
                    return
            if finalize_claim is not None:
                worker_epoch = int(finalize_claim)
            else:
                worker_epoch = 0

        event = threading.Event()
        with self._inflight_lock:
            if session_id in self._inflight:
                self._dirty_sessions.add(session_id)
                if override_intent:
                    self._dirty_override_intents.add(session_id)
                return
            self._worker_epochs[session_id] = worker_epoch
            self._inflight[session_id] = event
        try:
            worker = threading.Thread(
                target=_wrap_with_context(self._eval_worker),
                args=(session_id, event, worker_epoch, override_intent),
                name=f"autotitler-eval-{session_id[:8]}",
                daemon=True,
            )
            worker.start()
        except Exception as e:
            log.warning(
                "auto-titler failed to start eval worker for %s: %s",
                session_id[:12], e,
            )
            with self._inflight_lock:
                self._dirty_sessions.discard(session_id)
                self._dirty_override_intents.discard(session_id)
                self._inflight.pop(session_id, None)
                self._worker_epochs.pop(session_id, None)
            event.set()

    def _eval_worker(
        self,
        session_id: str,
        event: threading.Event,
        worker_epoch: int = 0,
        initial_override_intent: bool = False,
    ) -> None:
        try:
            force_eval = False
            current_override = initial_override_intent
            while True:
                try:
                    with self._state_lock:
                        finalize_meta = dict(self._finalize_intents.get(session_id, {}))
                        covers_finalize = bool(
                            finalize_meta
                            and int(finalize_meta.get("close_epoch", 0)) <= worker_epoch
                            and finalize_meta.get("base_title_known", True)
                        )
                        if current_override:
                            self._active_override_sessions.add(session_id)
                        else:
                            self._active_override_sessions.discard(session_id)
                    # A recovered finalize intent represents a missed terminal
                    # snapshot. It must not be throttled by the last periodic eval.
                    # Eager pre-titles must also not throttle the first full turn's evaluation.
                    try:
                        try:
                            result = self.evaluate(
                                session_id,
                                force=force_eval or covers_finalize or current_override,
                                claim_epoch=worker_epoch,
                            )
                        except TypeError:
                            result = self.evaluate(
                                session_id,
                                force=force_eval or covers_finalize or current_override,
                            )
                    finally:
                        with self._state_lock:
                            self._active_override_sessions.discard(session_id)
                    action = str((result or {}).get("action") or "")
                    with self._state_lock:
                        if action not in {"failed", "pending", "throttled"}:
                            self._completed_epochs[session_id] = max(
                                self._completed_epochs.get(session_id, 0), worker_epoch
                            )
                            meta = self._finalize_intents.get(session_id)
                            if (
                                meta
                                and int(meta.get("close_epoch", 0)) <= worker_epoch
                                # An unknown anchor means the terminal snapshot
                                # was never observed.  Claiming the epoch alone
                                # would make the coverage proof tautological, so
                                # the intent survives until the anchor is
                                # validated and re-claimed.
                                and meta.get("base_title_known", True)
                            ):
                                self._clear_finalize_intent_locked(session_id, close_epoch=worker_epoch)
                        elif covers_finalize:
                            meta = self._finalize_intents.get(session_id)
                            if meta:
                                attempts = int(meta.get("attempts", 0)) + 1
                                delay = min(30 * (2 ** (attempts - 1)), 600)
                                meta["attempts"] = attempts
                                meta["next_retry_at"] = time.monotonic() + delay
                        self._persist_state_locked()
                except Exception as e:
                    log.warning("auto-titler background evaluate failed: %s", e)
                    with self._state_lock:
                        meta = self._finalize_intents.get(session_id)
                        if meta and int(meta.get("close_epoch", 0)) <= worker_epoch:
                            attempts = int(meta.get("attempts", 0)) + 1
                            delay = min(30 * (2 ** (attempts - 1)), 600)
                            meta["attempts"] = attempts
                            meta["next_retry_at"] = time.monotonic() + delay
                            self._persist_state_locked()

                with self._inflight_lock:
                    if session_id in self._dirty_sessions:
                        self._dirty_sessions.remove(session_id)
                        force_eval = True
                        # Dirty rerun triggers re-evaluation but never promotes epoch/finalize claim
                        if session_id in self._dirty_override_intents:
                            self._dirty_override_intents.discard(session_id)
                            current_override = True
                        else:
                            current_override = False
                        continue
                    self._dirty_override_intents.discard(session_id)
                    self._inflight.pop(session_id, None)
                    self._worker_epochs.pop(session_id, None)
                    break
        finally:
            with self._inflight_lock:
                self._dirty_sessions.discard(session_id)
                self._dirty_override_intents.discard(session_id)
                self._inflight.pop(session_id, None)
                self._worker_epochs.pop(session_id, None)
            with self._state_lock:
                self._active_override_sessions.discard(session_id)
            event.set()

    def _close_eval(self, session_id: str) -> None:
        """Persist finalization without networking; network wait is capped at 100ms.

        Atomically fences the session against ordinary submissions, persists the
        typed finalize intent, and only then inspects any running worker.  A
        running worker clears the finalize intent only when its tagged epoch
        matches or exceeds this close epoch.
        """
        if not self.cfg.get("on_close", True):
            return

        with self._state_lock:
            self._closing_fenced.add(session_id)
            existing_intent = self._finalize_intents.get(session_id)
            if existing_intent:
                close_epoch = int(existing_intent.get("close_epoch", 0))
            else:
                close_epoch = self._closing_epochs.get(session_id, 0) + 1
                self._closing_epochs[session_id] = close_epoch

            # Atomically queue the intent under the fence before looking at in-flight state.
            self._queue_session_locked(session_id, reason="finalize", close_epoch=close_epoch)

        with self._inflight_lock:
            existing_event = self._inflight.get(session_id)
            worker_epoch = self._worker_epochs.get(session_id, 0)

        completed = False
        if existing_event is not None:
            log.info(
                "auto-titler close: waiting at most 100ms for in-flight worker for %s",
                session_id[:12],
            )
            completed = existing_event.wait(0.1)

        with self._state_lock:
            covered = (
                completed
                and worker_epoch >= close_epoch
                and self._completed_epochs.get(session_id, 0) >= close_epoch
            )
            if not covered and existing_event is not None and not completed:
                # Update intent reason to reflect timeout without altering close_epoch or budget.
                intent = self._finalize_intents.get(session_id)
                if intent:
                    intent["reason"] = "finalize budget exceeded"
                    self._persist_state_locked()

    def _queue_session(
        self,
        session_id: str,
        *,
        reason: str,
        close_epoch: Optional[int] = None,
    ) -> None:
        with self._state_lock:
            self._queue_session_locked(session_id, reason=reason, close_epoch=close_epoch)

    def _clear_finalize_intent_locked(
        self, session_id: str, *, close_epoch: Optional[int] = None
    ) -> bool:
        """Drop a finalize intent together with the closing fence it created.

        The fence only means "an unresolved terminal intent exists".  Once the
        intent is settled — either covered by a worker that provably observed
        the final epoch, or provably unsatisfiable (user authority, legacy
        protection, rename cap, empty session) — the session must regain
        ordinary foreground titling instead of being barred forever.
        """
        meta = self._finalize_intents.get(session_id)
        if meta is not None:
            if close_epoch is None or int(meta.get("close_epoch", 0)) > close_epoch:
                return False
        self._finalize_intents.pop(session_id, None)
        if session_id not in self._finalize_intents:
            self._closing_fenced.discard(session_id)
        return True

    def _queue_session_locked(
        self,
        session_id: str,
        *,
        reason: str,
        close_epoch: Optional[int] = None,
    ) -> None:
        """Upsert a typed finalize intent under ``_state_lock``."""
        base_title_known = True
        try:
            base_title = self.db.get_session_title(session_id)
        except Exception:
            base_title = None
            base_title_known = False
        now_wall = time.time()
        pending = dict(self._pending.get(session_id) or {})
        previous = dict(self._failed_sessions.get(session_id, {}))
        existing = dict(self._finalize_intents.get(session_id, {}))
        epoch = int(close_epoch or self._closing_epochs.get(session_id, 0))
        self._closing_epochs[session_id] = max(
            self._closing_epochs.get(session_id, 0), epoch
        )
        existing_epoch = int(existing.get("close_epoch", -1))
        if existing and existing_epoch >= epoch:
            # on_session_end(reason=...) and on_session_finalize may report the
            # same close.  Preserve the original typed intent and its budget.
            return
        # Finalization is not the sixth ordinary retry.  It has a separate
        # typed ledger so a stale ordinary worker cannot erase it.
        self._finalize_intents[session_id] = {
            "attempts": 0,
            "next_retry_at": time.monotonic(),
            "capacity": False,
            "reason": reason,
            "queued_at": now_wall,
            "base_title": pending.get("base_title", base_title),
            "base_title_known": True if pending.get("base_title") is not None else base_title_known,
            "candidate": pending.get("title"),
            "confirmations": int(pending.get("confirmations", 0)),
            "expected_title": base_title,
            "close_epoch": epoch,
            "previous_retry_attempts": int(previous.get("attempts", 0)),
        }
        self._persist_state_locked()

    @staticmethod
    def _state_kind(meta: Dict[str, Any], pending: Dict[str, Any], count: int) -> str:
        reason = str(meta.get("reason") or "")
        parts = []
        if reason in {"finalize", "finalize budget exceeded"}:
            parts.append("finalize")
        elif meta:
            parts.append("retry")
        if pending:
            parts.append("pending")
        if count:
            parts.append("counter")
        return "+".join(parts) or "counter"

    def _persistent_sessions(self) -> Dict[str, Dict[str, Any]]:
        """Build a typed serialized view under ``_state_lock``."""
        now_mono = time.monotonic()
        now_wall = time.time()
        failed = {sid: dict(meta) for sid, meta in self._failed_sessions.items()}
        finalizes = {sid: dict(meta) for sid, meta in self._finalize_intents.items()}
        sessions: Dict[str, Dict[str, Any]] = {
            sid: dict(rec) for sid, rec in self._unresolved_disk_records.items()
        }
        session_ids = (
            set(failed)
            | set(finalizes)
            | set(self._pending)
            | set(self._rename_counts)
        )
        for sid in session_ids:
            retry = failed.get(sid, {})
            finalize = finalizes.get(sid, {})
            anchor = finalize or retry
            pending = dict(self._pending.get(sid, {}))
            count = int(self._rename_counts.get(sid, 0))
            base_known = anchor.get("base_title_known")
            try:
                current_title = self.db.get_session_title(sid)
                if base_known is None and not anchor.get("base_title") and not pending.get("base_title"):
                    base_known = True
            except Exception:
                current_title = anchor.get("base_title") or pending.get("base_title")
                if base_known is None:
                    base_known = False
            base_title = pending.get("base_title", anchor.get("base_title") or current_title)
            if base_known is None:
                base_known = True if base_title is not None else False
            parts = []
            if finalize:
                parts.append("finalize")
            if retry:
                parts.append("retry")
            if pending:
                parts.append("pending")
            if count:
                parts.append("counter")
            record: Dict[str, Any] = {
                "kind": "+".join(parts) or "counter",
                "base_title": base_title,
                "base_title_known": bool(base_known),
                "expected_title": anchor.get("expected_title", current_title),
                "rename_count": count,
            }
            if pending:
                record["pending_review"] = {
                    "candidate": pending.get("title"),
                    "confirmations": int(pending.get("confirmations", 0)),
                }

            def serialize_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
                next_retry_at = float(meta.get("next_retry_at", now_mono))
                return {
                    "attempts": int(meta.get("attempts", 0)),
                    "next_retry_at": now_wall + max(0.0, next_retry_at - now_mono),
                    "capacity": bool(meta.get("capacity", False)),
                    "queued_at": float(meta.get("queued_at", now_wall)),
                    "reason": str(meta.get("reason") or "error"),
                }

            if retry:
                record["retry"] = serialize_meta(retry)
            if finalize:
                record["finalize_intent"] = True
                serialized = serialize_meta(finalize)
                serialized["close_epoch"] = int(finalize.get("close_epoch", 0))
                record["finalize"] = serialized
            sessions[sid] = record
        return sessions

    def _persist_state_locked(self) -> None:
        try:
            StateStore(self._state_path).save(self._persistent_sessions())
        except Exception:
            log.warning("auto-titler state persistence failed", exc_info=True)

    def _persist_state(self) -> None:
        try:
            with self._state_lock:
                self._persist_state_locked()
        except Exception:
            log.warning("auto-titler state persistence failed", exc_info=True)

    def restore_state(self) -> None:
        """Restore valid pending/retry state; stale title snapshots are discarded."""
        if not self._state_path.is_file():
            return
        try:
            with self._state_lock:
                sessions = StateStore(self._state_path).load()["sessions"]
        except Exception:
            log.warning("auto-titler state load failed; starting with empty state", exc_info=True)
            return

        now_mono = time.monotonic()
        now_wall = time.time()
        restored = 0
        unresolved = False
        with self._state_lock:
            self._unresolved_disk_records.clear()
        for sid, raw in sessions.items():
            if not isinstance(sid, str) or not isinstance(raw, dict):
                continue
            try:
                current = self.db.get_session_title(sid)
                source = self.db.get_session_title_source(sid)
            except Exception:
                # DB unknown is not stale.  Preserve the on-disk record verbatim so
                # another normal startup can validate it against SessionDB.
                unresolved = True
                with self._state_lock:
                    self._unresolved_disk_records[sid] = dict(raw)
                continue

            try:
                base_title = raw.get("base_title")
                base_title_known = raw.get("base_title_known", True)
                if source == SessionDB.TITLE_SOURCE_USER:
                    continue
                if base_title_known and current != base_title:
                    continue

                # v1 legacy records are accepted once and rewritten as typed state.
                pending_raw = raw.get("pending_review")
                if pending_raw is None and raw.get("candidate"):
                    pending_raw = {
                        "candidate": raw.get("candidate"),
                        "confirmations": raw.get("confirmations", 0),
                    }
                if pending_raw is not None:
                    if not isinstance(pending_raw, dict) or not pending_raw.get("candidate"):
                        raise ValueError("invalid pending_review")
                    candidate = str(pending_raw["candidate"])
                    confirmations = max(0, int(pending_raw.get("confirmations", 0)))
                    with self._state_lock:
                        self._pending[sid] = {
                            "title": candidate,
                            "base_title": base_title,
                            "confirmations": confirmations,
                        }
                else:
                    candidate = None
                    confirmations = 0

                rename_count = max(0, int(raw.get("rename_count", 0)))
                if rename_count:
                    with self._state_lock:
                        self._rename_counts[sid] = rename_count

                finalize_raw = raw.get("finalize")
                retry_raw = raw.get("retry")
                if finalize_raw is None and retry_raw is None and (
                    "attempts" in raw or "reason" in raw
                ):
                    legacy_meta = {
                        "attempts": raw.get("attempts", 0),
                        "next_retry_at": raw.get("next_retry_at", now_wall),
                        "capacity": raw.get("capacity", False),
                        "queued_at": raw.get("queued_at", now_wall),
                        "reason": raw.get("reason", "error"),
                    }
                    if str(legacy_meta["reason"]) in {
                        "finalize",
                        "finalize budget exceeded",
                    }:
                        finalize_raw = legacy_meta
                    else:
                        retry_raw = legacy_meta

                def restore_meta(typed: Any, *, finalize: bool) -> None:
                    if typed is None:
                        return
                    if not isinstance(typed, dict):
                        raise ValueError("retry/finalize metadata must be an object")
                    attempts = max(0, int(typed.get("attempts", 0)))
                    retry_wall = float(typed.get("next_retry_at", now_wall))
                    queued_at = float(typed.get("queued_at", now_wall))
                    reason = str(
                        typed.get("reason") or ("finalize" if finalize else "error")
                    )
                    close_epoch = max(0, int(typed.get("close_epoch", 0)))
                    meta = {
                        "attempts": attempts,
                        "next_retry_at": now_mono + max(0.0, retry_wall - now_wall),
                        "capacity": bool(typed.get("capacity", False)),
                        "reason": reason,
                        "queued_at": queued_at,
                        "base_title": base_title,
                        "base_title_known": bool(base_title_known),
                        "candidate": candidate,
                        "confirmations": confirmations,
                        "expected_title": raw.get("expected_title"),
                        "close_epoch": close_epoch,
                    }
                    if finalize:
                        self._finalize_intents[sid] = meta
                        self._closing_fenced.add(sid)
                        self._closing_epochs[sid] = max(
                            self._closing_epochs.get(sid, 0), close_epoch
                        )
                    else:
                        self._failed_sessions[sid] = meta

                with self._state_lock:
                    restore_meta(retry_raw, finalize=False)
                    restore_meta(finalize_raw, finalize=True)
                restored += 1
            except (TypeError, ValueError, OverflowError):
                log.warning("auto-titler state: skipping invalid record %s", sid[:12])
                with self._state_lock:
                    self._pending.pop(sid, None)
                    self._failed_sessions.pop(sid, None)
                    self._finalize_intents.pop(sid, None)
                    self._closing_fenced.discard(sid)
                    self._rename_counts.pop(sid, None)
                continue
        if restored:
            log.info("auto-titler state: restored %d session(s)", restored)
        if not unresolved:
            self._persist_state()

    # -- 评估 ---------------------------------------------------------------

    def evaluate(
        self,
        session_id: str,
        force: bool = False,
        blind: bool = False,
        claim_epoch: Optional[int] = None,
    ) -> Dict[str, Any]:
        t0 = time.monotonic()
        res = self._do_evaluate(session_id, force=force, blind=blind, claim_epoch=claim_epoch)
        dur_ms = int((time.monotonic() - t0) * 1000)
        action = res.get("action", "unknown")
        reason = res.get("reason") or res.get("candidate") or res.get("title") or ""
        log_line = f"sid={session_id[:8]} act={action} reason={reason} dur={dur_ms}ms"
        self._audit_log.append(log_line)
        log.info("auto-titler audit: %s", log_line)
        return res

    def _do_evaluate(
        self,
        session_id: str,
        force: bool = False,
        blind: bool = False,
        claim_epoch: Optional[int] = None,
    ) -> Dict[str, Any]:
        try:
            try:
                result = self._execute_evaluate(
                    session_id, force=force, blind=blind, claim_epoch=claim_epoch
                )
            except TypeError:
                result = self._execute_evaluate(
                    session_id, force=force, blind=blind
                )
            return result
        finally:
            # State mutations in _execute_evaluate and their durable snapshot are
            # one barrier transaction. Network I/O remains outside this section.
            with self._state_lock:
                self._pre_llm_snapshots.pop(session_id, None)
                self._persist_state_locked()

    def _execute_evaluate(
        self,
        session_id: str,
        force: bool = False,
        blind: bool = False,
        claim_epoch: Optional[int] = None,
    ) -> Dict[str, Any]:
        with self._state_lock:
            first_turn_override = session_id in self._active_override_sessions
            if claim_epoch is not None:
                active_claim = claim_epoch
            elif force and session_id not in self._worker_epochs:
                active_claim = int(self._finalize_intents.get(session_id, {}).get("close_epoch", 0))
            else:
                active_claim = 0
        if not self.cfg.get("enabled", True) and not force and not first_turn_override:
            return {"action": "disabled"}
        db = self.db
        if not force and not first_turn_override:
            last = self._last_eval.get(session_id)
            interval = float(self.cfg.get("min_interval_minutes", 5)) * 60
            if last is not None and (time.monotonic() - last) < interval:
                return {"action": "throttled"}

        try:
            src = db.get_session_title_source(session_id)
        except Exception:
            src = None
        if src == SessionDB.TITLE_SOURCE_USER:
            # 用户权威变化使任何 stale 候选失效并移出重试账本
            self._pending.pop(session_id, None)
            with self._retry_lock:
                self._failed_sessions.pop(session_id, None)
                self._clear_finalize_intent_locked(session_id, close_epoch=active_claim)
            with self._state_lock:
                self._eager_pre_sessions.discard(session_id)
            return {"action": "skipped", "reason": "user title is authoritative"}

        try:
            current = db.get_session_title(session_id)
        except Exception:
            current = None

        # NULL provenance（provenance 列出现前的老行）在 Hermes 里按 user 权威
        # 对待（hermes_state._title_rank：旧自动标题与当年手动 /title 无法区分），
        # 已有标题时 llm 永远写不进去——这是官方保守设计，尊重它，不算失败，移出重试。
        if src is None and current:
            with self._retry_lock:
                self._failed_sessions.pop(session_id, None)
                self._clear_finalize_intent_locked(session_id, close_epoch=active_claim)
            with self._state_lock:
                self._eager_pre_sessions.discard(session_id)
            return {"action": "skipped", "reason": "legacy title (NULL provenance) is protected"}

        # 达到上限时在调用模型前短路，避免继续消耗标题模型额度。blind 是用户
        # 显式的 rename-now/批量重生成路径，保留其旁路语义；首次无标题生成
        # 与首回合 end 强制覆盖也不应消耗“替换次数”。
        if not first_turn_override:
            limit_result = self._rename_limit_result(
                session_id,
                current,
                blind=blind,
            )
            if limit_result is not None:
                self._pending.pop(session_id, None)
                with self._retry_lock:
                    self._failed_sessions.pop(session_id, None)
                    self._clear_finalize_intent_locked(session_id, close_epoch=active_claim)
                return limit_result

        recent, all_user, opening, earlier_summary = load_context_with_summary(
            db,
            session_id,
            int(self.cfg.get("recent_turns", 2)),
            bool(self.cfg.get("include_all_user_messages", True)),
            int(self.cfg.get("opening_turns", 2)),
            bool(self.cfg.get("ignore_model_messages", False)),
            int(self.cfg.get("preview_chars", 100)),
            int(self.cfg.get("user_message_threshold", 40)),
            int(self.cfg.get("user_message_preview_chars", 300)),
            summary_chars=(
                int(self.cfg.get("retitle_summary_chars", 1600)) if blind
                else int(self.cfg.get("summary_preview_chars", 1200))
            ),
        )

        if not recent:
            # 如果存在 pre_llm_call 暂存的首轮快照，解决 Hermes 持久化落盘慢于 worker 启动的真实 DB 竞态
            fallback_u = self._pre_llm_snapshots.pop(session_id, None)
            if fallback_u:
                user_txt = fallback_u if isinstance(fallback_u, str) else str(fallback_u.get("content", ""))
                if user_txt:
                    recent = [("user", user_txt)]
                    opening = [("user", user_txt)]
                    all_user = [("user", user_txt)]
            if not recent:
                # 无消息会话不可评估，移出重试账本防止无限重试
                with self._retry_lock:
                    self._failed_sessions.pop(session_id, None)
                    self._clear_finalize_intent_locked(session_id, close_epoch=active_claim)
                return {"action": "skipped", "reason": "no messages"}

        self._last_eval[session_id] = time.monotonic()
        # 压缩会话的可见消息位于摘要之后，本质上是新鲜续段，不是原始
        # opening。blind 重生成保留其中的真实用户意图，供模型识别持续的新
        # 阶段；assistant/recent 与伪 opening 仍清空，避免收尾回复或执行细节
        # 抢走主线。日常评估保持原行为。
        if blind and earlier_summary:
            recent = []
            opening = []
        # derived 是 Hermes 从首条用户消息截出的临时兜底，说明原生标题 LLM
        # 尚未成功升级；未命名的会话（current is None/empty）更必须生成标题。
        # 因此只要无标题或仍是 derived，或首回合 end 覆盖，就强制要求本次模型给出正式标题（rename-only）。
        force_rename = not current or src == SessionDB.TITLE_SOURCE_DERIVED or first_turn_override
        # 评审协议：0 = 关闭（单轮评估直接改名写库）；
        # 1 = 确认 1 次（第 1 轮产生候选 pending，第 2 轮模型觉得上一轮改名可以则 approve 采用落库）；
        # derived/无标题升级、blind 终局评估及首回合 end 覆盖旁路直接提交。
        needed = 0 if (blind or first_turn_override) else int(self.cfg.get("rename_confirmations", 0))
        review = needed >= 1 and src == SessionDB.TITLE_SOURCE_LLM and bool(current)
        pending = self._pending.get(session_id) if review else None
        proposed = pending["title"] if pending else None
        if blind or first_turn_override:
            # 终局评估与首回合覆盖以内容为准，直接落库并忽略进程内待审候选
            self._pending.pop(session_id, None)

        action, title = self._generate(
            current, recent, all_user, opening,
            force_rename=force_rename, blind=blind, proposed=proposed,
            earlier_summary=earlier_summary, session_id=session_id,
        )
        log.info(
            "auto-titler %s: captured current=%r input=%s",
            session_id[:12],
            current,
            _audit_input_shape(current, recent, all_user, opening, earlier_summary),
        )
        # 规范化 + 截断后的候选才与当前标题比较；相等 = keep（不是 failed/加后缀）
        candidate = self._prepare_candidate(title) if title else None

        if action == "error":
            # 模型调用失败（网络中断/503/超时）：登记入失败重试字典，实施指数退避，
            # 并避免记录正常 _last_eval 锁死重试窗口。
            self._pending.pop(session_id, None)
            self._last_eval.pop(session_id, None)
            err = self._last_generate_errors.pop(session_id, "") or getattr(self, "_last_generate_error", "") or ""
            capacity = _is_capacity_error(err)
            with self._retry_lock:
                meta = self._failed_sessions.get(session_id, {"attempts": 0})
                attempts = int(meta.get("attempts", 0)) + 1
                delay = min(30 * (2 ** (attempts - 1)), 600)
                self._failed_sessions[session_id] = {
                    "attempts": attempts,
                    "next_retry_at": time.monotonic() + delay,
                    "capacity": capacity or bool(meta.get("capacity")),
                }
            log.warning(
                "auto-titler %s: recorded failure (attempt %d%s, next retry in %ds)",
                session_id[:12],
                attempts,
                ", capacity" if capacity or meta.get("capacity") else "/5",
                delay,
            )
            return {"action": "failed", "reason": "model call failed"}

        # 评估成功推进：仅在有效生成新标题或已有标题维持 keep 时清除重试记录
        if (action == "rename" and candidate) or current:
            with self._retry_lock:
                self._failed_sessions.pop(session_id, None)

        if review and proposed:
            if proposed == current:
                # stale 候选（当前标题已是它）：无意义，放弃
                self._pending.pop(session_id, None)
                log.info("auto-titler %s: keep (stale review candidate)", session_id[:12])
                return {"action": "keep"}
            if action == "approve" or (action == "rename" and candidate == proposed):
                # 模型背书候选（显式 approve，或裁决时原样重复）→ 落库候选本身
                return self._commit_rename(
                    db,
                    session_id,
                    proposed,
                    expected_title=current,
                    bypass_limit=blind,
                    claim_epoch=active_claim,
                )
            if action == "rename" and candidate and candidate != current:
                # 模型给出更好的新候选：替换待审，旧候选作废，并记录生成时的 current 快照作为 base_title
                self._pending[session_id] = {"title": candidate, "base_title": current}
                log.info(
                    "auto-titler %s: pending (review) candidate=%r",
                    session_id[:12], candidate,
                )
                return {"action": "pending", "candidate": candidate}
            # keep / 无效 rename / 候选等于当前标题：放弃待审候选
            self._pending.pop(session_id, None)
            log.info("auto-titler %s: keep (review, current=%r)", session_id[:12], current)
            return {"action": "keep"}

        if action != "rename" or not candidate or candidate == current:
            self._pending.pop(session_id, None)
            # 无原标题时模型不得用 keep 伪装成成功，避免缺标题误报为 keep
            if not current:
                reason = "untitled session did not produce a new title"
                log.warning("auto-titler %s: %s", session_id[:12], reason)
                with self._retry_lock:
                    meta = self._failed_sessions.get(session_id, {"attempts": 0})
                    attempts = int(meta.get("attempts", 0)) + 1
                    delay = min(30 * (2 ** (attempts - 1)), 600)
                    self._failed_sessions[session_id] = {
                        "attempts": attempts,
                        "next_retry_at": time.monotonic() + delay,
                        "capacity": False,
                    }
                return {"action": "failed", "reason": reason}
            log.info("auto-titler %s: keep (current=%r)", session_id[:12], current)
            return {"action": "keep"}

        if review:
            # 首次提出候选：挂起待审，不写库，同时捕获生成输入时的 current 快照作为 base_title
            self._pending[session_id] = {"title": candidate, "base_title": current}
            log.info(
                "auto-titler %s: pending candidate=%r base=%r", session_id[:12], candidate, current,
            )
            return {"action": "pending", "candidate": candidate}

        return self._commit_rename(
            db,
            session_id,
            candidate,
            expected_title=current,
            bypass_limit=blind or first_turn_override,
            claim_epoch=active_claim,
        )

    @staticmethod
    def _language_rule() -> str:
        """Use the host-normalized title locale, falling back to display.language."""
        try:
            from hermes_cli.config import load_config_readonly
            root = load_config_readonly() or {}
            aux = root.get("auxiliary") or {}
            title_cfg = aux.get("title_generation") or {}
            configured = str(title_cfg.get("language") or "").strip()
            from agent.i18n import get_language, _normalize_lang
            raw = _normalize_lang(configured) if configured else get_language()
            if raw:
                return f"Write the title in Hermes display language (language code: {raw}). Do not switch to other languages."
        except Exception:
            pass
        return "Write the title in the primary natural language of the user's messages."

    def _commit_rename(
        self,
        db: SessionDB,
        session_id: str,
        title: str,
        expected_title: Optional[str] = None,
        *,
        bypass_limit: bool = False,
        claim_epoch: Optional[int] = None,
    ) -> Dict[str, Any]:
        """实际写库 + 状态清理。title 必须是已 _prepare_candidate 的候选。"""
        # _do_evaluate() 已在模型调用前检查过一次；这里再检查一次，覆盖
        # review/pending 和并发评估之间的窗口。锁只保护插件计数，不替代
        # SessionDB 自己的写入 CAS。
        active_claim = claim_epoch if claim_epoch is not None else 0
        if not bypass_limit:
            current = expected_title
            if current is None:
                try:
                    current = db.get_session_title(session_id)
                except Exception:
                    current = None
            result = self._rename_limit_result(
                session_id,
                current,
                blind=False,
            )
            if result is not None:
                self._pending.pop(session_id, None)
                with self._retry_lock:
                    self._failed_sessions.pop(session_id, None)
                    self._clear_finalize_intent_locked(session_id, close_epoch=active_claim)
                return result

        previous_title = expected_title
        if previous_title is None:
            try:
                previous_title = db.get_session_title(session_id)
            except Exception:
                previous_title = None

        if bypass_limit:
            written = self._write(db, session_id, title, expected_title=expected_title)
        else:
            # 把“再次检查上限 → 写入 → 成功后计数”放在同一把进程锁内，
            # 避免两个绕过 hook 去重的同步调用同时通过最后一道门。
            with self._rename_count_lock:
                result = self._rename_limit_result(
                    session_id,
                    previous_title,
                    blind=False,
                )
                if result is not None:
                    self._pending.pop(session_id, None)
                    self._failed_sessions.pop(session_id, None)
                    self._clear_finalize_intent_locked(session_id, close_epoch=active_claim)
                    return result
                written = self._write(db, session_id, title, expected_title=expected_title)
                # 只统计“已有标题 → 新标题”的自动替换；首次生成不消耗额度。
                # 写入成功后才递增，避免失败/冲突消耗额度。
                if written and previous_title and previous_title != written:
                    self._rename_counts[session_id] = self._rename_counts.get(session_id, 0) + 1
        if written:
            self._pending.pop(session_id, None)
            log.info("auto-titler %s: renamed -> %r", session_id[:12], written)
            return {"action": "renamed", "title": written}
        self._pending.pop(session_id, None)
        reason = self._protected_reason(db, session_id)
        if reason:
            # LLM 调用期间用户 /title（或出现 legacy NULL 标题）：权威方胜出，不算失败
            log.info("auto-titler %s: %s", session_id[:12], reason)
            return {"action": "skipped", "reason": reason}
        log.warning("auto-titler %s: rename failed to write title %r", session_id[:12], title)
        return {"action": "failed"}

    def _rename_limit_result(
        self,
        session_id: str,
        current: Optional[str],
        *,
        blind: bool,
    ) -> Optional[Dict[str, Any]]:
        """Return a skip result when automatic replacement reached its optional cap."""
        if blind or not current:
            return None
        limit = max(0, int(self.cfg.get("max_renames_per_session", 0)))
        if limit <= 0:
            return None
        with self._rename_count_lock:
            count = self._rename_counts.get(session_id, 0)
        if count < limit:
            return None
        log.info(
            "auto-titler %s: automatic rename limit reached (%d/%d)",
            session_id[:12], count, limit,
        )
        return {
            "action": "skipped",
            "reason": "max_renames_per_session reached",
            "renames": count,
            "limit": limit,
        }

    # -- 模型生成 -----------------------------------------------------------

    def _generate(
        self,
        current: Optional[str],
        recent: List[Tuple[str, str]],
        all_user: List[Tuple[str, str]],
        opening: List[Tuple[str, str]],
        force_rename: bool = False,
        blind: bool = False,
        proposed: Optional[str] = None,
        earlier_summary: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Tuple[str, Optional[str]]:
        raise NotImplementedError("Title decision policy must be provided by policy.AutoTitler")

    def _record_usage(self, session_id: Optional[str], res: Any) -> None:
        """把真实 PluginLlm 调用记入 SessionDB.record_auxiliary_usage。

        必须手动记账：宿主 PluginLlm 以 task=None 调 auxiliary_client，环境
        记账不会把它归到 hermes_auto_titler 任务（上游 #23270 的任务维度统计
        依赖显式 task 参数）。task='hermes_auto_titler'，进 dashboard 的任务
        维度统计。老宿主 / 测试 fake 没有该 API 或 usage 字段时静默跳过，
        绝不阻断评估。
        """
        if not session_id:
            return
        rec = getattr(self.db, "record_auxiliary_usage", None)
        if not callable(rec):
            return
        usage = getattr(res, "usage", None) or {}
        try:
            rec(
                session_id,
                "hermes_auto_titler",
                model=getattr(res, "model", None) or self.cfg.get("model") or None,
                billing_provider=getattr(res, "provider", None) or None,
                input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                cache_read_tokens=int(getattr(usage, "cache_read_tokens", 0) or 0),
                cache_write_tokens=int(getattr(usage, "cache_write_tokens", 0) or 0),
                estimated_cost_usd=getattr(usage, "cost_usd", None),
            )
        except Exception as e:
            log.debug("auto-titler aux usage recording skipped: %s", e)

    # -- 写回 ---------------------------------------------------------------

    def _bounds(self) -> Tuple[int, int]:
        cfg_len = self.cfg.get("max_title_length")
        max_len = min(int(cfg_len), SessionDB.MAX_TITLE_LENGTH) if cfg_len is not None else SessionDB.MAX_TITLE_LENGTH
        # 显示列宽硬限（中文=2 列）：complete 风格放宽 12 列
        max_cols = int(self.cfg.get("max_display_width", 40))
        if self.cfg.get("title_style", "concise") == "complete":
            max_cols += 12
        return max_len, max_cols

    def _clean_title(self, title: str) -> str:
        """规范化模型输出：折叠空白、去包裹引号、去 'Title:'/'标题：' 前缀、去尾部句读。"""
        t = re.sub(r"\s+", " ", (title or "").strip())
        for _ in range(3):
            prev = t
            t = t.strip('"\'“”‘’「」『』')
            t = re.sub(r"^(?:title|标题)\s*[:：]\s*", "", t, flags=re.IGNORECASE)
            t = re.sub(r"[.。!！?？,，;；:：…]+$", "", t).strip()
            if t == prev:
                break
        return t

    def _prepare_candidate(self, title: str) -> Optional[str]:
        """规范化 + 双重硬截断（字符上限 + 列宽上限）后的候选标题；空则 None。"""
        t = self._clean_title(title)
        if not t:
            return None
        max_len, max_cols = self._bounds()
        t = truncate_to_width(t, max_cols)
        t = re.sub(r"[\s\"'“”‘’「」『』.。!！?？,，;；:：…]+$", "", t)
        if len(t) > max_len:
            t = t[:max_len]
            t = re.sub(r"[\s\"'“”‘’「」『』.。!！?？,，;；:：…]+$", "", t)
        return t or None

    def _protected_reason(self, db: SessionDB, session_id: str) -> Optional[str]:
        """写回前的权威性复核（LLM 调用期间用户可能 /title 或出现 legacy 标题）。

        返回非 None 表示当前标题受保护，写回必须放弃。
        """
        try:
            src = db.get_session_title_source(session_id)
        except Exception:
            return None
        if src == SessionDB.TITLE_SOURCE_USER:
            return "user title is authoritative (set during evaluation)"
        if src is None:
            try:
                cur = db.get_session_title(session_id)
            except Exception:
                cur = None
            if cur:
                return "legacy title (NULL provenance) is protected"
        return None

    def _write(self, db: SessionDB, session_id: str, title: str, expected_title: Optional[str] = None) -> Optional[str]:
        max_len, max_cols = self._bounds()
        title = self._prepare_candidate(title)
        if not title:
            return None

        # 快速失败：写回前先核对一次权威性（LLM 调用期间用户可能 /title）。
        # apply() 内每次实际写尝试还会再刷新一次来源与保护，覆盖冲突重试
        # 之间新出现的竞态窗口。
        if self._protected_reason(db, session_id):
            return None

        def apply(t: str) -> bool:
            # 每次实际写尝试前都重新核对权威性与写路径来源
            if self._protected_reason(db, session_id):
                return False
            try:
                src = db.get_session_title_source(session_id)
            except Exception:
                src = None

            # 无标题 / derived → llm：set_auto_title 是单事务 CAS（title +
            # source 一起写），原子，无 crash window；legacy NULL 由上游按
            # user 权威拒绝。
            if src is None or src == SessionDB.TITLE_SOURCE_DERIVED:
                return bool(
                    db.set_auto_title(session_id, t, source=SessionDB.TITLE_SOURCE_LLM)
                )
            # 来源在我们检查后变成了 user：永不覆盖用户标题
            if src == SessionDB.TITLE_SOURCE_USER:
                return False

            # llm → llm：执行真正的单事务原子 CAS，杜绝 TOCTOU 竞态。
            # 直接在 SQLite 事务中基于进入 _write 前读取到的 expected 快照比较并写入：
            # UPDATE sessions SET title = ?, title_source = ?
            # WHERE id = ? AND title IS ? AND title_source = 'llm'
            # 若用户在 LLM 推理期间手动改名（或外部写入改变了 title/source），更新匹配 0 行直接失败（返回 False），
            # 绝对不覆盖用户手改标题，也杜绝了分步 write -> readback -> restore 之间的非原子漏洞。
            expected = expected_title if expected_title is not None else db.get_session_title(session_id)
            def _atomic_llm_cas(conn):
                return conn.execute(
                    "UPDATE sessions SET title = ?, title_source = ? "
                    "WHERE id = ? AND title IS ? AND title_source = ?",
                    (t, SessionDB.TITLE_SOURCE_LLM, session_id, expected, SessionDB.TITLE_SOURCE_LLM),
                ).rowcount > 0

            exec_write = getattr(db, "_execute_write", None)
            if callable(exec_write):
                try:
                    return bool(exec_write(_atomic_llm_cas))
                except Exception as e:
                    log.warning("auto-titler %s: atomic llm CAS execution failed: %s", session_id[:12], e)
                    return False

            # 后备路径（极老宿主无 _execute_write 时）：严格两步 fail-closed
            if db.set_session_title(session_id, t):
                try:
                    stored = db.get_session_title(session_id)
                except Exception as e:
                    log.warning("auto-titler %s: failed to readback title after write, failing closed: %s", session_id[:12], e)
                    return False
                if stored != t:
                    log.warning("auto-titler %s: stored title %r != expected %r, aborting source recovery", session_id[:12], stored, t)
                    return False
                try:
                    res_src = db.set_session_title_source(session_id, SessionDB.TITLE_SOURCE_LLM)
                    if res_src is False:
                        log.warning("auto-titler %s: set_session_title_source returned False, failing closed", session_id[:12])
                        return False
                except Exception as e:
                    log.warning("auto-titler %s: exception during source recovery, failing closed: %s", session_id[:12], e)
                    return False
                return True
            return False

        try:
            if apply(title):
                return title
        except ValueError:
            pass  # 唯一性冲突 → 加后缀重试

        for i in range(2, 20):
            suffix = f" ({i})"
            base = title
            # 后缀必须同时落在字符与列宽双上限内：收窄 base 腾出空间
            while base and (
                len(base) + len(suffix) > max_len
                or display_width(base + suffix) > max_cols
            ):
                base = base[:-1].rstrip()
            if not base:
                break
            cand = base + suffix
            try:
                if apply(cand):
                    return cand
                # 非唯一性冲突的失败（保护/同级 no-op 等不可恢复原因）：
                # 后缀重试无意义，直接放弃。
                break
            except ValueError:
                continue
        return None

    # -- 批量重生成 ---------------------------------------------------------

    def retitle_all(
        self,
        dry_run: bool = False,
        limit: Optional[int] = None,
        min_messages: int = 0,
    ) -> List[Dict[str, Any]]:
        db = self.db
        rows = db.list_sessions_rich(
            limit=limit or 1000,
            min_message_count=min_messages,
            include_children=False,
        )
        results: List[Dict[str, Any]] = []
        for row in rows:
            sid = row.get("id")
            if not sid:
                continue
            try:
                src = db.get_session_title_source(sid)
            except Exception:
                src = None
            if src == SessionDB.TITLE_SOURCE_USER:
                results.append({"session_id": sid, "action": "skipped", "reason": "user title"})
                continue
            if dry_run:
                results.append({"session_id": sid, "action": "dry-run", "title": row.get("title")})
                continue
            try:
                # force=False：同一实例刚评估过（min_interval 内）的会话被节流跳过
                r = self.evaluate(sid, force=False, blind=True)
                results.append({"session_id": sid, **r})
            except Exception as e:
                results.append({"session_id": sid, "action": "error", "reason": str(e)})
        return results


def _parse_decision(text: str) -> Tuple[str, Optional[str]]:
    """容错解析模型输出：只认 JSON（全文或内嵌），不认自由文本启发式。

    宁可 keep 也不从自然语言里猜标题——猜错 = 幻觉写回。
    """
    text = (text or "").strip()
    candidates = []

    def try_loads(s: str) -> Optional[dict]:
        try:
            d = json.loads(s)
            return d if isinstance(d, dict) else None
        except Exception:
            return None

    if text:
        candidates.append(try_loads(text))
    if not any(candidates):
        # 提取第一个含 action 的 JSON 对象（容忍 markdown 围栏/前后缀）
        m = re.search(r"\{[^{}]*\"action\"[^{}]*\}", text)
        if m:
            candidates.append(try_loads(m.group(0)))
    for d in candidates:
        if not d:
            continue
        action = str(d.get("action", "")).lower()
        if action in ("keep", "approve", "rename"):
            title = str(d.get("title") or "").strip().strip('"').strip("'")
            if action == "rename" and not title:
                # rename 动作必须提供非空标题，否则视为非法决策
                return "error", None
            return action, (title or None)
    return "error", None
