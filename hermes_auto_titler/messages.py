"""从 Hermes SessionDB 提取用于标题评估的对话上下文。

只取纯文本：附件（图片/文件）只保留占位符，不展开内容，控制成本。
"""

from __future__ import annotations

from typing import Any, List, Tuple


def _part_text(part: dict) -> str:
    t = part.get("type", "")
    if t == "text":
        return str(part.get("text", ""))
    if t == "image_url":
        return "[图片]"
    if t in ("file", "input_file"):
        name = part.get("name") or part.get("file_name") or ""
        return f"[文件: {name}]" if name else "[文件]"
    return "[附件]"


def message_text(content: Any) -> str:
    """OpenAI 格式 content（str 或 parts list）→ 纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_part_text(p) for p in content if isinstance(p, dict))
    if isinstance(content, dict):
        return _part_text(content)
    return ""


def load_context(
    db,
    session_id: str,
    recent_turns: int,
    include_all_user: bool,
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """返回 (最近 N 轮 user/assistant 对, 全部用户消息)，均为 (role, text)。"""
    conv = db.get_messages_as_conversation(session_id, include_ancestors=True) or []
    pairs: List[Tuple[str, str]] = []
    for m in conv:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        text = message_text(m.get("content")).strip()
        if text:
            pairs.append((role, text))

    recent: List[Tuple[str, str]] = []
    seen_user = 0
    for role, text in reversed(pairs):
        recent.append((role, text))
        if role == "user":
            seen_user += 1
            if seen_user >= recent_turns:
                break
    recent.reverse()

    all_user = [(r, t) for r, t in pairs if r == "user"]
    return recent, (all_user if include_all_user else [])
