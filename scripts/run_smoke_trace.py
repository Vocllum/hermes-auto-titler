#!/usr/bin/env python3
"""AutoTitler smoke trace diagnostic runner.

Supports two distinct, clearly separated evaluation modes:
1. Active-Window Diagnostic (mode="active_window", default):
   - Diagnoses AutoTitler state machine behavior over the uncompacted active window (messages WHERE active = 1).
   - Honestly acknowledges this is an active-window diagnostic and does NOT claim full session chronology
     or originating Turn 1 from inception.
   - Reads current session metadata from source DB as the diagnostic baseline for the window (respecting
     user-protected titles if present) unless explicit initial_title/source are provided.
2. Historical Replay (mode="historical_replay") is explicitly unavailable until
   compaction provenance, withdrawn-message exclusion, and initial title history
   can be reproduced safely in the sandbox.

Safety & Fidelity Guarantees:
- Per-cell sandbox isolation: each cell has its own sandbox directory and independent SessionDB, cleaned up on exit.
- Source DB protection: reads SQLite source connection read-only (mode=ro URI), never mutating the source.
- Equal baseline clock: each cell is initialized with its own independent clock starting at the exact same baseline,
  eliminating cross-cell clock drift or state leaks.
- Honest timing progression: clock advances by the actual message timestamp delta (delta_ts); no artificial 301s leaps
  are injected unless bypass_cooldown=True is explicitly requested.
- Safe offline default: FakeSmokeLLM runs completely offline without network or paid models, and sets usage=None
  to avoid fabricated token counts being labeled as "measured".
- Output protection: default CLI does not overwrite any historical trace (such as smoke-run-20260924.jsonl).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from typing import Any, Dict, List, Optional, Union
import urllib.request

repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root))

# Mock host environment exactly as in tests/conftest.py to isolate from hermes-agent CLI/PM updater
import tests.conftest  # noqa: F401

from eval.replay import ReplayClock, ReplayHarness, make_sandbox, slice_prefix
from eval.trace import TraceLLM

_SENTINEL = object()


class FakeSmokeLLM:
    """确定性离线模拟 LLM，用于在无真实网关或单测环境下驱动状态机转移。"""

    def __init__(self, responses: Optional[List[Dict[str, Any]]] = None) -> None:
        self.responses = list(responses) if responses is not None else [
            {"action": "rename", "title": "hermes-auto-titler 状态机评测"},
            {"action": "approve"},
        ]
        self._call_idx = 0
        self.calls: List[Dict[str, Any]] = []

    def complete(self, messages: List[Dict[str, Any]], **kwargs: Any) -> Any:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self._call_idx < len(self.responses):
            resp = self.responses[self._call_idx]
            self._call_idx += 1
        else:
            resp = {"action": "keep"}

        text = json.dumps(resp) if isinstance(resp, dict) else str(resp)

        class Response:
            def __init__(self, t: str, req_id: str) -> None:
                self.text = t
                self.id = req_id
                # 离线模拟器不伪造使用量；设为 None 避免被 TraceLLM 误标为 measured
                self.usage = None

        return Response(text, f"fake_req_{self._call_idx}")


class GatewayLLM:
    """网关 LLM 适配器，连接本地或受控代理网关。"""

    def __init__(self, base_url: str = "http://127.0.0.1:10100/v1", model: str = "Mercury") -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.provider = "opencodex"

    def complete(self, messages: List[Dict[str, Any]], **kwargs: Any) -> Any:
        payload = {
            "model": kwargs.get("model") or self.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", 0),
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        choice = data["choices"][0]
        text = choice["message"].get("content") or ""
        usage_data = data.get("usage") or {}

        class Usage:
            def __init__(self, u: Dict[str, Any]) -> None:
                self.input_tokens = u.get("prompt_tokens", 0)
                self.output_tokens = u.get("completion_tokens", 0)
                self.cache_read_tokens = u.get("prompt_tokens_details", {}).get("cached_tokens", 0)
                self.cache_write_tokens = 0
                self.cost_usd = None

        class Response:
            def __init__(self, t: str, req_id: str, u: Usage) -> None:
                self.text = t
                self.id = req_id
                self.usage = u

        return Response(text, data.get("id"), Usage(usage_data))


def run_smoke_trace(
    conn: Optional[Any] = None,
    session_id: str = "20260920_123615_4302a0",
    llm: Optional[Any] = None,
    out_file: Optional[Union[str, Path]] = None,
    cells: Optional[List[Dict[str, Any]]] = None,
    clock: Optional[ReplayClock] = None,
    probe_cooldown: bool = False,
    initial_title: Any = _SENTINEL,
    initial_title_source: Any = _SENTINEL,
    mode: str = "active_window",
    bypass_cooldown: bool = False,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """执行状态机 smoke 回放或视窗诊断。

    模式与来源保真约定：
    1. mode="active_window"（默认）：活跃视窗诊断。
       - 仅读取 active = 1 消息切片，明确不宣称其为全历史或从会话原点开始；
       - 若未显式传入 initial_title / initial_title_source，则以源库当前状态为视窗基线。
    2. mode="historical_replay"：尚未实现；直接拒绝运行，绝不产出不可信的全历史结果。

    时钟与隔离约定：
    - 每个 Cell 使用独立的 ReplayClock 实例并初始化到相同的基线时钟，杜绝跨 Cell 累积漂移；
    - 轮次间时钟依据消息实际时间戳差值 (delta_ts) 推进；仅在显式开启 bypass_cooldown=True 时强制跃迁 301s；
    - 源数据库始终只读挂载，沙箱完全运行于临时目录。
    """
    if mode not in ("active_window", "historical_replay"):
        raise ValueError(f"Unknown mode: {mode!r}. Expected 'active_window' or 'historical_replay'.")

    if mode == "historical_replay":
        raise NotImplementedError(
            "Historical replay is not implemented: the sandbox does not preserve "
            "compaction provenance or exclude inactive withdrawn rows. Use "
            "active_window for a limited diagnostic."
        )

    should_close_conn = False
    if conn is None:
        db_path = Path("/Users/Vocllum/.hermes/state.db")
        if not db_path.exists():
            raise FileNotFoundError(f"State DB not found at {db_path}")
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        should_close_conn = True

    try:
        # 读取会话事件
        if hasattr(conn, "get_messages"):
            all_events = conn.get_messages(session_id)
        else:
            c = conn.cursor()
            if mode == "historical_replay":
                c.execute(
                    "SELECT role, content, timestamp, active FROM messages WHERE session_id = ? ORDER BY id ASC",
                    (session_id,),
                )
            else:
                c.execute(
                    "SELECT role, content, timestamp, active FROM messages WHERE session_id = ? AND active = 1 ORDER BY id ASC",
                    (session_id,),
                )
            rows = c.fetchall()
            all_events = [
                {"role": r[0], "content": r[1], "timestamp": r[2], "active": r[3] if len(r) > 3 else 1}
                for r in rows
            ]

        if llm is None:
            llm = FakeSmokeLLM()

        if cells is None:
            cells = [
                {"cell_id": "baseline", "preview_chars": 1200, "turns": [1, 8]},
                {"cell_id": "summary_2400", "preview_chars": 2400, "turns": [1, 8]},
            ]

        captured: List[Dict[str, Any]] = []
        eval_records: List[Dict[str, Any]] = []
        trace = TraceLLM(llm, sink=captured.append)

        base_monotonic = clock.monotonic() if clock is not None else 1000.0
        base_time = clock.time() if clock is not None else 1789880000.0

        if out_file:
            out_path = Path(out_file)
            if out_path.exists() and not overwrite:
                raise FileExistsError(
                    f"Refusing to overwrite existing trace file at {out_path} without overwrite=True"
                )

        for cell in cells:
            cell_id = cell["cell_id"]
            preview_chars = cell.get("preview_chars", 1200)
            turns = cell.get("turns", [1, 8])
            rename_confirmations = cell.get("rename_confirmations", 1)

            # 每个 cell 拥有独立同一起点的时钟，消除跨 cell 时钟累积漂移
            cell_clock = ReplayClock(initial_monotonic=base_monotonic, initial_time=base_time)

            mode_desc = "Active-Window Diagnostic" if mode == "active_window" else "Historical Replay"
            print(f"=== Starting Cell: {cell_id} [{mode_desc}] (turns={turns}, preview={preview_chars}) ===")
            tmpdir = tempfile.mkdtemp(prefix=f"smoke_{cell_id}_")
            harness = None
            try:
                first_turn = turns[0] if turns else 1
                p_first = slice_prefix(all_events, first_turn)
                sandbox_kwargs: Dict[str, Any] = {
                    "mode": mode,
                }
                if initial_title is not _SENTINEL:
                    sandbox_kwargs["initial_title"] = initial_title
                if initial_title_source is not _SENTINEL:
                    sandbox_kwargs["initial_title_source"] = initial_title_source

                # 沙箱创建：严格受控
                sandbox_db = make_sandbox(conn, session_id, tmpdir, events=p_first, **sandbox_kwargs)
                cfg = {
                    "summary_preview_chars": preview_chars,
                    "rename_confirmations": rename_confirmations,
                    "min_interval_minutes": 5,
                    "model": "Mercury",
                    "provider": "opencodex",
                }
                harness = ReplayHarness(tmpdir, cfg=cfg, clock=cell_clock, mock_llm=trace, db=sandbox_db)

                prev_turn = None
                prev_ts = None

                for turn in turns:
                    turn_label = f"Active-Window Turn {turn}" if mode == "active_window" else f"Historical Turn {turn}"
                    print(f"--- Running {cell_id} {turn_label} ---")
                    p_turn = slice_prefix(all_events, turn)
                    curr_ts = p_turn[-1]["timestamp"] if (p_turn and p_turn[-1].get("timestamp") is not None) else cell_clock.time()

                    if prev_turn is not None:
                        # 追加轮次间新增的消息切片
                        p_prev = slice_prefix(all_events, prev_turn)
                        new_msgs = p_turn[len(p_prev):]
                        if new_msgs:
                            sandbox_db.append_messages_batch(session_id, new_msgs)

                        delta_ts = max(0.0, curr_ts - (prev_ts if prev_ts is not None else curr_ts))

                        if bypass_cooldown:
                            # 显式 bypass：人为向前推进至少 301 秒跨过冷却期
                            advance_needed = max(301.0, delta_ts)
                            cell_clock.advance(advance_needed)
                        else:
                            # 忠实时间推进：依据真实时间差值
                            if probe_cooldown:
                                if delta_ts > 5.0:
                                    cell_clock.advance(5.0)
                                    probe_res = harness.evaluate(session_id, force=False)
                                    probe_rec = {
                                        "cell_id": cell_id,
                                        "turn": turn,
                                        "mode": mode,
                                        "stage": "cooldown_probe",
                                        "action": probe_res.get("action"),
                                        "result": probe_res,
                                        "monotonic": cell_clock.monotonic(),
                                    }
                                    eval_records.append(probe_rec)
                                    print(f"Cooldown probe before Turn {turn}: {probe_res}")
                                    cell_clock.advance(delta_ts - 5.0)
                                else:
                                    cell_clock.advance(delta_ts)
                                    probe_res = harness.evaluate(session_id, force=False)
                                    probe_rec = {
                                        "cell_id": cell_id,
                                        "turn": turn,
                                        "mode": mode,
                                        "stage": "cooldown_probe",
                                        "action": probe_res.get("action"),
                                        "result": probe_res,
                                        "monotonic": cell_clock.monotonic(),
                                    }
                                    eval_records.append(probe_rec)
                            else:
                                cell_clock.advance(delta_ts)

                    trace.set_context(session_id=session_id, prefix=turn, config_id=cell_id)

                    res = harness.evaluate(session_id, force=False)
                    trans = harness.record_transition(session_id, res)
                    trans.update({
                        "cell_id": cell_id,
                        "turn": turn,
                        "mode": mode,
                        "stage": "turn_evaluation",
                    })
                    eval_records.append(trans)
                    print(
                        f"Result for {cell_id} Turn {turn}: action={trans.get('action')}, "
                        f"db_title={trans.get('db_title')!r}, pending={trans.get('pending')}"
                    )

                    prev_turn = turn
                    prev_ts = curr_ts
            finally:
                if harness:
                    harness.stop_clock_patch()
                    harness.close()
                shutil.rmtree(tmpdir, ignore_errors=True)

        if out_file:
            out_path = Path(out_file)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                for rec in captured:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"Written traces to {out_path}")

        return {
            "eval_records": eval_records,
            "captured_count": len(captured),
            "traces": captured,
        }
    finally:
        if should_close_conn and conn:
            try:
                conn.close()
            except Exception:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AutoTitler smoke trace diagnostic runner (active-window diagnostic or historical replay)"
    )
    parser.add_argument(
        "--mode",
        choices=["active_window", "historical_replay"],
        default="active_window",
        help="Evaluation mode: 'active_window' diagnostic (default); historical_replay is unavailable and fails closed.",
    )
    parser.add_argument(
        "--gateway",
        action="store_true",
        help="Use local OpenCodex Gateway (default is offline FakeSmokeLLM)",
    )
    parser.add_argument(
        "--session-id",
        default="20260920_123615_4302a0",
        help="Session ID to evaluate",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output JSONL trace path (default None; explicitly specify to write trace)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Explicitly permit overwriting an existing output file",
    )
    parser.add_argument(
        "--initial-title",
        default=None,
        help="Explicit initial title for session sandbox",
    )
    parser.add_argument(
        "--initial-title-source",
        default=None,
        help="Explicit initial title source ('user', 'llm', 'derived', or None)",
    )
    parser.add_argument(
        "--probe-cooldown",
        action="store_true",
        help="Probe cooldown throttling before turn evaluation",
    )
    parser.add_argument(
        "--bypass-cooldown",
        action="store_true",
        help="Bypass cooldown gate by artificially advancing clock 301s between turns",
    )
    args = parser.parse_args()

    if args.gateway:
        print("[INFO] Using GatewayLLM (http://127.0.0.1:10100/v1)")
        llm: Any = GatewayLLM()
    else:
        print("[INFO] Using FakeSmokeLLM (offline deterministic state machine driver; no network/paid calls)")
        llm = FakeSmokeLLM()

    init_title = args.initial_title if args.initial_title is not None else _SENTINEL
    init_source = args.initial_title_source if args.initial_title_source is not None else _SENTINEL

    results = run_smoke_trace(
        session_id=args.session_id,
        llm=llm,
        out_file=args.out,
        mode=args.mode,
        probe_cooldown=args.probe_cooldown,
        bypass_cooldown=args.bypass_cooldown,
        overwrite=args.overwrite,
        initial_title=init_title,
        initial_title_source=init_source,
    )
    print("\nSummary of Decisions & Transitions:")
    for r in results["eval_records"]:
        print(
            f"Cell {r.get('cell_id')} Turn {r.get('turn')} [{r.get('stage')}]: "
            f"action={r.get('action')}, db_title={r.get('db_title')!r}, pending={r.get('pending')}"
        )


if __name__ == "__main__":
    main()
