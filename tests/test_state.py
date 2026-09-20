"""Durable scheduler-state tests."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_auto_titler.config import DEFAULTS
from hermes_auto_titler.policy import AutoTitler
from hermes_auto_titler.state import StateStore
from hermes_auto_titler.titler import AutoTitler as BaseAutoTitler


class SessionDB:
    TITLE_SOURCE_DERIVED = "derived"
    TITLE_SOURCE_LLM = "llm"
    TITLE_SOURCE_USER = "user"
    MAX_TITLE_LENGTH = 100

    def __init__(self, state):
        self.state = dict(state)

    def get_session_title(self, sid):
        return self.state[sid][0]

    def get_session_title_source(self, sid):
        return self.state[sid][1]


class NoNetworkLlm:
    def complete(self, **kwargs):  # pragma: no cover - a call is a test failure
        raise AssertionError("restore must not call the network")


def make_titler(db, path: Path):
    titler = AutoTitler(SimpleNamespace(llm=NoNetworkLlm()), {**DEFAULTS}, db=db)
    titler._state_path = path
    return titler


def test_state_store_replaces_atomically_and_leaves_no_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    store = StateStore(path)
    replaced = []
    real_replace = os.replace

    def recording_replace(src, dst):
        src_path = Path(src)
        assert src_path.parent == path.parent
        assert src_path.read_text(encoding="utf-8").endswith("\n")
        replaced.append((src_path, Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", recording_replace)
    store.save({"s1": {"reason": "finalize"}})

    assert replaced and replaced[0][1] == path
    assert store.load()["sessions"]["s1"]["reason"] == "finalize"
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_restore_rebuilds_valid_pending_retry_and_rename_count(tmp_path):
    path = tmp_path / "state.json"
    StateStore(path).save(
        {
            "s1": {
                "base_title": "旧标题",
                "candidate": "新标题",
                "confirmations": 1,
                "expected_title": "旧标题",
                "next_retry_at": 0,
                "attempts": 2,
                "capacity": True,
                "queued_at": 123.0,
                "reason": "finalize budget exceeded",
                "rename_count": 3,
            }
        }
    )
    titler = make_titler(SessionDB({"s1": ("旧标题", "llm")}), path)

    titler.restore_state()

    assert titler._pending["s1"] == {
        "title": "新标题",
        "base_title": "旧标题",
        "confirmations": 1,
    }
    assert titler._failed_sessions["s1"]["attempts"] == 2
    assert titler._failed_sessions["s1"]["capacity"] is True
    assert titler._failed_sessions["s1"]["expected_title"] == "旧标题"
    assert titler._rename_counts["s1"] == 3


@pytest.mark.parametrize(
    ("current", "source"),
    [("其他自动标题", "llm"), ("用户手改", "user")],
)
def test_restore_discards_stale_or_user_authoritative_records(tmp_path, current, source):
    path = tmp_path / "state.json"
    StateStore(path).save(
        {
            "s1": {
                "base_title": "旧标题",
                "candidate": "新标题",
                "confirmations": 0,
                "expected_title": "旧标题",
                "next_retry_at": 0,
                "attempts": 0,
                "capacity": False,
                "queued_at": 123.0,
                "reason": "finalize",
                "rename_count": 0,
            }
        }
    )
    titler = make_titler(SessionDB({"s1": (current, source)}), path)

    titler.restore_state()

    assert "s1" not in titler._pending
    assert "s1" not in titler._failed_sessions
    assert StateStore(path).load()["sessions"] == {}


def test_restore_keeps_record_when_db_temporarily_unavailable(tmp_path):
    path = tmp_path / "state.json"
    record = {
        "base_title": "旧标题",
        "candidate": "新标题",
        "confirmations": 0,
        "expected_title": "旧标题",
        "next_retry_at": 0,
        "attempts": 1,
        "capacity": False,
        "queued_at": 123.0,
        "reason": "finalize",
        "rename_count": 0,
    }
    StateStore(path).save({"s1": record})

    class BrokenDB(SessionDB):
        def get_session_title(self, sid):
            raise RuntimeError("db temporarily unavailable")

    titler = make_titler(BrokenDB({"s1": ("旧标题", "llm")}), path)

    titler.restore_state()

    assert StateStore(path).load()["sessions"]["s1"]["candidate"] == "新标题"


def test_restore_corrupt_state_fails_closed_without_overwrite(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text("{not-json", encoding="utf-8")
    titler = make_titler(SessionDB({}), path)

    titler.restore_state()

    assert titler._pending == {}
    assert titler._failed_sessions == {}
    assert path.read_text(encoding="utf-8") == "{not-json"
    assert "state load failed" in caplog.text


def test_missing_state_file_loads_empty(tmp_path):
    path = tmp_path / "missing.json"
    titler = make_titler(SessionDB({}), path)

    titler.restore_state()

    assert titler._pending == {}
    assert titler._failed_sessions == {}
    assert not path.exists()


def test_restore_then_retry_continues_confirmation_gate(tmp_path):
    path = tmp_path / "state.json"
    StateStore(path).save(
        {
            "s1": {
                "base_title": "旧标题",
                "candidate": "新标题",
                "confirmations": 0,
                "expected_title": "旧标题",
                "next_retry_at": 0,
                "attempts": 0,
                "capacity": False,
                "queued_at": 123.0,
                "reason": "finalize",
                "rename_count": 0,
            }
        }
    )
    db = SessionDB({"s1": ("旧标题", "llm")})
    titler = make_titler(db, path)
    titler.restore_state()
    submitted = []
    titler._submit_eval = submitted.append

    titler._retry_failed_sessions()

    assert submitted == ["s1"]
    assert db.get_session_title("s1") == "旧标题"


def test_plain_retry_metadata_persists_current_title_as_base(tmp_path):
    path = tmp_path / "state.json"
    titler = make_titler(SessionDB({"s1": ("已有标题", "llm")}), path)
    titler._failed_sessions["s1"] = {
        "attempts": 1,
        "next_retry_at": 0,
        "capacity": False,
    }

    titler._persist_state()

    saved = StateStore(path).load()["sessions"]["s1"]
    assert saved["base_title"] == "已有标题"
    assert saved["expected_title"] == "已有标题"


def test_runtime_failure_is_persisted_for_restart(tmp_path):
    path = tmp_path / "state.json"

    class MessagesDB(SessionDB):
        def get_messages_as_conversation(self, session_id, include_ancestors=False):
            return [{"role": "user", "content": "测试失败恢复"}]

    class FailingLlm:
        def complete(self, **kwargs):
            raise RuntimeError("503 overloaded")

    db = MessagesDB({"s1": (None, None)})
    titler = AutoTitler(SimpleNamespace(llm=FailingLlm()), {**DEFAULTS}, db=db)
    titler._state_path = path

    result = titler.evaluate("s1", force=True)

    assert result["action"] == "failed"
    saved = StateStore(path).load()["sessions"]["s1"]
    assert saved["attempts"] == 1
    assert saved["capacity"] is True
    assert saved["reason"] == "error"


def test_successful_evaluation_prunes_durable_retry_state(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    db = SessionDB({"s1": ("旧标题", "llm")})
    titler = make_titler(db, path)
    titler._failed_sessions["s1"] = {
        "attempts": 1,
        "next_retry_at": 0,
        "capacity": False,
        "reason": "error",
        "base_title": "旧标题",
    }
    titler._persist_state()
    monkeypatch.setattr(
        BaseAutoTitler,
        "_execute_evaluate",
        lambda self, session_id, force=False, blind=False: (
            self._failed_sessions.pop(session_id, None) or {"action": "keep"}
        ) and {"action": "keep"},
    )

    result = titler.evaluate("s1", force=True)

    assert result["action"] == "keep"
    assert StateStore(path).load()["sessions"] == {}


def test_register_restores_state_before_starting_retry_loop(tmp_path, monkeypatch):
    import hermes_auto_titler as plugin_entry

    path = tmp_path / "state.json"
    StateStore(path).save(
        {
            "s1": {
                "base_title": "旧标题",
                "candidate": "新标题",
                "confirmations": 0,
                "expected_title": "旧标题",
                "next_retry_at": 0,
                "attempts": 0,
                "capacity": False,
                "queued_at": 123.0,
                "reason": "finalize",
                "rename_count": 0,
            }
        }
    )

    db = SessionDB({"s1": ("旧标题", "llm")})
    events = []

    class TrackedTitler(AutoTitler):
        def __init__(self, ctx, cfg):
            super().__init__(ctx, cfg, db=db)
            self._state_path = path

        def restore_state(self):
            events.append("restore_state")
            super().restore_state()

        def start_retry_loop(self):
            events.append(("start_retry_loop", bool(self._failed_sessions)))

    ctx = SimpleNamespace(
        register_hook=lambda *args, **kwargs: None,
        register_command=lambda *args, **kwargs: None,
        llm=NoNetworkLlm(),
    )

    monkeypatch.setattr(plugin_entry, "AutoTitler", TrackedTitler)
    monkeypatch.setattr(plugin_entry, "disable_builtin_title_generation", lambda: False)
    monkeypatch.setattr(plugin_entry, "load_config", lambda: {**DEFAULTS, "enabled": True})

    plugin_entry.register(ctx)

    assert events == ["restore_state", ("start_retry_loop", True)]


def test_queue_persists_unendorsed_candidate_but_never_writes_title(tmp_path):
    path = tmp_path / "state.json"
    db = SessionDB({"s1": ("旧标题", "llm")})
    titler = make_titler(db, path)
    titler._pending["s1"] = {
        "title": "未背书候选",
        "base_title": "旧标题",
        "confirmations": 0,
    }

    titler._queue_session("s1", reason="finalize")

    assert db.get_session_title("s1") == "旧标题"
    saved = json.loads(path.read_text(encoding="utf-8"))["sessions"]["s1"]
    assert saved["candidate"] == "未背书候选"
    assert saved["confirmations"] == 0
