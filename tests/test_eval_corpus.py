import pytest
from unittest.mock import MagicMock
from eval.corpus import count_human_turns, select_eligible


def test_count_human_turns_filters_noise_and_counts_real_turns():
    messages = [
        {"role": "user", "content": "[IMPORTANT: user invoked skill foo] do something"},
        {"role": "assistant", "content": "Sure, doing it."},
        {"role": "user", "content": "[System: the active model for this chat has changed]"},  # 无有效 payload
        {"role": "assistant", "content": "Model changed ack."},
        {"role": "user", "content": "[async delegation batch completed: 3 tasks]"},
        {"role": "assistant", "content": "Got subagents."},
        # 包含真实人话的中断包
        {
            "role": "user",
            "content": "[System note: your previous turn was interrupted mid-run] 请帮我重构这段代码",
        },
        {"role": "assistant", "content": "正在重构..."},
        # 同一用户连续 replay 重复消息只计一次
        {"role": "user", "content": "继续重构"},
        {"role": "user", "content": "继续重构"},
        {"role": "assistant", "content": "继续完毕。"},
    ]
    # [IMPORTANT...] 被 clean_captured_text 过滤或判定为噪声 -> 0
    # Model changed 无正文 -> 0
    # async delegation -> 0
    # interrupted mid-run 带正文 -> 1
    # 继续重构 (重复) -> 1
    # 总共 2 个真实人类轮次
    assert count_human_turns(messages) == 2


def test_select_eligible_gates_and_reasons():
    db = MagicMock()

    # 会话 1: 轮次不足 (<8)
    # 会话 2: 有父会话 (subagent/child)
    # 会话 3: 最终标题为 user 且初始 provenance 证据不足
    # 会话 4: 合格样本 (>=8 轮，无父会话，初始为可自动命名)

    def mock_get_session(sid):
        if sid == "sess_short":
            return {
                "id": sid,
                "parent_id": None,
                "title": "Short Session",
                "title_source": "llm",
                "initial_source": "llm",
            }
        elif sid == "sess_child":
            return {
                "id": sid,
                "parent_id": "sess_parent",
                "title": "Child Session",
                "title_source": "llm",
                "initial_source": "llm",
            }
        elif sid == "sess_user_title_unknown":
            return {
                "id": sid,
                "parent_id": None,
                "title": "Manual Title",
                "title_source": "user",
                # 无 initial provenance 证据
            }
        elif sid == "sess_eligible":
            return {
                "id": sid,
                "parent_id": None,
                "title": "Eligible Title",
                "title_source": "llm",
                "initial_source": "llm",
            }
        return None

    def mock_get_messages(sid):
        if sid == "sess_short":
            return [{"role": "user", "content": f"Turn {i}"} for i in range(3)]
        elif sid == "sess_child":
            return [{"role": "user", "content": f"Turn {i}"} for i in range(10)]
        elif sid == "sess_user_title_unknown":
            return [{"role": "user", "content": f"Turn {i}"} for i in range(10)]
        elif sid == "sess_eligible":
            return [{"role": "user", "content": f"Turn {i}"} for i in range(10)]
        return []

    db.get_session = mock_get_session
    db.get_messages = mock_get_messages

    eligible, excluded = select_eligible(
        db,
        ["sess_short", "sess_child", "sess_user_title_unknown", "sess_eligible"],
        min_human_turns=8,
    )

    eligible_ids = [s["id"] for s in eligible]
    assert eligible_ids == ["sess_eligible"]

    excluded_map = {item["session_id"]: item["reason"] for item in excluded}
    assert excluded_map["sess_short"] == "insufficient_human_turns"
    assert excluded_map["sess_child"] == "has_parent_session"
    assert excluded_map["sess_user_title_unknown"] == "initial_provenance_unknown"
