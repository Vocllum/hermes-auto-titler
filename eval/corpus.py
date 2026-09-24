from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from hermes_auto_titler.messages import (
    clean_captured_text,
    is_summary,
    is_system_noise,
    message_text,
)


def count_human_turns(messages: List[Dict[str, Any]]) -> int:
    """计算会话中真实人类输入的轮次数。

    规则：
    1. 仅统计 role == 'user' 的消息；
    2. 经 clean_captured_text 清洗，过滤系统提示、subagent 回调、技能注入等纯系统噪声；
    3. 过滤纯压缩摘要载体；
    4. 相邻的重复用户输入（如重试、replay 副本）去重只计 1 次。
    """
    count = 0
    last_user_text: Optional[str] = None

    for m in messages:
        if m.get("role") != "user":
            continue
        raw_text = message_text(m.get("content"))
        cleaned = clean_captured_text(raw_text)
        if not cleaned:
            continue
        cleaned_stripped = cleaned.strip()
        if not cleaned_stripped or is_system_noise(cleaned_stripped) or is_summary(cleaned_stripped):
            continue
        # 相邻重复用户消息去重
        if last_user_text is not None and cleaned_stripped == last_user_text:
            continue
        last_user_text = cleaned_stripped
        count += 1

    return count


def select_eligible(
    db: Any,
    ids: List[str],
    *,
    min_human_turns: int = 8,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """从数据库中筛选符合评测标准的样本。

    门槛：
    1. 顶层会话：无父会话（parent_id 为空）；
    2. 初始来源证明：不能事后把 user 权威倒推为 llm；若当前为 user 且初始来源不明，排除；
    3. 真实人类轮次必须 >= min_human_turns。
    """
    eligible: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []

    for sid in ids:
        sess = None
        if hasattr(db, "get_session"):
            sess = db.get_session(sid)
        if not sess:
            excluded.append({"session_id": sid, "reason": "session_not_found"})
            continue

        # 检查是否为顶层会话
        parent_id = sess.get("parent_id") or sess.get("parent_session_id")
        if parent_id:
            excluded.append({"session_id": sid, "reason": "has_parent_session"})
            continue

        # 检查初始 provenance
        initial_source = sess.get("initial_source")
        title_source = sess.get("title_source")
        if title_source == "user" and (not initial_source or initial_source == "unknown"):
            excluded.append({"session_id": sid, "reason": "initial_provenance_unknown"})
            continue

        # 获取消息并计算真实人类轮次
        messages = []
        if hasattr(db, "get_messages"):
            messages = db.get_messages(sid) or []
        elif hasattr(db, "get_session_messages"):
            messages = db.get_session_messages(sid) or []

        turns = count_human_turns(messages)
        if turns < min_human_turns:
            excluded.append({
                "session_id": sid,
                "reason": "insufficient_human_turns",
                "human_turns": turns,
            })
            continue

        sess_copy = dict(sess)
        sess_copy["human_turns"] = turns
        eligible.append(sess_copy)

    return eligible, excluded
