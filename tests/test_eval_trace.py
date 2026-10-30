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


def test_extract_section_lengths_indexed_sampled_history():
    prompt = (
        "Conversation contains 12 user turns.\n\n"
        "Opening:\n"
        "User turn 1 of 12: 最初目标\n\n"
        "Sampled history:\n"
        "User turn 6 of 12: 中段进展\n\n"
        "Compaction summary (secondary evidence):\n"
        "中段压缩主题\n\n"
        "Recent:\n"
        "User turn 12 of 12: 当前阶段\n\n"
        "Fallback historical summary:\n"
        "旧宿主摘要\n\n"
        "Current title: 标题"
    )
    secs = extract_section_lengths(prompt)
    assert any(k.startswith("Opening:") for k in secs)
    assert any(k.startswith("Sampled history:") for k in secs)
    assert any(k.startswith("Recent:") for k in secs)
    assert any(k.startswith("Compaction summary (secondary evidence):") for k in secs)
    assert any(k.startswith("Fallback historical summary:") for k in secs)


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


def test_trace_normalizes_openai_dict_usage_instead_of_fabricating_zero():
    from types import SimpleNamespace

    sink = []
    inner = MagicMock(model="requested")
    inner.complete.return_value = SimpleNamespace(
        text='{"action":"keep"}', model="returned", usage={
            "prompt_tokens": 123, "completion_tokens": 45,
            "completion_tokens_details": {"reasoning_tokens": 12},
            "prompt_tokens_details": {"cached_tokens": 80},
        },
    )
    TraceLLM(inner, sink).complete([])
    assert sink[0]["usage"]["input_tokens"] == 123
    assert sink[0]["usage"]["output_tokens"] == 45
    assert sink[0]["usage"]["reasoning_tokens"] == 12
    assert sink[0]["usage"]["cache_read_tokens"] == 80
    assert sink[0]["usage"]["cache_write_tokens"] is None
    assert sink[0]["response_model"] == "returned"


def test_empty_usage_is_unavailable_and_partial_usage_stays_partial():
    from types import SimpleNamespace

    for usage, source in (({}, "unavailable"), ({"prompt_tokens": 20}, "partial")):
        sink = []
        inner = MagicMock()
        inner.complete.return_value = SimpleNamespace(text="{}", usage=usage)
        TraceLLM(inner, sink).complete([])
        assert sink[0]["token_source"] == source
        if usage:
            assert sink[0]["usage"]["input_tokens"] == 20
            assert sink[0]["usage"]["output_tokens"] is None
        else:
            assert sink[0]["usage"] is None


def test_native_gemini_usage_and_nonfinite_values_are_normalized():
    from eval.trace import normalize_usage

    usage = normalize_usage({"promptTokenCount": 10, "candidatesTokenCount": 5,
                             "thoughtsTokenCount": 3, "totalTokenCount": 18})
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 5
    assert usage["reasoning_tokens"] == 3
    assert usage["total_tokens"] == 18
    assert normalize_usage({"input_tokens": float("nan"), "output_tokens": -1,
                            "cost_usd": float("inf")}) is None


def test_missing_mock_metadata_is_nullable_and_json_serializable():
    sink = []
    inner = MagicMock()
    inner.complete.return_value.text = '{"action":"keep"}'
    inner.complete.return_value.usage = {}
    TraceLLM(inner, sink).complete([])
    json.dumps(sink[0])
    assert sink[0]["response_model"] is None
    assert sink[0]["response_provider"] is None
    assert sink[0]["request_id"] is None
    assert sink[0]["model_route"]["model"] is None


def test_openai_prompt_details_cache_write_is_captured_without_backfilling_missing():
    from eval.trace import normalize_usage

    usage = normalize_usage({
        "prompt_tokens": 12, "completion_tokens": 3,
        "prompt_tokens_details": {"cached_tokens": 4, "cache_write_tokens": 8},
    })
    assert usage["cache_read_tokens"] == 4
    assert usage["cache_write_tokens"] == 8
    assert normalize_usage({"prompt_tokens": 12, "completion_tokens": 3})["cache_write_tokens"] is None


def test_broken_usage_accessor_preserves_response_and_partial_success_ledger():
    from types import SimpleNamespace

    class BrokenUsage:
        output_tokens = 3

        @property
        def input_tokens(self):
            raise RuntimeError("usage property unavailable")

    response = SimpleNamespace(text='{"action":"keep"}', usage=BrokenUsage())
    inner = MagicMock()
    inner.complete.return_value = response
    sink = []
    assert TraceLLM(inner, sink).complete([]) is response
    assert len(sink) == 1 and sink[0]["status"] == "success"
    assert sink[0]["token_source"] == "partial"
    assert sink[0]["usage"]["input_tokens"] is None
    assert sink[0]["usage"]["output_tokens"] == 3


def test_giant_usage_integers_do_not_overflow_or_mask_successful_response():
    from types import SimpleNamespace

    response = SimpleNamespace(text='{"action":"keep"}', usage={
        "input_tokens": 10 ** 1000, "output_tokens": 3, "cost_usd": 10 ** 1000,
    })
    inner = MagicMock()
    inner.complete.return_value = response
    sink = []
    assert TraceLLM(inner, sink).complete([]) is response
    assert sink[0]["status"] == "success" and sink[0]["token_source"] == "partial"
    assert sink[0]["usage"]["input_tokens"] is None
    assert sink[0]["usage"]["output_tokens"] == 3
    assert sink[0]["usage"]["cost_usd"] is None
    json.dumps(sink[0])


def test_broken_response_usage_and_metadata_properties_are_fail_soft():
    class BrokenResponse:
        text = '{"action":"keep"}'

        def __getattr__(self, name):
            raise RuntimeError("optional response metadata unavailable")

    response = BrokenResponse()
    inner = MagicMock()
    inner.complete.return_value = response
    sink = []
    assert TraceLLM(inner, sink).complete([]) is response
    assert sink[0]["status"] == "success" and sink[0]["usage"] is None
    assert sink[0]["token_source"] == "unavailable"
    assert sink[0]["response_model"] is sink[0]["response_provider"] is sink[0]["request_id"] is None


def test_unexpected_normalization_failure_still_emits_success_ledger(monkeypatch):
    from types import SimpleNamespace

    def broken_normalizer(usage):
        raise RuntimeError("unexpected normalization defect")

    monkeypatch.setattr("eval.trace.normalize_usage", broken_normalizer)
    response = SimpleNamespace(text='{"action":"keep"}', usage={"input_tokens": 12})
    inner = MagicMock()
    inner.complete.return_value = response
    sink = []
    assert TraceLLM(inner, sink).complete([]) is response
    assert sink[0]["status"] == "success" and sink[0]["token_source"] == "unavailable"
    assert sink[0]["usage"] is None
