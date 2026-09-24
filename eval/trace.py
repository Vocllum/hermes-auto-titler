from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union


def classify_error(exc: Exception) -> str:
    """将模型调用过程中的异常分类为受控类型：route/auth/rate_limit/timeout/parse/other。"""
    if isinstance(exc, (TimeoutError,)):
        return "timeout"
    if isinstance(exc, (json.JSONDecodeError,)):
        return "parse"

    msg = str(exc).lower()
    if any(k in msg for k in ("timeout", "timed out", "deadline")):
        return "timeout"
    if any(k in msg for k in ("429", "rate limit", "rate_limit", "quota", "resource_exhausted")):
        return "rate_limit"
    if any(k in msg for k in ("401", "403", "unauthorized", "authentication", "forbidden", "api key", "access_denied")):
        return "auth"
    if any(k in msg for k in ("model not found", "invalid model", "unknown model", "no route", "route", "provider not supported")):
        return "route"
    if any(k in msg for k in ("json", "parse", "decode", "expecting value")):
        return "parse"

    return "other"


def extract_section_lengths(content: str) -> Dict[str, int]:
    """从 user prompt 提取各证据区段名称及其字符长度。"""
    if not content:
        return {}

    lines = content.splitlines()
    sections: Dict[str, int] = {}
    current_section = "header"
    current_chars = 0

    known_prefixes = (
        "Opening context",
        "Visible continuation",
        "Earlier-history summary",
        "Recent context",
        "Sampled user-intent trajectory",
        "User messages after the summary",
        "Conversation evidence",
        "Current title",
        "Proposed title",
    )

    for line in lines:
        stripped = line.strip()
        is_new_section = False
        matched_name = None

        if stripped.startswith("#"):
            is_new_section = True
            matched_name = stripped.lstrip("#").strip()
        else:
            for kp in known_prefixes:
                if stripped.startswith(kp):
                    is_new_section = True
                    matched_name = stripped
                    break

        if is_new_section and matched_name:
            if current_chars > 0:
                sections[current_section] = sections.get(current_section, 0) + current_chars
            current_section = matched_name
            current_chars = len(line) + 1
        else:
            current_chars += len(line) + 1

    if current_chars > 0:
        sections[current_section] = sections.get(current_section, 0) + current_chars

    return sections


class TraceLLM:
    """在生产模型调用边界无侵入记录证据与真实账单的透明包装器。"""

    def __init__(
        self,
        inner: Any,
        sink: Any,
        *,
        private_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        self.inner = inner
        self.sink = sink
        self.private_dir = Path(private_dir) if private_dir else None
        self._ctx: Dict[str, Any] = {
            "session_id": None,
            "prefix": None,
            "config_id": None,
        }

    def set_context(
        self,
        session_id: Optional[str] = None,
        prefix: Optional[int] = None,
        config_id: Optional[str] = None,
    ) -> None:
        """设置当前回放/评测上下文元数据。"""
        if session_id is not None:
            self._ctx["session_id"] = session_id
        if prefix is not None:
            self._ctx["prefix"] = prefix
        if config_id is not None:
            self._ctx["config_id"] = config_id

    def complete(self, messages: List[Dict[str, Any]], **kwargs: Any) -> Any:
        """透传请求，捕获 prompt hash、分节长度、耗时、模型路由与真实账单。"""
        session_id = kwargs.pop("session_id", self._ctx["session_id"])
        prefix = kwargs.pop("prefix", self._ctx["prefix"])
        config_id = kwargs.pop("config_id", self._ctx["config_id"])

        system_content = ""
        user_content = ""
        for m in messages:
            role = m.get("role")
            cnt = m.get("content", "") or ""
            if role == "system" and not system_content:
                system_content = cnt
            elif role == "user":
                user_content = cnt

        system_hash = hashlib.sha256(system_content.encode("utf-8")).hexdigest()[:16]
        user_hash = hashlib.sha256(user_content.encode("utf-8")).hexdigest()[:16]
        sec_lengths = extract_section_lengths(user_content)

        model_route = {
            "model": kwargs.get("model") or getattr(self.inner, "model", None),
            "provider": kwargs.get("provider") or getattr(self.inner, "provider", None),
            "task": kwargs.get("task"),
        }

        t0 = time.perf_counter()
        res = None
        error_type: Optional[str] = None
        status = "success"

        try:
            res = self.inner.complete(messages=messages, **kwargs)
            return res
        except Exception as exc:
            status = "error"
            error_type = classify_error(exc)
            raise
        finally:
            elapsed = round(time.perf_counter() - t0, 4)

            usage_dict = None
            token_source = "unavailable"
            if res is not None:
                usage_obj = getattr(res, "usage", None)
                if usage_obj is not None:
                    try:
                        usage_dict = {
                            "input_tokens": int(getattr(usage_obj, "input_tokens", 0) or 0),
                            "output_tokens": int(getattr(usage_obj, "output_tokens", 0) or 0),
                            "cache_read_tokens": int(getattr(usage_obj, "cache_read_tokens", 0) or 0),
                            "cache_write_tokens": int(getattr(usage_obj, "cache_write_tokens", 0) or 0),
                            "cost_usd": getattr(usage_obj, "cost_usd", None),
                        }
                        token_source = "measured"
                    except Exception:
                        usage_dict = None
                        token_source = "unavailable"

            record = {
                "session_id": session_id,
                "prefix": prefix,
                "config_id": config_id,
                "status": status,
                "token_source": token_source,
                "usage": usage_dict,
                "request_id": getattr(res, "id", None) or getattr(res, "request_id", None) if res else None,
                "system_prompt_hash": system_hash,
                "user_prompt_hash": user_hash,
                "section_lengths": sec_lengths,
                "model_route": model_route,
                "elapsed": elapsed,
                "timestamp": time.time(),
            }
            if error_type is not None:
                record["error_type"] = error_type

            self._write_sink(record)

            if self.private_dir:
                self._write_private(
                    session_id=session_id,
                    prefix=prefix,
                    config_id=config_id,
                    messages=messages,
                    response=res,
                    record=record,
                )

    def _write_sink(self, record: Dict[str, Any]) -> None:
        if callable(self.sink):
            self.sink(record)
        elif hasattr(self.sink, "append"):
            self.sink.append(record)
        elif hasattr(self.sink, "write"):
            self.sink.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _write_private(
        self,
        session_id: Optional[str],
        prefix: Optional[int],
        config_id: Optional[str],
        messages: List[Dict[str, Any]],
        response: Any,
        record: Dict[str, Any],
    ) -> None:
        try:
            self.private_dir.mkdir(parents=True, exist_ok=True)
            fname = f"trace_{session_id or 'unknown'}_p{prefix or 0}_{config_id or 'base'}_{int(time.time()*1000)}.json"
            out_file = self.private_dir / fname
            payload = {
                "record": record,
                "messages": messages,
                "response_text": getattr(response, "text", None) if response else None,
            }
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            fd = os.open(str(out_file), flags, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
