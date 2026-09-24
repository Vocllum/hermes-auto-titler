from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from eval.replay import (
    is_real_session_db_available,
    make_sandbox,
    slice_prefix,
)


def test_slice_prefix_preserves_order_and_groups_assistant_replies():
    events = [
        {"role": "user", "content": "第一轮人类输入", "timestamp": 100.0},
        {"role": "assistant", "content": "助手思考与回复 1-1", "timestamp": 101.0},
        {"role": "assistant", "content": "助手补充回复 1-2", "timestamp": 102.0},
        {"role": "user", "content": "第二轮人类输入", "timestamp": 200.0},
        {"role": "assistant", "content": "助手回复 2", "timestamp": 201.0},
        {"role": "user", "content": "第三轮人类输入", "timestamp": 300.0},
        {"role": "assistant", "content": "助手回复 3", "timestamp": 301.0},
    ]

    # turn=1: 应该包含第 1 轮 user 及后续所有助手回复，但不包含第 2 轮
    prefix_1 = slice_prefix(events, turn=1)
    assert len(prefix_1) == 3
    assert [m["content"] for m in prefix_1] == [
        "第一轮人类输入",
        "助手思考与回复 1-1",
        "助手补充回复 1-2",
    ]
    # 保留原 role/content/timestamp/order
    assert prefix_1[0]["role"] == "user"
    assert prefix_1[0]["timestamp"] == 100.0
    assert prefix_1[1]["role"] == "assistant"
    assert prefix_1[1]["timestamp"] == 101.0

    # turn=2: 包含前两轮
    prefix_2 = slice_prefix(events, turn=2)
    assert len(prefix_2) == 5
    assert [m["content"] for m in prefix_2] == [
        "第一轮人类输入",
        "助手思考与回复 1-1",
        "助手补充回复 1-2",
        "第二轮人类输入",
        "助手回复 2",
    ]


def test_slice_prefix_subagent_and_noise_do_not_open_new_turn():
    events = [
        {"role": "user", "content": "开始分析系统日志", "timestamp": 100.0},
        {"role": "assistant", "content": "正在委派子代理排查...", "timestamp": 101.0},
        # 子代理汇总或系统通知，角色为 user 但属于系统噪声，不能作为新人类轮次
        {"role": "user", "content": "[async delegation batch completed: 2 tasks]", "timestamp": 102.0},
        {"role": "assistant", "content": "子代理返回完成，汇总结果。", "timestamp": 103.0},
        # 真实第二轮
        {"role": "user", "content": "请针对刚才的结果生成修复脚本", "timestamp": 200.0},
        {"role": "assistant", "content": "修复脚本如下...", "timestamp": 201.0},
    ]

    prefix_1 = slice_prefix(events, turn=1)
    # 子代理汇总包归入 turn 1，不提前截断，也不开辟新轮
    assert len(prefix_1) == 4
    assert prefix_1[2]["content"] == "[async delegation batch completed: 2 tasks]"
    assert prefix_1[3]["content"] == "子代理返回完成，汇总结果。"

    # turn 2 包含全部
    prefix_2 = slice_prefix(events, turn=2)
    assert len(prefix_2) == 6


def test_slice_prefix_compaction_summary_only_visible_at_real_timestamp():
    events = [
        {"role": "user", "content": "第一轮人类输入", "timestamp": 100.0},
        {"role": "assistant", "content": "回复 1", "timestamp": 101.0},
        {"role": "user", "content": "第二轮人类输入", "timestamp": 200.0},
        {"role": "assistant", "content": "回复 2", "timestamp": 201.0},
        # 在第 2 轮之后发生的压缩载体
        {
            "role": "user",
            "content": "[CONTEXT COMPACTION: Earlier conversation summary]\n前期排查了系统异常",
            "timestamp": 250.0,
        },
        {"role": "user", "content": "第三轮人类输入", "timestamp": 300.0},
        {"role": "assistant", "content": "回复 3", "timestamp": 301.0},
    ]

    # turn=1 切片绝不能看到未来（第 2 轮之后）的压缩载体
    prefix_1 = slice_prefix(events, turn=1)
    assert not any("[CONTEXT COMPACTION" in m["content"] for m in prefix_1)

    # turn=2 切片：压缩载体发生在 turn 2 与 turn 3 之间，归入 turn 2 之后可见范围
    prefix_2 = slice_prefix(events, turn=2)
    assert any("[CONTEXT COMPACTION" in m["content"] for m in prefix_2)
    assert prefix_2[-1]["content"].startswith("[CONTEXT COMPACTION")

    # turn=3 切片包含压缩载体以及第 3 轮
    prefix_3 = slice_prefix(events, turn=3)
    assert len(prefix_3) == 7


def test_slice_prefix_edge_cases():
    events = [
        {"role": "user", "content": "单一输入", "timestamp": 100.0},
        {"role": "assistant", "content": "单一回复", "timestamp": 101.0},
    ]
    # turn <= 0 -> 空列表
    assert slice_prefix(events, turn=0) == []
    assert slice_prefix(events, turn=-1) == []
    # turn 大于实际轮次 -> 返回全量
    assert len(slice_prefix(events, turn=10)) == 2


def test_make_sandbox_isolation_and_no_source_mutation(tmp_path):
    # 模拟生产源数据库
    source_db = MagicMock()
    source_session_data = {
        "id": "sess_prod_123",
        "source": "cli",
        "title": "生产既有标题",
        "title_source": "llm",
        "model": "gpt-4o",
        "parent_session_id": None,
    }
    source_messages = [
        {"role": "user", "content": "生产第 1 轮", "timestamp": 100.0},
        {"role": "assistant", "content": "生产回复 1", "timestamp": 101.0},
        {"role": "user", "content": "生产第 2 轮", "timestamp": 200.0},
        {"role": "assistant", "content": "生产回复 2", "timestamp": 201.0},
    ]

    source_db.get_session.return_value = dict(source_session_data)
    source_db.get_session_title.return_value = "生产既有标题"
    source_db.get_session_title_source.return_value = "llm"
    source_db.get_messages.return_value = list(source_messages)

    workdir = tmp_path / "sandbox_run_1"
    workdir.mkdir()

    # 制作切片并装载入沙箱
    prefix = slice_prefix(source_messages, turn=1)
    sandbox_db = make_sandbox(
        source_db,
        session_id="sess_prod_123",
        workdir=workdir,
        events=prefix,
    )

    # 验证沙箱独立性
    assert sandbox_db is not None
    # 沙箱 DB 文件必须落在 workdir 内部
    assert (workdir / "state.db").is_file()

    # 沙箱内会话属性与初始标题一致
    assert sandbox_db.get_session_title("sess_prod_123") == "生产既有标题"
    assert sandbox_db.get_session_title_source("sess_prod_123") == "llm"

    # 沙箱内的消息仅包含切片（第 1 轮 2 条），不含未来的第 2 轮
    conv = sandbox_db.get_messages_as_conversation("sess_prod_123")
    assert len(conv) == 2
    assert conv[0]["content"] == "生产第 1 轮"
    assert conv[1]["content"] == "生产回复 1"

    # 在沙箱中执行写回（改标题）
    sandbox_db.set_session_title("sess_prod_123", "沙箱新标题")
    sandbox_db.set_session_title_source("sess_prod_123", "derived")
    assert sandbox_db.get_session_title("sess_prod_123") == "沙箱新标题"

    # 验证源生产 DB 未被写穿或篡改
    source_db.set_session_title.assert_not_called()
    source_db.set_session_title_source.assert_not_called()
    assert source_db.get_session_title("sess_prod_123") == "生产既有标题"

    # 验证沙箱状态文件隔离
    state_file = workdir / "state.json"
    state_file.write_text(json.dumps({"sandbox_key": "val"}), encoding="utf-8")
    assert state_file.is_file()
    # 真实工作区根目录的 state.json 不变
    real_state = Path("/Users/Vocllum/.hermes/plugins/hermes-auto-titler/state.json")
    if real_state.exists():
        content = json.loads(real_state.read_text(encoding="utf-8"))
        assert "sandbox_key" not in content

    if hasattr(sandbox_db, "close"):
        sandbox_db.close()


@pytest.mark.skipif(
    os.environ.get("HERMES_EVAL_INTEGRATION") != "1",
    reason="Opt-in via HERMES_EVAL_INTEGRATION=1 for host SessionDB integration smoke",
)
def test_real_host_sessiondb_sandbox_isolation_smoke(tmp_path):
    """起真实子进程验证宿主 SessionDB 在独立沙箱中装载切片，无生产库写入，且 3 轮会话可见性按 1->2->3 展开。"""
    smoke_script = f"""
import sys
from pathlib import Path

# 加入宿主与插件路径
sys.path.insert(0, "/Users/Vocllum/.hermes/hermes-agent")
sys.path.insert(0, "{os.getcwd()}")

from hermes_state import SessionDB
from eval.replay import slice_prefix, make_sandbox

workdir = Path("{tmp_path}") / "integration_sandbox"
workdir.mkdir(parents=True, exist_ok=True)

# 1. 创建源真实 SessionDB (也在临时目录)
src_dir = Path("{tmp_path}") / "src_db"
src_dir.mkdir(parents=True, exist_ok=True)
src_db = SessionDB(db_path=src_dir / "state.db")
src_db.create_session("s_smoke", source="cli")
src_db.set_session_title("s_smoke", "Smoke Title")
src_db.set_session_title_source("s_smoke", "llm")

events = [
    {{"role": "user", "content": "人类轮次 1", "timestamp": 10.0}},
    {{"role": "assistant", "content": "助手回复 1", "timestamp": 11.0}},
    {{"role": "user", "content": "人类轮次 2", "timestamp": 20.0}},
    {{"role": "assistant", "content": "助手回复 2", "timestamp": 21.0}},
    {{"role": "user", "content": "人类轮次 3", "timestamp": 30.0}},
    {{"role": "assistant", "content": "助手回复 3", "timestamp": 31.0}},
]
src_db.append_messages_batch("s_smoke", events)

# 2. 验证逐轮切片并制作独立沙箱
for turn in (1, 2, 3):
    turn_dir = workdir / f"turn_{{turn}}"
    turn_dir.mkdir(parents=True, exist_ok=True)
    prefix = slice_prefix(events, turn=turn)
    s_db = make_sandbox(src_db, "s_smoke", workdir=turn_dir, events=prefix)
    conv = s_db.get_messages_as_conversation("s_smoke")
    user_msgs = [m for m in conv if m.get("role") == "user"]
    assert len(user_msgs) == turn, f"Turn {{turn}} expected {{turn}} user messages, got {{len(user_msgs)}}"
    s_db.close()

src_db.close()
print("SMOKE_PASS")
"""
    result = subprocess.run(
        ["/Users/Vocllum/.hermes/hermes-agent/venv/bin/python", "-c", smoke_script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"Host smoke failed with stderr: {result.stderr}"
    assert "SMOKE_PASS" in result.stdout
