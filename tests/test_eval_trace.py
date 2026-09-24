from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from eval.trace import TraceLLM, classify_error, extract_section_lengths


def test_classify_error():
    assert classify_error(TimeoutError("request timed out")) == "timeout"
    assert classify_error(Exception("429 Too Many Requests")) == "rate_limit"
    assert classify_error(Exception("401 Unauthorized token")) == "auth"
    assert classify_error(Exception("model not found: gpt-fake")) == "route"
    assert classify_error(json.JSONDecodeError("Expecting value", "doc", 0)) == "parse"
    assert classify_error(RuntimeError("something unexpected")) == "other"


def test_extract_section_lengths():
    prompt = (
        "Opening context / 开头内容 (identify the durable subject):\n"
        "user: 我想实现一个评测流水线\n\n"
        "Recent context (current state / real topic shift evidence):\n"
        "user: 现在需要接入 TraceLLM\n"
        "assistant: 好的\n\n"
        "Current title: 评测流水线实现"
    )
    secs = extract_section_lengths(prompt)
    assert any("Opening context" in k for k in secs)
    assert all(isinstance(v, int) and v > 0 for v in secs.values())


def test_trace_llm_success_and_sink(tmp_path):
    sink = []
    private_dir = tmp_path / "private"

    mock_inner = MagicMock()
    mock_res = MagicMock()
    mock_res.text = '{"action": "keep"}'
    mock_res.id = "req_12345"
    mock_usage = MagicMock()
    mock_usage.input_tokens = 120
    mock_usage.output_tokens = 15
    mock_usage.cache_read_tokens = 0
    mock_usage.cache_write_tokens = 0
    mock_usage.cost_usd = 0.0002
    mock_res.usage = mock_usage
    mock_inner.complete.return_value = mock_res

    tracer = TraceLLM(mock_inner, sink, private_dir=private_dir)
    tracer.set_context(session_id="sess_abc", prefix=2, config_id="baseline")

    messages = [
        {"role": "system", "content": "You are AutoTitler."},
        {
            "role": "user",
            "content": (
                "Opening context / 开头内容 (identify the durable subject):\n"
                "user: 需求 1\n\n"
                "Current title: (none)"
            ),
        },
    ]

    res = tracer.complete(
        messages=messages,
        model="gpt-4o-mini",
        provider="custom",
        temperature=0,
    )

    # 1. Exact return transparent
    assert res is mock_res
    mock_inner.complete.assert_called_once()

    # 2. Sink record verification
    assert len(sink) == 1
    rec = sink[0]
    assert rec["session_id"] == "sess_abc"
    assert rec["prefix"] == 2
    assert rec["config_id"] == "baseline"
    assert rec["status"] == "success"
    assert rec["token_source"] == "measured"
    assert rec["usage"]["input_tokens"] == 120
    assert rec["usage"]["output_tokens"] == 15
    assert rec["request_id"] == "req_12345"
    assert rec["system_prompt_hash"] is not None
    assert rec["user_prompt_hash"] is not None
    assert isinstance(rec["section_lengths"], dict)
    assert rec["model_route"]["model"] == "gpt-4o-mini"
    assert rec["model_route"]["provider"] == "custom"
    # No secret or raw prompt text in public sink record
    rec_str = json.dumps(rec)
    assert "You are AutoTitler" not in rec_str
    assert "需求 1" not in rec_str

    # 3. Private raw dump file verification
    private_files = list(private_dir.glob("*.json"))
    assert len(private_files) == 1
    # Check permissions 0600
    file_stat = os.stat(private_files[0])
    assert stat.S_IMODE(file_stat.st_mode) & 0o777 == 0o600
    private_content = json.loads(private_files[0].read_text(encoding="utf-8"))
    assert private_content["messages"][0]["content"] == "You are AutoTitler."


def test_trace_llm_missing_usage():
    sink = []
    mock_inner = MagicMock()
    mock_res = MagicMock()
    mock_res.text = '{"action": "keep"}'
    mock_res.usage = None  # No usage
    mock_inner.complete.return_value = mock_res

    tracer = TraceLLM(mock_inner, sink)
    messages = [
        {"role": "system", "content": "Sys"},
        {"role": "user", "content": "User"},
    ]
    tracer.complete(messages=messages)

    assert len(sink) == 1
    rec = sink[0]
    assert rec["usage"] is None
    assert rec["token_source"] == "unavailable"


def test_trace_llm_exception_classification_and_reraise():
    sink = []
    mock_inner = MagicMock()
    mock_inner.complete.side_effect = TimeoutError("gateway timeout")

    tracer = TraceLLM(mock_inner, sink)
    messages = [
        {"role": "system", "content": "Sys"},
        {"role": "user", "content": "User"},
    ]

    with pytest.raises(TimeoutError):
        tracer.complete(messages=messages)

    assert len(sink) == 1
    rec = sink[0]
    assert rec["status"] == "error"
    assert rec["error_type"] == "timeout"
