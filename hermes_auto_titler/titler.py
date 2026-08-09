"""核心：标题评估、模型生成、provenance 正确的写回。

来源规则（对齐 Hermes SessionDB）：
- 用户手改的标题（source=user）→ 永不覆盖
- 无标题 → set_auto_title(source=llm)
- 自动标题（llm/derived）→ 权威写入后恢复 llm 来源，保持可升级
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from hermes_state import SessionDB

from .messages import load_context

log = logging.getLogger(__name__)

_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["keep", "rename"]},
        "title": {"type": "string"},
    },
    "required": ["action"],
}

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

    def evaluate(self, session_id: str, force: bool = False) -> Dict[str, Any]:
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

        recent, all_user = load_context(
            db,
            session_id,
            int(self.cfg.get("recent_turns", 2)),
            bool(self.cfg.get("include_all_user_messages", True)),
        )
        if not recent:
            return {"action": "skipped", "reason": "no messages"}

        self._last_eval[session_id] = time.time()
        action, title = self._generate(current, recent, all_user)
        if action != "rename" or not title or title == current:
            return {"action": "keep"}
        written = self._write(db, session_id, title)
        if written:
            return {"action": "renamed", "title": written}
        return {"action": "failed"}

    # -- 模型生成 -----------------------------------------------------------

    def _generate(
        self,
        current: Optional[str],
        recent: List[Tuple[str, str]],
        all_user: List[Tuple[str, str]],
    ) -> Tuple[str, Optional[str]]:
        strategy = self.cfg.get("strategy", "conservative")
        if strategy == "aggressive":
            rule = "每次都给出最能概括当前会话的标题；只要与当前标题不同就 rename。"
        elif strategy == "balanced":
            rule = "当前标题已不准确或明显可以更好时 rename，小差异不必改。"
        else:
            rule = "只有当前标题明显无法概括会话内容时才 rename，否则 keep。"

        system = (
            "你是会话标题维护器，负责判断 Hermes 会话标题是否仍然准确。\n"
            "输出 JSON，格式：{\"action\": \"keep\" 或 \"rename\", \"title\": \"新标题\"}。\n"
            f"{rule}\n"
            "rename 时标题要求：3~8 个词的短语；具体、可检索（别人靠标题能找回这个会话）；"
            "避免「对话」「讨论」「问题」「查询」这类空泛词；语言跟随用户消息；"
            f"不超过 {int(self.cfg.get('max_title_length', 80))} 字符；不要引号。"
        )

        lines = [f"当前标题：{current or '（无）'}"]
        lines.append("")
        lines.append("最近对话：")
        for role, text in recent:
            lines.append(f"{role}: {text}")
        if all_user:
            lines.append("")
            lines.append("会话中的全部用户消息：")
            for _, text in all_user:
                lines.append(f"user: {text}")
        user_prompt = "\n".join(lines)

        try:
            from agent.plugin_llm import PluginLlmTextInput

            res = self.ctx.llm.complete_structured(
                instructions=user_prompt,
                input=[PluginLlmTextInput(text=user_prompt)],
                system_prompt=system,
                json_schema=_SCHEMA,
                schema_name="title_decision",
                model=self.cfg.get("model") or None,
                temperature=0.2,
                max_tokens=150,
                timeout=30,
                purpose="auto-title",
            )
            parsed = getattr(res, "parsed", None) or {}
        except Exception as e:
            log.warning("auto-titler LLM call failed: %s", e)
            return "keep", None

        action = str(parsed.get("action", "keep")).lower()
        title = str(parsed.get("title") or "").strip().strip('"').strip("'")
        if action == "rename" and title:
            return "rename", title
        return "keep", None

    # -- 写回 ---------------------------------------------------------------

    def _write(self, db: SessionDB, session_id: str, title: str) -> Optional[str]:
        max_len = min(int(self.cfg.get("max_title_length", 80)), SessionDB.MAX_TITLE_LENGTH)
        title = title.strip()
        if len(title) > max_len:
            title = title[:max_len].rstrip()
        if not title:
            return None

        try:
            src = db.get_session_title_source(session_id)
        except Exception:
            src = None

        def apply(t: str) -> bool:
            if src is None:
                return bool(db.set_auto_title(session_id, t, source=SessionDB.TITLE_SOURCE_LLM))
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
            if len(cand) > max_len:
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
                r = self.evaluate(sid, force=True)
                results.append({"session_id": sid, **r})
            except Exception as e:
                results.append({"session_id": sid, "action": "error", "reason": str(e)})
        return results
