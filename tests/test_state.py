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


def test_catalog_discloses_non_invasive_default_and_zero_uninstall_residue():
    import yaml

    manifest = yaml.safe_load(
        Path(__file__).parents[1].joinpath("plugin.yaml").read_text(encoding="utf-8")
    )
    description = manifest["description"]

    # 0.3 契约：默认不接管宿主标题生成，卸载/停用零残留
    assert "Hermes owns the first title" in description
    assert "never rewrites" in description

    schema = manifest["config_schema"]
    assert schema["first_title_mode"]["default"] == "builtin"


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
    assert titler._finalize_intents["s1"]["attempts"] == 2
    assert titler._finalize_intents["s1"]["capacity"] is True
    assert titler._finalize_intents["s1"]["expected_title"] == "旧标题"
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


def test_unresolved_record_survives_subsequent_persist_of_other_sessions(tmp_path):
    path = tmp_path / "state.json"
    record_unknown = {
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
    record_known = {
        "base_title": "已命名",
        "candidate": "新标题2",
        "confirmations": 0,
        "expected_title": "已命名",
        "next_retry_at": 0,
        "attempts": 0,
        "capacity": False,
        "queued_at": 124.0,
        "reason": "finalize",
        "rename_count": 0,
    }
    StateStore(path).save({"unknown": record_unknown, "known": record_known})

    class PartialDB(SessionDB):
        def get_session_title(self, sid):
            if sid == "unknown":
                raise RuntimeError("db temporarily unavailable")
            return self.state[sid][0]

        def get_session_title_source(self, sid):
            if sid == "unknown":
                raise RuntimeError("db temporarily unavailable")
            return self.state[sid][1]

    titler = make_titler(PartialDB({"known": ("已命名", "llm"), "unknown": ("旧标题", "llm")}), path)
    titler.restore_state()

    # Now an unrelated event modifies known session and persists
    with titler._state_lock:
        titler._finalize_intents["known"]["attempts"] = 3
    titler._persist_state()

    sessions = StateStore(path).load()["sessions"]
    assert "unknown" in sessions
    assert sessions["unknown"]["candidate"] == "新标题"
    assert sessions["known"]["finalize"]["attempts"] == 3


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
    assert saved["kind"] == "retry"
    assert saved["retry"]["attempts"] == 1
    assert saved["retry"]["capacity"] is True
    assert saved["retry"]["reason"] == "error"


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
            events.append(
                (
                    "start_retry_loop",
                    bool(self._failed_sessions or self._finalize_intents),
                )
            )

    ctx = SimpleNamespace(
        register_hook=lambda *args, **kwargs: None,
        register_command=lambda *args, **kwargs: None,
        llm=NoNetworkLlm(),
    )

    monkeypatch.setattr(plugin_entry, "AutoTitler", TrackedTitler)
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
    assert saved["pending_review"]["candidate"] == "未背书候选"
    assert saved["pending_review"]["confirmations"] == 0
    assert saved["finalize_intent"] is True


def test_completed_inflight_still_queues_finalize_without_epoch_proof(tmp_path):
    import threading

    path = tmp_path / "state.json"
    titler = make_titler(SessionDB({"s1": ("旧标题", "llm")}), path)
    finished = threading.Event()
    finished.set()
    titler._inflight["s1"] = finished
    titler._pending["s1"] = {
        "title": "未背书候选",
        "base_title": "旧标题",
        "confirmations": 0,
    }

    titler._close_eval("s1")

    saved = StateStore(path).load()["sessions"]["s1"]
    assert saved["finalize_intent"] is True
    assert saved["pending_review"]["candidate"] == "未背书候选"


def test_finalize_intent_has_independent_retry_budget(tmp_path):
    path = tmp_path / "state.json"
    titler = make_titler(SessionDB({"s1": ("旧标题", "llm")}), path)
    titler._failed_sessions["s1"] = {
        "attempts": 5,
        "next_retry_at": 0,
        "capacity": False,
        "reason": "error",
    }
    titler._queue_session("s1", reason="finalize")
    submitted = []
    titler._submit_eval = submitted.append

    titler._retry_failed_sessions()

    assert submitted == ["s1"]
    assert "s1" in titler._finalize_intents


def test_restore_invalid_mixed_record_isolated_without_partial_state(tmp_path):
    path = tmp_path / "state.json"
    StateStore(path).save(
        {
            "bad": {
                "kind": "pending+retry",
                "base_title": "旧标题",
                "pending_review": {
                    "candidate": "不应残留",
                    "confirmations": 0,
                },
                "retry": {"attempts": "bad", "next_retry_at": 0},
            }
        }
    )
    titler = make_titler(SessionDB({"bad": ("旧标题", "llm")}), path)

    titler.restore_state()

    assert "bad" not in titler._pending
    assert "bad" not in titler._failed_sessions
    assert "bad" not in titler._finalize_intents
    assert StateStore(path).load()["sessions"] == {}


def test_restore_skips_bad_record_but_loads_other_records(tmp_path):
    path = tmp_path / "state.json"
    StateStore(path).save(
        {
            "bad": {
                "kind": "retry",
                "base_title": "坏标题",
                "retry": {"attempts": "bad", "next_retry_at": 0},
            },
            "good": {
                "kind": "finalize",
                "base_title": "好标题",
                "finalize_intent": True,
                "expected_title": "好标题",
                "finalize": {"attempts": 0, "next_retry_at": 0, "queued_at": 123.0},
            },
        }
    )
    titler = make_titler(
        SessionDB({"bad": ("坏标题", "llm"), "good": ("好标题", "llm")}),
        path,
    )

    titler.restore_state()

    assert "bad" not in titler._failed_sessions
    assert titler._finalize_intents["good"]["reason"] == "finalize"


def test_counter_only_record_does_not_become_retry(tmp_path):
    path = tmp_path / "state.json"
    StateStore(path).save(
        {
            "s1": {
                "kind": "counter",
                "base_title": "已有标题",
                "rename_count": 3,
            }
        }
    )
    titler = make_titler(SessionDB({"s1": ("已有标题", "llm")}), path)

    titler.restore_state()

    assert titler._rename_counts["s1"] == 3
    assert "s1" not in titler._failed_sessions
    assert "s1" not in titler._finalize_intents


def test_pending_only_record_does_not_become_retry(tmp_path):
    path = tmp_path / "state.json"
    StateStore(path).save(
        {
            "s1": {
                "kind": "pending",
                "base_title": "已有标题",
                "expected_title": "已有标题",
                "rename_count": 0,
                "pending_review": {
                    "candidate": "待审标题",
                    "confirmations": 0,
                },
            }
        }
    )
    titler = make_titler(SessionDB({"s1": ("已有标题", "llm")}), path)

    titler.restore_state()
    submitted = []
    titler._submit_eval = submitted.append
    titler._retry_failed_sessions()

    assert titler._pending["s1"]["title"] == "待审标题"
    assert "s1" not in titler._failed_sessions
    assert "s1" not in titler._finalize_intents
    assert submitted == []


def test_lock_hierarchy_never_nests_state_and_inflight_locks():
    """Verify that _state_lock and _inflight_lock are never acquired simultaneously."""
    import threading

    titler = AutoTitler(SimpleNamespace(llm=NoNetworkLlm()), {**DEFAULTS}, db=SessionDB({}))

    state_held = False
    inflight_held = False
    inversion_detected = False

    class TrackingStateLock:
        def __init__(self, real_lock):
            self._real = real_lock

        def __enter__(self):
            nonlocal state_held, inversion_detected
            if inflight_held:
                inversion_detected = True
            res = self._real.__enter__()
            state_held = True
            return res

        def __exit__(self, *args):
            nonlocal state_held
            state_held = False
            return self._real.__exit__(*args)

        def acquire(self, *args, **kwargs):
            return self._real.acquire(*args, **kwargs)

        def release(self):
            return self._real.release()

    class TrackingInflightLock:
        def __init__(self, real_lock):
            self._real = real_lock

        def __enter__(self):
            nonlocal inflight_held, inversion_detected
            if state_held:
                inversion_detected = True
            res = self._real.__enter__()
            inflight_held = True
            return res

        def __exit__(self, *args):
            nonlocal inflight_held
            inflight_held = False
            return self._real.__exit__(*args)

        def acquire(self, *args, **kwargs):
            return self._real.acquire(*args, **kwargs)

        def release(self):
            return self._real.release()

    titler._state_lock = TrackingStateLock(titler._state_lock)
    titler._retry_lock = titler._state_lock
    titler._rename_count_lock = titler._state_lock
    titler._inflight_lock = TrackingInflightLock(titler._inflight_lock)

    # Run _submit_eval
    titler._submit_eval("s1")
    assert not inversion_detected, "Lock inversion detected in _submit_eval"

    # Run _close_eval
    titler._close_eval("s1")
    assert not inversion_detected, "Lock inversion detected in _close_eval"

    # Run _retry_failed_sessions
    titler._retry_failed_sessions()
    assert not inversion_detected, "Lock inversion detected in _retry_failed_sessions"


def test_closing_fence_blocks_concurrent_ordinary_submit_before_intent_persisted(
    tmp_path, monkeypatch
):
    import threading

    entered_db_read = threading.Event()
    release_db_read = threading.Event()
    network_started = threading.Event()

    class BlockingDB(SessionDB):
        def get_session_title(self, sid):
            entered_db_read.set()
            assert release_db_read.wait(2)
            return super().get_session_title(sid)

    path = tmp_path / "state.json"
    titler = make_titler(BlockingDB({"s1": ("旧标题", "llm")}), path)
    monkeypatch.setattr(
        titler,
        "evaluate",
        lambda session_id, force=False: network_started.set() or {"action": "keep"},
    )

    close_thread = threading.Thread(target=titler._close_eval, args=("s1",))
    close_thread.start()
    assert entered_db_read.wait(2)

    submit_done = threading.Event()

    def submit():
        titler._submit_eval("s1")
        submit_done.set()

    submit_thread = threading.Thread(target=submit)
    submit_thread.start()
    assert not network_started.wait(0.05)

    release_db_read.set()
    close_thread.join(2)
    submit_thread.join(2)

    assert submit_done.is_set()
    assert not network_started.is_set()
    assert titler._finalize_intents["s1"]["close_epoch"] == 1


def test_retry_loop_claims_existing_finalize_epoch_explicitly(tmp_path):
    path = tmp_path / "state.json"
    titler = make_titler(SessionDB({"s1": ("旧标题", "llm")}), path)
    titler._queue_session("s1", reason="finalize", close_epoch=7)
    submitted = []

    def submit(session_id, *, finalize_claim=None):
        submitted.append((session_id, finalize_claim))

    titler._submit_eval = submit
    titler._retry_failed_sessions()

    assert submitted == [("s1", 7)]


def test_unknown_close_anchor_is_not_compared_as_known_none(tmp_path):
    path = tmp_path / "state.json"

    class BrokenDB(SessionDB):
        def get_session_title(self, sid):
            raise RuntimeError("db unavailable during close")

    first = make_titler(BrokenDB({"s1": ("真实标题", "llm")}), path)
    first._queue_session("s1", reason="finalize", close_epoch=1)

    saved = StateStore(path).load()["sessions"]["s1"]
    assert saved["base_title"] is None
    assert saved["base_title_known"] is False

    restored = make_titler(SessionDB({"s1": ("真实标题", "llm")}), path)
    restored.restore_state()

    assert "s1" in restored._finalize_intents
    assert StateStore(path).load()["sessions"]["s1"]["finalize_intent"] is True


def test_worker_from_before_close_cannot_erase_finalize_intent(tmp_path, monkeypatch):
    import threading

    path = tmp_path / "state.json"
    titler = make_titler(SessionDB({"s1": ("旧标题", "llm")}), path)
    entered = threading.Event()
    release = threading.Event()

    def stale_evaluate(session_id, force=False):
        entered.set()
        assert release.wait(2)
        with titler._state_lock:
            titler._failed_sessions.pop(session_id, None)
        return {"action": "keep"}

    monkeypatch.setattr(titler, "evaluate", stale_evaluate)
    titler._submit_eval("s1")
    assert entered.wait(2)
    titler._close_eval("s1")
    release.set()
    event = titler._inflight.get("s1")
    if event is not None:
        assert event.wait(2)

    saved = StateStore(path).load()["sessions"]["s1"]
    assert saved["finalize_intent"] is True


def test_finalize_worker_forces_evaluation_past_throttle(tmp_path, monkeypatch):
    import threading

    path = tmp_path / "state.json"
    titler = make_titler(SessionDB({"s1": ("旧标题", "llm")}), path)
    titler._queue_session("s1", reason="finalize", close_epoch=1)
    titler._closing_epochs["s1"] = 1
    calls = []

    def evaluate(session_id, force=False):
        calls.append((session_id, force))
        return {"action": "keep"}

    monkeypatch.setattr(titler, "evaluate", evaluate)
    titler._eval_worker("s1", threading.Event(), worker_epoch=1)

    assert calls == [("s1", True)]
    assert "s1" not in titler._finalize_intents


def test_failed_finalize_worker_keeps_independent_backoff_intent(tmp_path, monkeypatch):
    import threading
    import time

    path = tmp_path / "state.json"
    titler = make_titler(SessionDB({"s1": ("旧标题", "llm")}), path)
    titler._queue_session("s1", reason="finalize", close_epoch=1)
    titler._closing_epochs["s1"] = 1
    before = time.monotonic()

    monkeypatch.setattr(
        titler,
        "evaluate",
        lambda session_id, force=False: {"action": "failed", "reason": "model call failed"},
    )
    titler._eval_worker("s1", threading.Event(), worker_epoch=1)

    intent = titler._finalize_intents["s1"]
    assert intent["attempts"] == 1
    assert intent["next_retry_at"] >= before + 29
    saved = StateStore(path).load()["sessions"]["s1"]
    assert saved["finalize"]["attempts"] == 1


def test_pending_finalize_worker_keeps_intent_for_confirmation(tmp_path, monkeypatch):
    import threading

    path = tmp_path / "state.json"
    titler = make_titler(SessionDB({"s1": ("旧标题", "llm")}), path)
    titler._queue_session("s1", reason="finalize", close_epoch=1)
    titler._closing_epochs["s1"] = 1

    monkeypatch.setattr(
        titler,
        "evaluate",
        lambda session_id, force=False: {"action": "pending", "candidate": "待审标题"},
    )
    titler._eval_worker("s1", threading.Event(), worker_epoch=1)

    assert titler._finalize_intents["s1"]["attempts"] == 1
    assert StateStore(path).load()["sessions"]["s1"]["finalize_intent"] is True


def test_unknown_anchor_intent_is_not_covered_by_ordinary_turn_worker(tmp_path, monkeypatch):
    """A close whose DB read failed must not be "covered" by a later turn.

    The intent written during the outage carries ``base_title_known=False``:
    the terminal snapshot was never observed.  Any worker that merely claims the
    close epoch can therefore satisfy ``close_epoch <= worker_epoch`` without
    having seen that snapshot, turning the coverage proof into a tautology.  The
    intent must survive until the DB is readable and its anchor is validated.
    """
    import threading

    path = tmp_path / "state.json"

    class FlakyDB(SessionDB):
        """Reads fail until ``recovered`` is set, mimicking a real outage."""

        def __init__(self, state):
            super().__init__(state)
            self.recovered = threading.Event()

        def get_session_title(self, sid):
            if not self.recovered.is_set():
                raise RuntimeError("db unavailable")
            return super().get_session_title(sid)

        def get_session_title_source(self, sid):
            if not self.recovered.is_set():
                raise RuntimeError("db unavailable")
            return super().get_session_title_source(sid)

    db = FlakyDB({"s1": ("旧标题", "llm")})
    titler = make_titler(db, path)

    # Close during the outage: the anchor is unknown and stays queued.
    titler._close_eval("s1")
    assert titler._finalize_intents["s1"]["base_title_known"] is False
    assert titler._finalize_intents["s1"]["close_epoch"] == 1
    assert "s1" in titler._closing_fenced

    # DB recovers.  A later foreground turn reaches the retry sweep.  That
    # sweep must NOT consume the intent: it only re-validates the anchor and
    # defers, because the terminal snapshot was never observed.
    db.recovered.set()
    calls = []
    monkeypatch.setattr(
        titler,
        "evaluate",
        lambda session_id, force=False: calls.append(session_id) or {"action": "keep"},
    )
    titler._retry_failed_sessions()
    assert calls == [], "first sweep consumed an unknown-anchor intent"
    _drain_inflight(titler, "s1")
    assert titler._finalize_intents["s1"]["base_title_known"] is True
    assert titler._finalize_intents["s1"]["base_title"] == "旧标题"
    assert "s1" in titler._closing_fenced, "fence must survive the deferred sweep"

    # A later sweep, with the anchor now validated, may claim and settle it.
    titler._retry_failed_sessions()
    assert len(calls) == 1, "validated intent was never consumed"
    _drain_inflight(titler, "s1")
    assert "s1" not in titler._finalize_intents
    assert "s1" not in titler._closing_fenced


def _drain_inflight(titler, sid, timeout=5.0):
    """Wait for an in-flight worker to finish so the assertion is deterministic."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        with titler._inflight_lock:
            event = titler._inflight.get(sid)
        if event is None:
            return
        event.wait(0.05)


def test_close_epoch_worker_clears_finalize_only_after_covering_epoch(tmp_path, monkeypatch):
    import threading

    path = tmp_path / "state.json"
    titler = make_titler(SessionDB({"s1": ("旧标题", "llm")}), path)
    titler._queue_session("s1", reason="finalize", close_epoch=1)
    titler._closing_epochs["s1"] = 1
    evaluated = threading.Event()

    def covered_evaluate(session_id, force=False):
        with titler._state_lock:
            titler._failed_sessions.pop(session_id, None)
        evaluated.set()
        return {"action": "keep"}

    monkeypatch.setattr(titler, "evaluate", covered_evaluate)
    titler._submit_eval("s1", finalize_claim=1)
    assert evaluated.wait(2)
    event = titler._inflight.get("s1")
    if event is not None:
        assert event.wait(2)

    assert titler._completed_epochs["s1"] >= 1
    assert "s1" not in titler._finalize_intents
    assert StateStore(path).load()["sessions"] == {}


def test_retry_sweep_preserves_intent_when_db_is_unknown(tmp_path):
    path = tmp_path / "state.json"

    class BrokenDB(SessionDB):
        def get_session_title_source(self, sid):
            raise RuntimeError("db unavailable")

    titler = make_titler(BrokenDB({"s1": ("旧标题", "llm")}), path)
    titler._finalize_intents["s1"] = {
        "attempts": 0,
        "next_retry_at": 0,
        "capacity": False,
        "reason": "finalize",
        "base_title": "旧标题",
        "expected_title": "旧标题",
        "close_epoch": 1,
    }
    submitted = []
    titler._submit_eval = submitted.append

    titler._retry_failed_sessions()

    assert submitted == []
    assert "s1" in titler._finalize_intents


def test_close_path_total_stays_below_host_budget_after_100ms_network_wait(
    tmp_path, monkeypatch
):
    import threading
    import time

    path = tmp_path / "state.json"
    titler = make_titler(SessionDB({"s1": ("旧标题", "llm")}), path)
    titler._inflight["s1"] = threading.Event()
    waits = []
    real_wait = titler._inflight["s1"].wait

    def recording_wait(timeout=None):
        waits.append(timeout)
        return real_wait(timeout)

    monkeypatch.setattr(titler._inflight["s1"], "wait", recording_wait)
    started = time.monotonic()
    titler._close_eval("s1")
    elapsed = time.monotonic() - started

    assert waits == [0.1]
    assert elapsed < 1.0
    assert StateStore(path).load()["sessions"]["s1"]["finalize_intent"] is True


def test_persistent_snapshot_barrier_blocks_pending_mutation(tmp_path, monkeypatch):
    import threading

    path = tmp_path / "state.json"
    titler = make_titler(SessionDB({"s1": ("旧标题", "llm")}), path)
    titler._pending["s1"] = {
        "title": "候选 A",
        "base_title": "旧标题",
        "confirmations": 0,
    }
    entered = threading.Event()
    release = threading.Event()
    real_save = StateStore.save

    def blocking_save(store, sessions):
        entered.set()
        assert release.wait(2)
        return real_save(store, sessions)

    monkeypatch.setattr(StateStore, "save", blocking_save)
    persist_thread = threading.Thread(target=titler._persist_state)
    persist_thread.start()
    assert entered.wait(2)
    mutation_finished = threading.Event()

    def mutate():
        with titler._state_lock:
            titler._pending["s1"] = {
                "title": "候选 B",
                "base_title": "旧标题",
                "confirmations": 0,
            }
        mutation_finished.set()

    mutation_thread = threading.Thread(target=mutate)
    mutation_thread.start()
    assert not mutation_finished.wait(0.05)
    release.set()
    persist_thread.join(2)
    mutation_thread.join(2)
    assert mutation_finished.is_set()


def test_retry_cleanup_does_not_clear_concurrent_finalize_intent(tmp_path, monkeypatch):
    """P1: Retry sweep must not erase a finalize intent created during the sweep."""
    path = tmp_path / "state.json"
    db = SessionDB({"s1": ("已生成标题", "llm")})
    titler = make_titler(db, path)
    titler._failed_sessions["s1"] = {
        "attempts": 1,
        "next_retry_at": 0,
        "capacity": False,
        "reason": "error",
    }
    titler._persist_state()

    orig_source = db.get_session_title_source
    def racing_source(sid):
        res = orig_source(sid)
        # Concurrent session finalize creates a finalize intent
        titler._queue_session(sid, reason="finalize", close_epoch=1)
        titler._closing_fenced.add(sid)
        return res

    monkeypatch.setattr(db, "get_session_title_source", racing_source)
    titler._retry_failed_sessions()

    assert "s1" not in titler._failed_sessions
    assert "s1" in titler._finalize_intents
    assert titler._finalize_intents["s1"]["close_epoch"] == 1
    assert "s1" in titler._closing_fenced


def test_dirty_rerun_does_not_promote_worker_to_finalize_claimant(tmp_path, monkeypatch):
    """P1: An ordinary worker undergoing dirty rerun must not elevate to finalize claimant."""
    import threading

    path = tmp_path / "state.json"
    titler = make_titler(SessionDB({"s1": ("已有标题", "llm")}), path)

    first_eval_started = threading.Event()
    release_first_eval = threading.Event()
    eval_count = 0

    def mock_eval(session_id, force=False, blind=False, claim_epoch=None):
        nonlocal eval_count
        eval_count += 1
        if eval_count == 1:
            first_eval_started.set()
            assert release_first_eval.wait(2)
        return {"action": "keep"}

    monkeypatch.setattr(titler, "evaluate", mock_eval)

    # Spawn an ordinary worker (epoch 0)
    titler._submit_eval("s1")
    assert first_eval_started.wait(2)

    # While first eval is running, finalize happens and creates close_epoch=1
    with titler._state_lock:
        titler._closing_epochs["s1"] = 1
        titler._queue_session_locked("s1", reason="finalize", close_epoch=1)
        titler._closing_fenced.add("s1")

    # Mark the session dirty to trigger a rerun in the same worker
    with titler._inflight_lock:
        titler._dirty_sessions.add("s1")

    # Release the worker to do its dirty rerun
    release_first_eval.set()
    _drain_inflight(titler, "s1")

    # The worker reran (eval_count == 2), but could NOT clear the finalize intent
    # because its worker_epoch was 0 and was not upgraded.
    assert eval_count >= 2
    assert "s1" in titler._finalize_intents
    assert titler._finalize_intents["s1"]["close_epoch"] == 1
    assert "s1" in titler._closing_fenced


def test_finalize_persistence_fails_open_on_io_error(tmp_path, monkeypatch):
    """P1: Persistence errors during finalize must not bubble to host finalize."""
    path = tmp_path / "state.json"
    titler = make_titler(SessionDB({"s1": ("旧标题", "llm")}), path)

    def broken_save(store, sessions):
        raise OSError("Disk full: no space left on device")

    monkeypatch.setattr(StateStore, "save", broken_save)

    # on_session_finalize must NOT raise an exception
    titler.on_session_finalize(session_id="s1", platform="cli", reason="session_boundary")

    # And in-memory finalize state was still tracked under fence
    assert "s1" in titler._closing_fenced
    assert "s1" in titler._finalize_intents

