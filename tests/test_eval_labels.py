import json
from pathlib import Path
import pytest
from eval.labels import validate_manifest


def test_validate_manifest_valid_structure(tmp_path):
    valid_data = {
        "schema": 1,
        "classes": ["compacted", "multi_topic", "single_topic", "short_dialog", "shift", "noise", "repeat_compacted", "identifiers"],
        "sessions": [
            {
                "id": "sess_1",
                "class": "compacted",
                "initial_source": "llm",
                "human_turns": 8,
                "prefix_labels": [
                    {
                        "turn": 1,
                        "durable_subject": "Hermes AutoTitler 架构重构",
                        "allowed_shift": False,
                        "required_identifiers": ["AutoTitler", "SessionDB"],
                    }
                ],
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(valid_data), encoding="utf-8")
    assert validate_manifest(str(path)) is True


def test_validate_manifest_rejects_duplicate_id(tmp_path):
    data = {
        "schema": 1,
        "classes": ["compacted"],
        "sessions": [
            {
                "id": "sess_dup",
                "class": "compacted",
                "initial_source": "llm",
                "human_turns": 8,
                "prefix_labels": [{"turn": 1, "durable_subject": "主题", "allowed_shift": False, "required_identifiers": []}],
            },
            {
                "id": "sess_dup",
                "class": "compacted",
                "initial_source": "llm",
                "human_turns": 8,
                "prefix_labels": [{"turn": 1, "durable_subject": "主题2", "allowed_shift": False, "required_identifiers": []}],
            },
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        validate_manifest(str(path))


def test_validate_manifest_rejects_insufficient_turns(tmp_path):
    data = {
        "schema": 1,
        "classes": ["compacted"],
        "sessions": [
            {
                "id": "sess_short",
                "class": "compacted",
                "initial_source": "llm",
                "human_turns": 5,  # < 8
                "prefix_labels": [{"turn": 1, "durable_subject": "主题", "allowed_shift": False, "required_identifiers": []}],
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="human_turns"):
        validate_manifest(str(path))


def test_validate_manifest_rejects_sensitive_or_dialog_keys(tmp_path):
    data = {
        "schema": 1,
        "classes": ["compacted"],
        "sessions": [
            {
                "id": "sess_leak",
                "class": "compacted",
                "initial_source": "llm",
                "human_turns": 8,
                "messages": ["some text"],  # 泄漏原始对话/密钥字段
                "prefix_labels": [{"turn": 1, "durable_subject": "主题", "allowed_shift": False, "required_identifiers": []}],
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="forbidden"):
        validate_manifest(str(path))


def test_validate_manifest_rejects_empty_durable_subject(tmp_path):
    data = {
        "schema": 1,
        "classes": ["compacted"],
        "sessions": [
            {
                "id": "sess_empty_subj",
                "class": "compacted",
                "initial_source": "llm",
                "human_turns": 8,
                "prefix_labels": [{"turn": 1, "durable_subject": "   ", "allowed_shift": False, "required_identifiers": []}],
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="durable_subject"):
        validate_manifest(str(path))


def test_production_fixture_manifest():
    manifest_path = Path(__file__).parent.parent / "eval" / "fixtures" / "manifest.json"
    assert manifest_path.exists()
    assert validate_manifest(str(manifest_path)) is True
