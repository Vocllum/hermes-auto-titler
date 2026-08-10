"""核心：标题评估、模型生成、provenance 正确的写回。

来源规则（对齐 Hermes SessionDB）：
- 用户手改的标题（source=user）→ 永不覆盖
- 无标题 → set_auto_title(source=llm)
- 自动标题（llm/derived）→ 权威写入后恢复 llm 来源，保持可升级
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from hermes_state import SessionDB

from .messages import display_width, load_context_with_summary, truncate_to_width

log = logging.getLogger(__name__)

_db: Optional[SessionDB] = None


def get_db() -> SessionDB:
    global _db
    if _db is None:
        _db = SessionDB()
    return _db


class AutoTitler:
    def __init__(self, ctx, cfg: dict[str, Any], db: SessionDB | None = None):
        self.ctx = ctx
        self.cfg = cfg
        self._db = db
        self._turns: Dict[str, int] = {}
        self._last_eval: Dict[str, float] = {}
        self._current_session: Optional[str] = None

    @property
    def db(self) -> SessionDB:
        return self._db or get_db()

    # -- hook 入口 ----------------------------------------------------------

    def on_session_end(self, **payload: Any) -> None:
        if not self.cfg.get("enabled", True):
            return
        session_id = payload.get("session_id") or ""
        if not session_id:
            return
        self._current_session = session_id
        # 会话结束信号（gateway 关闭 / CLI 退出 safety net）
        if self.cfg.get("on_close") and (payload.get("reason") or payload.get("interrupted")):
            try:
                self.evaluate(session_id, force=True)
            except Exception as e:
                log.warning("auto-titler on_close evaluate failed: %s", e)
            return
        n = self._turns.get(session_id, 0) + 1
        self._turns[session_id] = n
        every = int(self.cfg.get("every_n_turns", 3))
        if n % every == 0:
            try:
                self.evaluate(session_id)
            except Exception as e:
                log.warning("auto-titler evaluate failed: %s", e)

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
            int(self.cfg.get("preview_chars", 200)),
            int(self.cfg.get("user_message_threshold", 20)),
            int(self.cfg.get("user_message_preview_chars", 200)),
        )
        if not recent:
            return {"action": "skipped", "reason": "no messages"}

        self._last_eval[session_id] = time.time()
        # derived 来源 + 超长标题（首条消息截断产物）视为低质量，强制重生成
        force_rename = src == SessionDB.TITLE_SOURCE_DERIVED and bool(current) and len(current) > 40
        action, title = self._generate(
            current, recent, all_user, opening,
            force_rename=force_rename, blind=blind,
            earlier_summary=earlier_summary,
        )
        if action != "rename" or not title or title == current:
            log.info("auto-titler %s: keep (current=%r)", session_id[:12], current)
            return {"action": "keep"}
        written = self._write(db, session_id, title)
        if written:
            log.info("auto-titler %s: renamed -> %r", session_id[:12], written)
            return {"action": "renamed", "title": written}
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
        earlier_summary: Optional[str] = None,
    ) -> Tuple[str, Optional[str]]:
        strategy = self.cfg.get("strategy", "conservative")
        if blind:
            # retitle-all 盲改：不提供原标题，直接按内容重新命名
            rule = (
                "Generate the best title directly from the conversation "
                "content (the current title is NOT provided); action MUST "
                "be rename."
            )
        elif strategy == "aggressive":
            rule = (
                "Rename whenever your title is better. "
                "The opening main task always outranks the latest subtask."
            )
        else:
            rule = (
                "Rename only when the current title no longer summarizes "
                "the conversation as a whole."
            )
        if force_rename and not blind:
            rule += (
                " The current title is a truncated auto-generated string; "
                "action MUST be rename."
            )

        # 标题风格：concise = 主体标签（Subject + 最小区分意图）/ complete =
        # 简短事件梗概（Subject + 主要意图/事件）。信息类型是第一约束，
        # 长度只是护栏（12 为目标、max_title_length 为硬上限）。
        max_title_len = int(self.cfg.get("max_title_length", 16))
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
        system = (
            "You maintain concise titles for Hermes conversations.\n"
            "Return JSON only:\n"
            '{"action":"keep"|"rename","title":"..."}\n'
            f"\n{rule}\n"
            f"\n{style_req}\n"
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
            "\nRules:\n"
            f"- Length is a guardrail, not the goal: aim for at most 12 "
            f"characters; never exceed {max_title_len} (Chinese and Latin "
            "each count as 1 character).\n"
            "- Keep key product names and identifiers exact and correctly "
            "cased; expand informal abbreviations from the user's messages "
            "instead of copying them: ov -> OpenViking, skill -> Skill, "
            "Codex, Hermes, DeepSeek.\n"
            "- When the conversation has two distinct tasks, name both "
            "entities even if it uses the full length budget.\n"
            "- No trailing punctuation, no quotes.\n"
            "- Use the dominant language of the user's messages.\n"
            'Good: {"action":"rename","title":"Dia密码导入Apple密码"}\n'
            'Too narrow: {"action":"rename","title":"关闭验证注入"}\n'
            'Too vague: {"action":"rename","title":"Code changes"}\n'
            'Reply with JSON only.'
        )

        lines = []
        if not blind:
            lines.append(f"Current title: {current or '(none)'}")
            lines.append("")
        lines.append(
            "Opening (the session's starting turns; the main through-line "
            "anchor; primary subject source):"
        )
        for role, text in opening:
            lines.append(f"{role}: {text}")
        if earlier_summary:
            lines.append("")
            lines.append(
                "Earlier history summary (weak hint; may contain stale subtask details. "
                "Use it only to recover the broad earlier topic when the original "
                "opening is unavailable):"
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
                temperature=0,
                max_tokens=150,
                timeout=30,
                purpose="auto-title",
            )
            text = getattr(res, "text", "") or ""
        except Exception as e:
            log.warning("auto-titler LLM call failed: %s", e)
            return "keep", None

        action, title = _parse_decision(text)
        if action == "rename" and title:
            return "rename", title
        return "keep", None

    # -- 写回 ---------------------------------------------------------------

    def _write(self, db: SessionDB, session_id: str, title: str) -> Optional[str]:
        max_len = min(int(self.cfg.get("max_title_length", 80)), SessionDB.MAX_TITLE_LENGTH)
        # 显示列宽硬限（中文=2 列）：complete 风格放宽 12 列
        max_cols = int(self.cfg.get("max_display_width", 40))
        if self.cfg.get("title_style", "concise") == "complete":
            max_cols += 12
        title = truncate_to_width(title.strip(), max_cols)
        if len(title) > max_len:
            title = title[:max_len].rstrip()
        if not title:
            return None

        try:
            src = db.get_session_title_source(session_id)
        except Exception:
            src = None

        def apply(t: str) -> bool:
            # 无标题 / derived → llm：set_auto_title 是单事务 CAS（title + source
            # 一起写），原子，无 crash window。
            if src is None or src == SessionDB.TITLE_SOURCE_DERIVED:
                return bool(
                    db.set_auto_title(session_id, t, source=SessionDB.TITLE_SOURCE_LLM)
                )
            # llm → llm：set_auto_title 对同级是 no-op（上游刻意防自我重命名），
            # 只能走 set_session_title（user 级，必落盘）+ 立刻恢复 llm 来源。
            # 两步之间是毫秒级窗口；若进程恰在此刻崩溃，标题会停在 user 来源、
            # 插件从此不再碰它——Hermes 公开 API 无原子覆盖同级的手段，接受。
            if db.set_session_title(session_id, t):
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
            cand = f"{title} ({i})"
            if display_width(cand) > max_cols or len(cand) > max_len:
                break
            try:
                if apply(cand):
                    return cand
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
                r = self.evaluate(sid, force=True, blind=True)
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
        if action in ("keep", "rename"):
            title = str(d.get("title") or "").strip().strip('"').strip("'")
            return action, (title or None)
    return "keep", None
