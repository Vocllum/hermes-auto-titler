#!/usr/bin/env python3
import os
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root))

# Mock host environment exactly as in tests/conftest.py to isolate from hermes-agent CLI/PM updater
import tests.conftest  # noqa: F401

import json
import shutil
import sqlite3
import tempfile
import urllib.request
from eval.replay import slice_prefix, make_sandbox, ReplayHarness
from eval.trace import TraceLLM


class GatewayLLM:
    def __init__(self, base_url="http://127.0.0.1:10100/v1", model="Mercury"):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.provider = "opencodex"

    def complete(self, messages, **kwargs):
        payload = {
            "model": kwargs.get("model") or self.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", 0),
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        choice = data["choices"][0]
        text = choice["message"].get("content") or ""
        usage_data = data.get("usage") or {}

        class Usage:
            def __init__(self, u):
                self.input_tokens = u.get("prompt_tokens", 0)
                self.output_tokens = u.get("completion_tokens", 0)
                self.cache_read_tokens = u.get("prompt_tokens_details", {}).get("cached_tokens", 0)
                self.cache_write_tokens = 0
                self.cost_usd = None

        class Response:
            def __init__(self, t, req_id, u):
                self.text = t
                self.id = req_id
                self.usage = Usage(u)

        return Response(text, data.get("id"), usage_data)


def main():
    conn = sqlite3.connect("/Users/Vocllum/.hermes/state.db")
    c = conn.cursor()
    sid = "20260920_123615_4302a0"
    c.execute(
        "SELECT role, content, timestamp FROM messages WHERE session_id = ? AND active = 1 ORDER BY id ASC",
        (sid,)
    )
    all_events = [{"role": r[0], "content": r[1], "timestamp": r[2]} for r in c.fetchall()]

    gw = GatewayLLM()
    captured = []
    eval_records = []

    out_file = repo_root / "eval" / "out" / "public" / "smoke-run-20260924.jsonl"
    out_file.parent.mkdir(parents=True, exist_ok=True)

    trace = TraceLLM(gw, sink=captured.append)

    runs = [
        ("baseline", 1, 1200),
        ("baseline", 8, 1200),
        ("summary_2400", 1, 2400),
        ("summary_2400", 8, 2400),
    ]

    for cell_id, turn, preview_chars in runs:
        print(f"--- Running {cell_id} Turn {turn} ---")
        tmpdir = tempfile.mkdtemp(prefix=f"smoke_{cell_id}_{turn}_")
        harness = None
        try:
            p = slice_prefix(all_events, turn)
            # make_sandbox creates a clean isolated SandboxSqliteSessionDB
            make_sandbox(conn, sid, tmpdir, events=p)
            cfg = {
                "summary_preview_chars": preview_chars,
                "rename_confirmations": 1,
                "model": "Mercury",
                "provider": "opencodex",
            }
            harness = ReplayHarness(tmpdir, cfg=cfg, mock_llm=trace)
            trace.set_context(session_id=sid, prefix=turn, config_id=cell_id)

            res = harness.evaluate(sid, force=True)
            print(f"Result for {cell_id} Turn {turn}: {res}")
            eval_records.append((cell_id, turn, res))
        finally:
            if harness:
                harness.stop_clock_patch()
            shutil.rmtree(tmpdir)

    print(f"\nTotal traces captured: {len(captured)}")
    with open(out_file, "w", encoding="utf-8") as f:
        for rec in captured:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"Written real gateway traces to {out_file}")

    print("\nSummary of Decisions:")
    for cell_id, turn, r in eval_records:
        print(f"Cell {cell_id} Turn {turn}: {r}")


if __name__ == "__main__":
    main()
