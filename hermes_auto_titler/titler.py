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

from .messages import load_context

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

        recent, all_user, opening = load_context(
            db,
            session_id,
            int(self.cfg.get("recent_turns", 2)),
            bool(self.cfg.get("include_all_user_messages", True)),
            int(self.cfg.get("opening_turns", 2)),
            bool(self.cfg.get("ignore_model_messages", False)),
            int(self.cfg.get("preview_chars", 200)),
        )
        if not recent:
            return {"action": "skipped", "reason": "no messages"}

        self._last_eval[session_id] = time.time()
        # derived 来源 + 超长标题（首条消息截断产物）视为低质量，强制重生成
        force_rename = src == SessionDB.TITLE_SOURCE_DERIVED and bool(current) and len(current) > 40
        action, title = self._generate(
            current, recent, all_user, opening,
            force_rename=force_rename, blind=blind,
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
    ) -> Tuple[str, Optional[str]]:
        strategy = self.cfg.get("strategy", "conservative")
        if blind:
            # retitle-all 盲改：不提供原标题，直接按内容重新命名
            if strategy == "aggressive":
                rule = ("不提供原标题。直接根据会话内容给出最能概括的新标题，"
                        "action 必须是 rename。优先反映最近对话的主题（用户最近在做什么），"
                        "其次才是开头主线。")
            else:
                rule = ("不提供原标题。直接根据会话内容给出最能概括的新标题，"
                        "action 必须是 rename。标题应概括会话的主要任务或主线，"
                        "而不是最新的一条小任务：如果会话开头确立了主题且之后围绕它展开，"
                        "优先用主线命名；只有会话确实转向了全新主题时才用最新主题命名。")
        elif strategy == "aggressive":
            rule = ("每次都给出最能概括当前会话的标题；只要与当前标题不同就 rename。"
                    "优先反映最近对话的主题（用户最近在做什么），其次才是开头主线。")
        else:
            rule = ("只有当前标题明显无法概括会话内容时才 rename，否则 keep。"
                    "标题应概括会话的主要任务或主线，而不是最新的一条小任务："
                    "如果会话开头确立了主题且之后围绕它展开（结合「会话开头」与「全部用户消息」判断），"
                    "优先用主线命名；只有会话确实转向了全新主题时才用最新主题命名。")
        if force_rename and not blind:
            rule += " 当前标题是自动截断的长文本，不合格，必须给出新的简洁标题（action 必须是 rename）。"

        system = (
            "你是会话标题维护器，负责判断 Hermes 会话标题是否仍然准确。\n"
            "输出 JSON，格式：{\"action\": \"keep\" 或 \"rename\", \"title\": \"新标题\"}。\n"
            f"{rule}\n"
            "rename 时标题要求：3~8 个词的短语；具体、可检索（别人靠标题能找回这个会话）；"
            "避免「对话」「讨论」「问题」「查询」这类空泛词；语言跟随用户消息；"
            f"不超过 {int(self.cfg.get('max_title_length', 80))} 字符；不要引号。\n"
            "超过 40 字符的标题视为冗长，即使语义仍相关也应建议更简洁的替代。"
        )

        lines = []
        if not blind:
            lines.append(f"当前标题：{current or '（无）'}")
            lines.append("")
        lines.append("会话开头：")
        for role, text in opening:
            lines.append(f"{role}: {text}")
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
            res = self.ctx.llm.complete(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                model=self.cfg.get("model") or None,
                temperature=0.2,
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
                r = self.evaluate(sid, force=True, blind=True)
                results.append({"session_id": sid, **r})
            except Exception as e:
                results.append({"session_id": sid, "action": "error", "reason": str(e)})
        return results


def _parse_decision(text: str) -> Tuple[str, Optional[str]]:
    """容错解析模型输出：JSON → 内嵌 JSON → 关键词启发式。"""
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
    # 启发式兜底
    if re.search(r"rename", text, re.IGNORECASE):
        m = re.search(r"(?:title|标题)[\"':：]\s*([^\n\"']+)", text, re.IGNORECASE)
        if m:
            return "rename", m.group(1).strip()
    return "keep", None
