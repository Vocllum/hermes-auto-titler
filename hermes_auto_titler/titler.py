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
  走正常节流（force=False）且不与进行中的自动评估重复；关闭评估刻意保持
  同步——可能阻塞至 provider timeout，换取关闭前完成标题更新的确定性
- 轮次评估在 daemon worker 中执行（携带当前 profile Context），hook 立即
  返回；同一会话 in-flight 去重
- early_turn_eval 的早期评估只对无标题或 derived 来源的会话触发
  （llm/user/legacy 不提前调用模型；正常 every_n_turns 边界不受影响）
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import threading
import time
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
        # 频次窗口：session_id -> 实际改名时间戳列表（滑动 60 分钟）
        self._rename_times: Dict[str, List[float]] = {}
        # 同一会话同一时刻只允许一个进行中的模型评估（hook 并发去重）
        self._inflight: set[str] = set()
        self._inflight_lock = threading.Lock()

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
        every = max(1, int(self.cfg.get("every_n_turns", 4)))
        if n % every == 0:
            self._submit_eval(session_id)
        elif (
            self.cfg.get("early_turn_eval", False)
            and n < every
            and self._early_eligible(session_id)
        ):
            self._submit_eval(session_id)

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
        self._close_eval(session_id)

    # -- 评估调度 -----------------------------------------------------------

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

    def _submit_eval(self, session_id: str) -> None:
        """把轮次评估提交到 daemon worker；同一会话已有 in-flight 评估则跳过。

        评估离开 hook 关键路径（hook 立即返回）。worker 经
        tools.thread_context.propagate_context_to_thread 携带当前 profile
        Context（老宿主退回 contextvars），保证辅助模型调用在正确上下文里
        执行。轮数/节流/去重计数器都是进程内的：进程重启后清零，属可接受
        的本地行为。
        """
        with self._inflight_lock:
            if session_id in self._inflight:
                return
            self._inflight.add(session_id)
        try:
            worker = threading.Thread(
                target=_wrap_with_context(self._eval_worker),
                args=(session_id,),
                name=f"autotitler-eval-{session_id[:8]}",
                daemon=True,
            )
            worker.start()
        except Exception as e:
            # 线程构造/启动失败：清掉 in-flight 标记（后续轮次可重试），
            # 只记日志，绝不让 hook 抛异常。
            log.warning(
                "auto-titler failed to start eval worker for %s: %s",
                session_id[:12], e,
            )
            with self._inflight_lock:
                self._inflight.discard(session_id)

    def _eval_worker(self, session_id: str) -> None:
        try:
            self.evaluate(session_id)
        except Exception as e:
            log.warning("auto-titler background evaluate failed: %s", e)
        finally:
            with self._inflight_lock:
                self._inflight.discard(session_id)

    def _close_eval(self, session_id: str) -> None:
        """真实关闭评估：正常节流（force=False）；已有 in-flight 评估时不重复。

        刻意保持同步：关闭是用户可见的终局动作，同步评估可能阻塞至 provider
        timeout（最长 ~30s），但换取「关闭前标题已更新」的确定性；轮次评估
        仍走异步 worker。
        """
        if not self.cfg.get("on_close", True):
            return
        with self._inflight_lock:
            if session_id in self._inflight:
                log.debug(
                    "auto-titler close: eval already in flight for %s, skipped",
                    session_id[:12],
                )
                return
            self._inflight.add(session_id)
        try:
            self.evaluate(session_id, force=False)
        except Exception as e:
            log.warning("auto-titler close evaluate failed: %s", e)
        finally:
            with self._inflight_lock:
                self._inflight.discard(session_id)

    # -- 评估 ---------------------------------------------------------------

    def evaluate(self, session_id: str, force: bool = False, blind: bool = False) -> Dict[str, Any]:
        db = self.db
        if not force:
            last = self._last_eval.get(session_id)
            interval = float(self.cfg.get("min_interval_minutes", 5)) * 60
            if last is not None and (time.time() - last) < interval:
                return {"action": "throttled"}

        try:
            src = db.get_session_title_source(session_id)
        except Exception:
            src = None
        if src == SessionDB.TITLE_SOURCE_USER:
            # 用户权威变化使任何 stale 候选失效
            self._pending.pop(session_id, None)
            return {"action": "skipped", "reason": "user title is authoritative"}

        try:
            current = db.get_session_title(session_id)
        except Exception:
            current = None

        # NULL provenance（provenance 列出现前的老行）在 Hermes 里按 user 权威
        # 对待（hermes_state._title_rank：旧自动标题与当年手动 /title 无法区分），
        # 已有标题时 llm 永远写不进去——这是官方保守设计，尊重它，不算失败。
        if src is None and current:
            return {"action": "skipped", "reason": "legacy title (NULL provenance) is protected"}

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
            return {"action": "skipped", "reason": "no messages"}

        self._last_eval[session_id] = time.time()
        # 压缩会话的可见消息位于摘要之后，本质上是新鲜续段，不是原始
        # opening。blind 重生成保留其中的真实用户意图，供模型识别持续的新
        # 阶段；assistant/recent 与伪 opening 仍清空，避免收尾回复或执行细节
        # 抢走主线。日常评估保持原行为。
        if blind and earlier_summary:
            recent = []
            opening = []
        # derived 是 Hermes 从首条用户消息截出的临时兜底，说明原生标题 LLM
        # 尚未成功升级；短句也可能是「这个文件夹是做什么的」这类污染标题，
        # 因此只要仍是 derived 就要求本次模型给出正式标题。
        force_rename = src == SessionDB.TITLE_SOURCE_DERIVED and bool(current)
        # 评审协议（rename_confirmations>1）：llm→llm 的改名候选先挂起，由下一
        # 次评估裁决——approve（背书落库）/ rename（换更好的新候选）/ keep（放弃），
        # 模型原样重复候选也视为背书。derived/无标题升级是补漏、blind 是终局
        # 评估（全貌已知），都旁路直接提交。
        needed = 1 if blind else int(self.cfg.get("rename_confirmations", 1))
        review = needed > 1 and src == SessionDB.TITLE_SOURCE_LLM and bool(current)
        pending = self._pending.get(session_id) if review else None
        proposed = pending["title"] if pending else None
        if blind:
            # 终局评估以内容为准，直接落库并忽略进程内待审候选
            self._pending.pop(session_id, None)

        action, title = self._generate(
            current, recent, all_user, opening,
            force_rename=force_rename, blind=blind, proposed=proposed,
            earlier_summary=earlier_summary, session_id=session_id,
        )
        # 规范化 + 截断后的候选才与当前标题比较；相等 = keep（不是 failed/加后缀）
        candidate = self._prepare_candidate(title) if title else None

        if review and proposed:
            if proposed == current:
                # stale 候选（当前标题已是它）：无意义，放弃
                self._pending.pop(session_id, None)
                log.info("auto-titler %s: keep (stale review candidate)", session_id[:12])
                return {"action": "keep"}
            if action == "approve" or (action == "rename" and candidate == proposed):
                # 模型背书候选（显式 approve，或裁决时原样重复）→ 落库候选本身
                return self._commit_rename(db, session_id, proposed)
            if action == "rename" and candidate and candidate != current:
                # 模型给出更好的新候选：替换待审，旧候选作废
                self._pending[session_id] = {"title": candidate}
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
            # keep：模型否决了之前的候选 → 清空 pending
            self._pending.pop(session_id, None)
            log.info("auto-titler %s: keep (current=%r)", session_id[:12], current)
            return {"action": "keep"}

        if review:
            # 首次提出候选：挂起待审，不写库
            self._pending[session_id] = {"title": candidate}
            log.info(
                "auto-titler %s: pending candidate=%r", session_id[:12], candidate,
            )
            return {"action": "pending", "candidate": candidate}

        return self._commit_rename(db, session_id, candidate)

    def _commit_rename(self, db: SessionDB, session_id: str, title: str) -> Dict[str, Any]:
        """频次上限检查 + 实际写库 + 记账。title 必须是已 _prepare_candidate 的候选。

        时间戳在写库成功后才记账——被保护/写失败的尝试不消耗名额；
        触顶保留待审候选，窗口滑过后模型背书即可写入。
        """
        cap = int(self.cfg.get("renames_per_hour", 0))
        if cap > 0:
            now = time.time()
            times = [t for t in self._rename_times.get(session_id, []) if now - t < 3600]
            if len(times) >= cap:
                log.info(
                    "auto-titler %s: capped (%d renames in the last hour)",
                    session_id[:12], len(times),
                )
                return {"action": "capped", "reason": "renames_per_hour limit"}
            self._rename_times[session_id] = times

        written = self._write(db, session_id, title)
        if written:
            self._pending.pop(session_id, None)
            # 记账：仅实际写入成功的改名消耗频次名额
            if cap > 0:
                self._rename_times.setdefault(session_id, []).append(time.time())
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
        strategy = self.cfg.get("strategy", "conservative")
        stability = ""  # 盲改不注入稳定性规则（终局评估以内容为准）
        if blind:
            # retitle-all 盲改：不提供原标题，直接按内容重新命名
            rule = (
                "Generate the best title directly from the conversation "
                "content (the current title is NOT provided); action MUST "
                "be rename. Name the subject or combination of subjects that "
                "explains the largest share of the session's substantive user "
                "requests and completed work. Judge dominance by coverage and "
                "sustained intent, not by where a topic appears in time. Do not title "
                "the session after final validation, cleanup, or parameter tuning "
                "unless that work is the session's entire subject."
            )
        elif proposed and not blind:
            # 评审模式：裁决上一轮评估提出的候选标题
            rule = (
                "A candidate title was proposed at a previous evaluation and is "
                "shown below as the Proposed title. Judge it against the full "
                "conversation: action \"approve\" if it is already the best "
                "summary as-is; \"rename\" with a better title if it misses the "
                "main subject or is wrong; \"keep\" if the current title is fine "
                "and the proposal is not an improvement. Replacing an accurate "
                "current title with a narrower or less complete proposal is not "
                "an improvement."
            )
        elif strategy == "aggressive":
            rule = (
                "Rename whenever your title is better. "
                "The opening main task always outranks the latest subtask."
            )
        else:
            rule = (
                "Rename only when the current title no longer summarizes "
                "the conversation as a whole. When uncertain, keep."
            )
        if force_rename and not blind:
            rule += (
                " The current title is a provisional deterministic fallback "
                "copied from the first user message, not a model title; "
                "action MUST be rename."
            )

        if blind:
            hierarchy = (
                "\nInformation hierarchy:\n"
                "- Use the whole available history. The opening, latest phase, and "
                "current blocker are evidence, not automatic winners.\n"
                "- In a compacted session, the earlier summary is a historical "
                "baseline and may predate a later phase.\n"
                "- A post-summary continuation may supersede or extend that baseline "
                "only when multiple substantive user requests establish a sustained "
                "topic shift.\n"
                "- One-off follow-ups, current blockers, validation, cleanup, and "
                "parameter tuning remain details even when they are the latest messages.\n"
            )
            multi_subject_rule = (
                "- When multiple phases are each substantial, prefer one precise shared "
                "umbrella only if it truthfully covers them; otherwise name up to two "
                "major subjects within the full length budget. A single task involving "
                "several core entities (sites, products, repositories, machines) should "
                "name them all.\n"
            )
        else:
            hierarchy = (
                "\nInformation hierarchy:\n"
                "- Subject comes from the session opening (or the earlier "
                "history summary when the original opening was compacted away); "
                "it is the main topic the session started about. A device or "
                "entity that appears only in the final turns is detail, not "
                "the subject.\n"
                "- Main intent comes from the user-message trajectory.\n"
                "- Recent turns show the latest event, current state, or a "
                "genuine topic shift; they must never define the subject by "
                "themselves.\n"
                "- Interim tools, temporary measures, and final validation, cleanup, "
                "or parameter tuning do not define the subject even when they are "
                "the latest messages.\n"
                "- In a compacted session the earlier history summary anchors the "
                "subject; the visible opening is only a local continuation after it "
                "and must not replace the summary as the main through-line.\n"
            )
            stability = (
                "\nStability rules:\n"
                "- The subject changes ONLY for a sustained topic shift confirmed by "
                "MULTIPLE later user requests. A single recent user message, however "
                "specific, is a subtask unless it opens an entirely new long-running "
                "effort.\n"
                "- If several subjects each have substantial coverage, prefer a title "
                "that covers them together; changing an existing accurate title to a "
                "narrower one is drift, not improvement.\n"
                "- Keep identifiers already present in the current title when they are "
                "still part of the conversation's subject (for example repository or "
                "product names); dropping them loses retrievability.\n"
                "- Prefer keep over rename when both options would be defensible: a "
                "title that is still broadly correct must not be replaced by one that "
                "is only marginally different.\n"
            )
            multi_subject_rule = (
                "- When the conversation has two distinct tasks, name both "
                "entities even if it uses the full length budget. When one task "
                "involves several core entities (sites, products, repositories, "
                "machines), name them all rather than keeping only the most recent "
                "or most prominent one.\n"
            )

        # 标题风格：concise = 主体标签（Subject + 最小区分意图）/ complete =
        # 简短事件梗概（Subject + 主要意图/事件）。信息类型是第一约束，
        # 长度只是护栏（12 为目标、max_title_length 为硬上限）。
        max_title_len = int(self.cfg.get("max_title_length", 24))
        style = self.cfg.get("title_style", "concise")
        if style == "complete":
            style_req = (
                "SUMMARY STYLE: briefly describe what the conversation is "
                "mainly about. Preserve the main subject and the most "
                "important intent, event, or correction."
            )
        else:
            style_req = (
                "LABEL STYLE: name the conversation. Identify its main "
                "subject and only the minimum intent needed to distinguish "
                "it. Do not retell what happened."
            )
        # JSON 契约：评审模式（有待审候选）增加 approve 动作
        contract = (
            '{"action":"keep"|"approve"|"rename","title":"..."}\n'
            if proposed and not blind
            else '{"action":"keep"|"rename","title":"..."}\n'
        )
        system = (
            "You maintain concise titles for Hermes conversations.\n"
            "Return JSON only:\n"
            f"{contract}"
            f"\n{rule}\n"
            f"\n{style_req}\n"
            f"{hierarchy}"
            f"{stability if not blind else ''}"
            "\nRules:\n"
            f"- Length is a guardrail, not the goal: aim for at most 12 "
            f"characters; never exceed {max_title_len} (Chinese and Latin "
            "each count as 1 character).\n"
            "- Never abbreviate, clip, or leave a word or phrase incomplete to meet the "
            "12-character target; use the full hard budget for a complete label.\n"
            "- Use the dominant language of the user's substantive messages for all ordinary "
            "task, action, and topic words. Generic English technical words such as ingest, "
            "deploy, config, fix, plugin, and update are not identifiers; when the dominant "
            "language is not English, express them in that language. Preserve proper product "
            "names and explicit literal identifiers as-is.\n"
            "- Use the natural word order and idiomatic phrasing of the dominant language; "
            "do not mirror the word order of another language.\n"
            "- Spacing is mandatory: insert exactly one half-width space at every boundary "
            "between CJK characters and Latin letters or digits, on both sides of every "
            "Latin word or run in a mixed-language title. A missing space there is a "
            "formatting error even when the words are correct.\n"
            "- Product and brand names use their established display casing in running text "
            "(each significant part capitalized), not the spelling of config keys, package "
            "names, domains, or CLI commands; treat a lowercase form as an identifier only "
            "when the conversation refers to that literal artifact (a path, command, "
            "package spec, or file), otherwise render the display name.\n"
            "- If you are not certain of a product name's canonical display spelling, do "
            "NOT guess or invent one: express that item in the dominant language (or omit "
            "it) instead.\n"
            "- A final spelling check is mandatory: normalize every plain-language product "
            "and technical term to its established display name and standard spacing, keep "
            "standard spacing around numbers and reference symbols such as '#', and never "
            "merge adjacent Latin names.\n"
            "- Preserve every explicit literal repository, package, file, or command "
            "identifier byte-for-byte, including hyphens, underscores, dots, spacing, "
            "and case (for example hermes-auto-titler). Never shorten or respell it to "
            "meet the 12-character target; use the full hard length budget when needed.\n"
            f"{multi_subject_rule}"
            "- No trailing punctuation, no quotes.\n"
            "- When the conversation has almost no substantive content, produce the shortest "
            "accurate label for what is actually there; do not invent a topic.\n"
            'Reply with JSON only.'
        )

        lines = []
        if not blind:
            lines.append(f"Current title: {current or '(none)'}")
            if proposed:
                lines.append(f"Proposed title: {proposed}")
            lines.append("")
        if earlier_summary and not blind:
            # 压缩会话日常评估：可见窗口开头是摘要之后的局部续段，不是原始
            # opening。摘要锚定主题；局部工具操作与最近轮次不得推翻它。
            # （盲改路径保留原标记：摘要已是 primary，opening 语义由 blind
            # hierarchy 的 supersede 规则约束。）
            lines.append(
                "Visible opening (top of the visible message window; in a "
                "compacted session this is a local continuation AFTER the "
                "earlier summary, NOT the original session opening — the "
                "earlier summary is the subject anchor):"
            )
        else:
            lines.append(
                "Opening (the session's starting turns; the main through-line "
                "anchor; primary subject source):"
            )
        for role, text in opening:
            lines.append(f"{role}: {text}")
        if earlier_summary:
            lines.append("")
            if blind:
                lines.append(
                    "Earlier history summary (primary historical context and historical "
                    "baseline because the original opening is unavailable; it may predate "
                    "a sustained post-summary continuation):"
                )
            else:
                lines.append(
                    "Earlier history summary (historical subject anchor — the original "
                    "opening was compacted away; the session's main through-line comes "
                    "from here. Local tool operations and the latest turns must not "
                    "override it; only a sustained new user task can shift the subject):"
                )
            lines.append(earlier_summary)
        lines.append("")
        lines.append(
            "Recent (the latest turns; the current event, state, or a genuine "
            "topic shift — never the subject by itself):"
        )
        for role, text in recent:
            lines.append(f"{role}: {text}")
        if all_user:
            lines.append("")
            if blind and earlier_summary:
                lines.append(
                    f"Post-summary user continuation ({len(all_user)} user messages; "
                    "newer than the historical summary):"
                )
            else:
                lines.append(
                    "User-message trajectory (how the conversation evolved; "
                    "the main intent source):"
                )
            for _, text in all_user:
                lines.append(f"user: {text}")
        user_prompt = "\n".join(lines)

        try:
            res = self.ctx.llm.complete(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                model=self.cfg.get("model") or None,
                provider=self.cfg.get("provider") or None,
                temperature=0,
                # 插件侧请求上限；当前 Hermes auxiliary_client 会对多数普通
                # OpenAI-compatible 路由省略该 wire 参数，
                # 因此不能把 64 宣称为 provider 实际输出硬上限。
                max_tokens=64,
                timeout=30,
                purpose="auto-title",
            )
            text = getattr(res, "text", "") or ""
        except Exception as e:
            log.warning("auto-titler LLM call failed: %s", e)
            return "keep", None
        self._record_usage(session_id, res)

        action, title = _parse_decision(text)
        if action == "rename" and title:
            return "rename", title
        if action == "approve":
            return "approve", None
        return "keep", None

    # -- 用量记账 -----------------------------------------------------------

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
        max_len = min(int(self.cfg.get("max_title_length", 24)), SessionDB.MAX_TITLE_LENGTH)
        # 显示列宽硬限（中文=2 列）：complete 风格放宽 12 列
        max_cols = int(self.cfg.get("max_display_width", 40))
        if self.cfg.get("title_style", "concise") == "complete":
            max_cols += 12
        return max_len, max_cols

    def _clean_title(self, title: str) -> str:
        """规范化模型输出：折叠空白、去包裹引号（可嵌套/前缀前后夹）、去
        'Title:'/'标题：' 前缀、去尾部句读；重复清洗直到稳定。

        覆盖 `Title: "Test 空转排查。"`、`"标题：Foo。"`、`「Title: "Foo"。」`
        等组合：先去外层引号再摘前缀、再去内层引号，直到无变化。
        """
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
        # 截断暴露在边界上的标点/引号也要剥掉（如 16 上限下
        # "abcdefghijklmno:xyz" → "abcdefghijklmno:" → 再去掉冒号）
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

    def _write(self, db: SessionDB, session_id: str, title: str) -> Optional[str]:
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
            # 每次实际写尝试前都重新核对权威性与写路径来源，不依赖进入 _write
            # 时的旧快照：LLM 调用期间/上一次尝试之后用户可能 /title（或出现
            # legacy NULL 标题）→ 优先保护，把「检查→写」窗口缩到最小。
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
            # llm → llm：set_auto_title 对同级是 no-op（上游刻意防自我重命名），
            # 只能走 set_session_title（临时记为 user）+ 恢复 llm 来源。恢复前
            # 重新读库：若用户的新标题恰好在我们写入之后抢先落地，就不恢复来源，
            # 保留用户标题与 user 权威。诚实边界：最后一次来源复核、
            # set_session_title、标题重读与来源恢复不是同一个公开原子 API；用户
            # 若恰好落在这些步骤之间，仍有极小竞态窗口。Hermes 目前没有公开的
            # llm→llm CAS 写法，因此这里只缩窗，不宣称绝对保护或原子更新。
            if db.set_session_title(session_id, t):
                try:
                    stored = db.get_session_title(session_id)
                except Exception:
                    stored = t
                if stored != t:
                    return False
                try:
                    db.set_session_title_source(session_id, SessionDB.TITLE_SOURCE_LLM)
                except Exception:
                    pass
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
        action = str(d.get("action", "keep")).lower()
        if action in ("keep", "approve", "rename"):
            title = str(d.get("title") or "").strip().strip('"').strip("'")
            return action, (title or None)
    return "keep", None
