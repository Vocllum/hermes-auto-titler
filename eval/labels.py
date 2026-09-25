from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Union

FORBIDDEN_KEYS = {
    "content",
    "message",
    "messages",
    "text",
    "raw_text",
    "secret",
    "token",
    "key",
    "password",
    "prompt",
}


def _check_forbidden_keys(obj: Any) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in FORBIDDEN_KEYS:
                raise ValueError(f"forbidden key found in manifest: {k}")
            _check_forbidden_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            _check_forbidden_keys(item)


def validate_manifest(source: Union[str, Path, Dict[str, Any]]) -> bool:
    """校验评测 manifest 文件的完整性与安全合规性。"""
    if isinstance(source, (str, Path)):
        p = Path(source)
        if not p.exists():
            raise ValueError(f"manifest file not found: {source}")
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    elif isinstance(source, dict):
        data = source
    else:
        raise ValueError("source must be a file path or dict")

    if not isinstance(data, dict):
        raise ValueError("manifest root must be an object")

    if data.get("schema") != 1:
        raise ValueError("unsupported manifest schema version")

    _check_forbidden_keys(data)

    sessions = data.get("sessions")
    if not isinstance(sessions, list):
        raise ValueError("sessions must be a list")

    seen_ids = set()
    for s in sessions:
        if not isinstance(s, dict):
            raise ValueError("session entry must be a dict")
        sid = s.get("id")
        if not sid:
            raise ValueError("session id is required")
        if sid in seen_ids:
            raise ValueError(f"duplicate session id found: {sid}")
        seen_ids.add(sid)

        s_class = s.get("class", "")
        human_turns = s.get("human_turns", 0)

        # 非特殊负例样本必须保证 human_turns >= 8
        if s_class != "manual_title_protected" and human_turns < 8:
            raise ValueError(f"session {sid} human_turns must be >= 8, got {human_turns}")

        prefix_labels = s.get("prefix_labels")
        if prefix_labels is not None:
            if not isinstance(prefix_labels, list):
                raise ValueError("prefix_labels must be a list")
            for pl in prefix_labels:
                if not isinstance(pl, dict):
                    raise ValueError("prefix label item must be a dict")
                subj = pl.get("durable_subject")
                if not isinstance(subj, str) or not subj.strip():
                    raise ValueError(f"session {sid} durable_subject cannot be empty")
                req_ids = pl.get("required_identifiers")
                if req_ids is not None and not isinstance(req_ids, list):
                    raise ValueError(f"session {sid} required_identifiers must be list")
                acc_titles = pl.get("acceptable_titles")
                if acc_titles is not None and not isinstance(acc_titles, list):
                    raise ValueError(f"session {sid} acceptable_titles must be list")
                sec_topics = pl.get("secondary_topics")
                if sec_topics is not None and not isinstance(sec_topics, list):
                    raise ValueError(f"session {sid} secondary_topics must be list")

    return True
