from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, List
import pytest

from eval.replay import ReplayClock
from scripts.run_smoke_trace import FakeSmokeLLM, run_smoke_trace


@pytest.fixture
def sample_source_db(tmp_path: Path):
    """创建隔离的样本 SQLite 数据库，模拟生产 state.db。"""
    db_file = tmp_path / "source_state.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        """
        CREATE TABLE sessions (\n            id TEXT PRIMARY KEY,\n            source TEXT,\n            title TEXT,\n            title_source TEXT,\n            model TEXT,\n            parent_session_id TEXT\n        )\n        """
    )
    conn.execute(
        """
        CREATE TABLE messages (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            session_id TEXT,\n            role TEXT,\n            content TEXT,\n            timestamp REAL,\n            active INTEGER DEFAULT 1\n        )\n        """
    )

    sid = "sess_smoke_test"
    conn.execute(
        """
        INSERT INTO sessions (id, source, title, title_source, model)
        VALUES (?, 'desktop', '会话初始标题', 'llm', 'test-model')
        """,
        (sid,),
    )

    # 构造 10 轮交互，时间跨度较大（模拟真实多小时会话，每轮间隔 600 秒）
    base_ts = 1789880000.0
    for t in range(1, 11):
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?, 'user', ?, ?, 1)",
            (sid, f"用户轮次 {t}：详细讨论工程架构与实现方案", base_ts + (t - 1) * 600.0),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, active) VALUES (?, 'assistant', ?, ?, 1)",
            (sid, f"助手回复 {t}：架构分析与代码生成", base_ts + (t - 1) * 600.0 + 5.0),
        )

    conn.commit()
    yield conn, sid
    conn.close()


def test_smoke_trace_cell_sandbox_isolation_and_state_continuation(sample_source_db, tmp_path):
    conn, sid = sample_source_db

    # 自定义 FakeLLM 响应序列：
    # baseline Cell:
    #   Turn 1 -> rename 候选 "AutoTitler 架构测试"
    #   Turn 8 -> approve (背书确认候选)
    # summary_2400 Cell:
    #   Turn 1 -> keep
    #   Turn 8 -> keep
    llm = FakeSmokeLLM(
        responses=[
            {"action": "rename", "title": "AutoTitler 架构测试"},
            {"action": "approve"},
            {"action": "keep"},
            {"action": "keep"},
        ]
    )

    cells = [
        {"cell_id": "cell_baseline", "preview_chars": 1200, "turns": [1, 8]},
        {"cell_id": "cell_summary", "preview_chars": 2400, "turns": [1, 8]},
    ]
    out_file = tmp_path / "smoke_out.jsonl"
    clock = ReplayClock(initial_monotonic=1000.0, initial_time=1789880000.0)

    res = run_smoke_trace(
        conn=conn,
        session_id=sid,
        llm=llm,
        out_file=out_file,
        cells=cells,
        clock=clock,
        probe_cooldown=True,
    )

    eval_records = res["eval_records"]
    assert len(eval_records) == 6  # 每个 cell: turn 1 eval + turn 8 cooldown probe + turn 8 eval

    # 1. cell_baseline Turn 1: 继承源标题 '会话初始标题' (llm 来源)，进入 pending 待审
    r_base_t1 = eval_records[0]
    assert r_base_t1["cell_id"] == "cell_baseline"
    assert r_base_t1["turn"] == 1
    assert r_base_t1["stage"] == "turn_evaluation"
    assert r_base_t1["action"] == "pending"
    assert r_base_t1["candidate"] == "AutoTitler 架构测试"
    assert r_base_t1["db_title"] == "会话初始标题"
    assert r_base_t1["db_source"] == "llm"
    assert r_base_t1["monotonic"] == 1000.0  # 基线起点
    assert r_base_t1["pending"] == {
        "title": "AutoTitler 架构测试",
        "base_title": "会话初始标题",
    }

    # 2. cell_baseline Turn 8 冷却探针：间隔仅 5 秒，返回 throttled
    r_base_probe = eval_records[1]
    assert r_base_probe["cell_id"] == "cell_baseline"
    assert r_base_probe["stage"] == "cooldown_probe"
    assert r_base_probe["action"] == "throttled"
    assert r_base_probe["monotonic"] == 1005.0

    # 3. cell_baseline Turn 8 评估：推进实际时间差值（4200s）后正常评估，获得 approve，确认落库
    r_base_t8 = eval_records[2]
    assert r_base_t8["cell_id"] == "cell_baseline"
    assert r_base_t8["turn"] == 8
    assert r_base_t8["stage"] == "turn_evaluation"
    assert r_base_t8["action"] == "renamed"
    assert r_base_t8["db_title"] == "AutoTitler 架构测试"
    assert r_base_t8["db_source"] == "llm"
    assert r_base_t8["pending"] is None

    # 4. cell_summary Cell: 必须在独立干净沙箱中启动，初始标题仍为源库的 '会话初始标题'，且基线时钟重置为 1000.0
    r_summ_t1 = eval_records[3]
    assert r_summ_t1["cell_id"] == "cell_summary"
    assert r_summ_t1["turn"] == 1
    assert r_summ_t1["action"] == "keep"
    assert r_summ_t1["db_title"] == "会话初始标题"
    assert r_summ_t1["pending"] is None
    assert r_summ_t1["monotonic"] == 1000.0  # 验证独立 Cell 时钟起点一致，无漂移污染

    # 5. cell_summary Turn 8: 探针验证 + keep
    r_summ_probe = eval_records[4]
    assert r_summ_probe["action"] == "throttled"
    r_summ_t8 = eval_records[5]
    assert r_summ_t8["action"] == "keep"
    assert r_summ_t8["db_title"] == "会话初始标题"

    # 6. 源数据库只读保护：确认源库中标题和来源未被修改
    cur = conn.cursor()
    cur.execute("SELECT title, title_source FROM sessions WHERE id = ?", (sid,))
    source_row = cur.fetchone()
    assert source_row[0] == "会话初始标题"
    assert source_row[1] == "llm"

    # 7. JSONL 跟踪记录落地：验证离线 FakeSmokeLLM 不伪造 token 使用量
    assert out_file.exists()
    lines = [json.loads(line) for line in out_file.read_text(encoding="utf-8").strip().split("\n")]
    assert len(lines) == 4
    for entry in lines:
        assert entry["session_id"] == sid
        assert entry["status"] == "success"
        assert entry["token_source"] == "unavailable"  # 杜绝将离线 fake 标记为 measured
        assert entry["usage"] is None  # 杜绝硬编码 120/15
        assert "elapsed" in entry


def test_smoke_trace_untitled_session_flow(tmp_path):
    """测试从无标题会话开始的回放状态流转。"""
    db_file = tmp_path / "untitled_state.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            title_source TEXT,
            model TEXT,
            parent_session_id TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL,
            active INTEGER DEFAULT 1
        )
        """
    )
    sid = "sess_untitled"
    conn.execute(
        "INSERT INTO sessions (id, source, title, title_source) VALUES (?, 'cli', NULL, NULL)",
        (sid,),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', '全新会话需求', 100.0)",
        (sid,),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'assistant', '收到需求', 105.0)",
        (sid,),
    )
    conn.commit()

    llm = FakeSmokeLLM(
        responses=[
            {"action": "rename", "title": "首轮新标题"},
        ]
    )

    cells = [{"cell_id": "cell_untitled", "preview_chars": 1200, "turns": [1]}]
    res = run_smoke_trace(
        conn=conn,
        session_id=sid,
        llm=llm,
        cells=cells,
        probe_cooldown=False,
    )

    eval_records = res["eval_records"]
    assert len(eval_records) == 1
    r = eval_records[0]
    # 无标题会话首次命名直接生效（无需先 pending）
    assert r["action"] == "renamed"
    assert r["db_title"] == "首轮新标题"
    assert r["db_source"] == "llm"

    conn.close()


def test_historical_replay_fails_closed_even_with_explicit_initial_provenance(sample_source_db):
    conn, sid = sample_source_db
    with pytest.raises(NotImplementedError, match="withdrawn rows"):
        run_smoke_trace(conn=conn, session_id=sid, mode="historical_replay")
    with pytest.raises(NotImplementedError, match="withdrawn rows"):
        run_smoke_trace(
            conn=conn, session_id=sid, mode="historical_replay",
            initial_title="初始标题", initial_title_source="llm",
        )


def test_cooldown_not_artificially_bypassed_when_delta_is_small(tmp_path):
    """验证真实消息间隔小于 5 分钟 (300s) 时，诚实触发 throttled，不隐式注入 301s 跃迁。"""
    db_file = tmp_path / "short_interval_state.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, title_source TEXT)")
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, timestamp REAL, active INTEGER DEFAULT 1)"
    )

    sid = "sess_short_interval"
    conn.execute("INSERT INTO sessions VALUES (?, 'cli', '既有标题', 'llm')", (sid,))
    # 两轮消息相隔仅 60 秒（远小于 5 分钟）
    conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', '第一轮输入', 100.0)", (sid,))
    conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'assistant', '第一轮回复', 105.0)", (sid,))
    conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', '第二轮输入', 160.0)", (sid,))
    conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'assistant', '第二轮回复', 165.0)", (sid,))
    conn.commit()

    llm = FakeSmokeLLM(responses=[{"action": "rename", "title": "首轮尝试"}, {"action": "rename", "title": "次轮尝试"}])
    cells = [{"cell_id": "cell_timing", "preview_chars": 1200, "turns": [1, 2]}]

    # 1. 默认 bypass_cooldown=False：第二轮应真实返回 throttled
    res = run_smoke_trace(
        conn=conn,
        session_id=sid,
        llm=llm,
        cells=cells,
        probe_cooldown=False,
        bypass_cooldown=False,
    )
    records = res["eval_records"]
    assert len(records) == 2
    assert records[0]["turn"] == 1
    assert records[0]["action"] == "pending"
    assert records[1]["turn"] == 2
    assert records[1]["action"] == "throttled"  # 60s 间隔被诚实拦截

    # 2. 显式 bypass_cooldown=True：人为推进 301s 跨过门禁
    llm2 = FakeSmokeLLM(responses=[{"action": "rename", "title": "首轮尝试"}, {"action": "approve"}])
    res2 = run_smoke_trace(
        conn=conn,
        session_id=sid,
        llm=llm2,
        cells=cells,
        probe_cooldown=False,
        bypass_cooldown=True,
    )
    records2 = res2["eval_records"]
    assert len(records2) == 2
    assert records2[1]["turn"] == 2
    assert records2[1]["action"] == "renamed"  # 显式 bypass 后评估通过

    conn.close()


def test_cells_independent_equal_baseline_clocks(sample_source_db):
    """验证多个 Cell 各自获得独立的基线时钟，Cell 1 的推进绝不会污染 Cell 2 的初始时间。"""
    conn, sid = sample_source_db
    llm = FakeSmokeLLM()

    cells = [
        {"cell_id": "c1", "preview_chars": 1200, "turns": [1, 8]},
        {"cell_id": "c2", "preview_chars": 2400, "turns": [1]},
    ]
    clock = ReplayClock(initial_monotonic=5000.0, initial_time=1700000000.0)

    res = run_smoke_trace(
        conn=conn,
        session_id=sid,
        llm=llm,
        cells=cells,
        clock=clock,
        probe_cooldown=False,
    )
    records = res["eval_records"]
    # c1 Turn 1, c1 Turn 8, c2 Turn 1
    assert len(records) == 3
    assert records[0]["cell_id"] == "c1"
    assert records[0]["turn"] == 1
    assert records[0]["monotonic"] == 5000.0

    assert records[1]["cell_id"] == "c1"
    assert records[1]["turn"] == 8
    assert records[1]["monotonic"] > 5000.0

    # c2 的 Turn 1 必须仍然处于 5000.0 起点
    assert records[2]["cell_id"] == "c2"
    assert records[2]["turn"] == 1
    assert records[2]["monotonic"] == 5000.0


def test_out_file_overwrite_protection(sample_source_db, tmp_path):
    """验证对既有跟踪文件的覆盖保护，防止误删或覆盖历史基线（如 smoke-run-20260924.jsonl）。"""
    conn, sid = sample_source_db
    llm = FakeSmokeLLM()
    cells = [{"cell_id": "c1", "preview_chars": 1200, "turns": [1]}]

    existing_file = tmp_path / "historical_trace.jsonl"
    existing_file.write_text("{\"historical\": \"data\"}\n", encoding="utf-8")

    # 1. 默认 overwrite=False -> 抛出 FileExistsError
    with pytest.raises(FileExistsError, match="Refusing to overwrite existing trace file"):
        run_smoke_trace(
            conn=conn,
            session_id=sid,
            llm=llm,
            cells=cells,
            out_file=existing_file,
            overwrite=False,
        )

    # 原历史文件内容完好无损
    assert existing_file.read_text(encoding="utf-8") == "{\"historical\": \"data\"}\n"

    # 2. 显式 overwrite=True -> 允许覆盖
    res = run_smoke_trace(
        conn=conn,
        session_id=sid,
        llm=llm,
        cells=cells,
        out_file=existing_file,
        overwrite=True,
    )
    assert res["captured_count"] == 1
    assert "historical" not in existing_file.read_text(encoding="utf-8")
