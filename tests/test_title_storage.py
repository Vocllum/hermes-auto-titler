"""Lossless production titles with isolated SQLite and a stubbed host SDK."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_auto_titler.config import DEFAULTS
from hermes_auto_titler import titler as core
from hermes_auto_titler.messages import display_width
from hermes_auto_titler.policy import AutoTitler
from eval.replay import SandboxSqliteSessionDB


CHINESE_TITLE = "文字工作 - 梳理供应链运输规划实验的可执行步骤与问题说明"
IDENTIFIER_TITLE = "BetaSyncImportRetryStrategyConfigurationMigration"
TITLES = [CHINESE_TITLE, IDENTIFIER_TITLE]


class TransactionalSandbox(SandboxSqliteSessionDB):
    """Real isolated SQLite plus the modern host's transaction/CAS surface."""

    def __init__(self, db_path):
        super().__init__(db_path)
        self.conn.execute("CREATE UNIQUE INDEX title_unique ON sessions(title)")
        self.write_attempts = 0
        self.transaction_log = []
        self.before_write = None

    def _execute_write(self, fn):
        self.write_attempts += 1
        if self.before_write is not None:
            callback, self.before_write = self.before_write, None
            callback()
        self.conn.execute("BEGIN IMMEDIATE")
        self.transaction_log.append("BEGIN")
        try:
            result = fn(self.conn)
            assert self.conn.in_transaction, "the host, not the callback, commits"
            self.conn.execute("COMMIT")
            self.transaction_log.append("COMMIT")
            return result
        except BaseException:
            self.conn.execute("ROLLBACK")
            self.transaction_log.append("ROLLBACK")
            raise

    def set_auto_title(self, session_id, title, source="llm"):
        return self._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET title = ?, title_source = ? "
                "WHERE id = ? AND (title IS NULL OR title = '' "
                "OR title_source = 'derived')",
                (title, source, session_id),
            ).rowcount > 0
        )

    def add(self, session_id, title=None, source=None):
        self.create_session(session_id)
        self.conn.execute(
            "UPDATE sessions SET title = ?, title_source = ? WHERE id = ?",
            (title, source, session_id),
        )
        self.append_message(
            session_id, "user", "继续梳理供应链运输规划实验，保留必要标识符。"
        )
        self.append_message(session_id, "assistant", "按实验目标继续整理。")


class SyntheticLlm:
    def __init__(self, action="rename", title=CHINESE_TITLE):
        self.action = action
        self.title = title
        self.calls = []
        self.before_response = None

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.before_response is not None:
            callback, self.before_response = self.before_response, None
            callback()
        return SimpleNamespace(
            text=json.dumps({"action": self.action, "title": self.title}),
            usage={},
        )


@pytest.fixture(autouse=True)
def isolate_state(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(core, "get_hermes_home", lambda: tmp_path)


@pytest.fixture
def sandbox(tmp_path):
    db = TransactionalSandbox(tmp_path / "synthetic.db")
    try:
        yield db
    finally:
        db.close()


def make_titler(db, *, title=CHINESE_TITLE, action="rename", **cfg):
    llm = SyntheticLlm(action, title)
    titler = AutoTitler(
        SimpleNamespace(llm=llm),
        {**DEFAULTS, "rename_confirmations": 0, **cfg},
        db=db,
    )
    return titler, llm


@pytest.mark.parametrize("title", TITLES)
@pytest.mark.parametrize("style", ["concise", "complete"])
def test_preparation_keeps_full_chinese_and_identifier(sandbox, title, style):
    titler, _ = make_titler(sandbox, title_style=style, max_display_width=32)
    assert display_width(title) > 40
    assert titler._prepare_candidate(title) == title


@pytest.mark.parametrize("title", TITLES)
def test_old_display_and_character_config_do_not_crop(sandbox, title):
    titler, _ = make_titler(
        sandbox, max_display_width=10, max_title_length=10
    )
    assert titler._prepare_candidate(title) == title
    sandbox.add("target")
    assert titler._write(sandbox, "target", title) == title
    assert sandbox.get_session_title("target") == title


@pytest.mark.parametrize("length", [100, 101])
def test_storage_limit_applies_after_cleaning(sandbox, length):
    title = "A" * length
    wrapped = f'  Title: "{title}!"  \n'
    titler, _ = make_titler(sandbox)
    assert titler._clean_title(wrapped) == title
    assert titler._prepare_candidate(wrapped) == (title if length == 100 else None)
    sandbox.add("target")
    assert titler._write(sandbox, "target", wrapped) == (
        title if length == 100 else None
    )
    assert sandbox.get_session_title("target") == (title if length == 100 else None)
    assert sandbox.write_attempts == (1 if length == 100 else 0)


@pytest.mark.parametrize("title", ["", "  \n\t", 'Title: "..."'])
def test_empty_cleaned_title_has_no_write(sandbox, title):
    sandbox.add("target")
    titler, _ = make_titler(sandbox)
    assert titler._prepare_candidate(title) is None
    assert titler._write(sandbox, "target", title) is None
    assert sandbox.get_session_title("target") is None
    assert sandbox.write_attempts == 0


@pytest.mark.parametrize("title", TITLES)
@pytest.mark.parametrize(
    ("old_title", "source"), [(None, None), ("Temporary title", "derived"), ("Old title", "llm")]
)
def test_real_sqlite_write_preserves_full_title_and_llm_provenance(
    sandbox, title, old_title, source
):
    sandbox.add("target", old_title, source)
    titler, _ = make_titler(sandbox, title=title)
    assert titler._write(sandbox, "target", title, expected_title=old_title) == title
    assert sandbox.get_session_title("target") == title
    assert sandbox.get_session_title_source("target") == "llm"
    assert sandbox.write_attempts == 1
    assert sandbox.transaction_log == ["BEGIN", "COMMIT"]


@pytest.mark.parametrize("title", TITLES)
@pytest.mark.parametrize("action", ["keep", "rename"])
def test_accurate_old_title_stays_and_performs_zero_writes(sandbox, title, action):
    sandbox.add("target", title, "llm")
    titler, llm = make_titler(sandbox, title=title, action=action)
    before = sandbox.conn.total_changes
    assert titler.evaluate("target", force=True) == {"action": "keep"}
    assert sandbox.get_session_title("target") == title
    assert sandbox.get_session_title_source("target") == "llm"
    assert sandbox.write_attempts == 0
    assert sandbox.conn.total_changes == before
    assert len(llm.calls) == 1


@pytest.mark.parametrize("source", ["user", None])
def test_manual_and_legacy_titles_are_protected(sandbox, source):
    sandbox.add("target", "Authoritative title", source)
    titler, llm = make_titler(sandbox)
    assert titler._write(sandbox, "target", CHINESE_TITLE) is None
    result = titler.evaluate("target", force=True)
    assert result["action"] == "skipped"
    assert sandbox.get_session_title("target") == "Authoritative title"
    assert sandbox.get_session_title_source("target") == source
    assert sandbox.write_attempts == 0
    assert llm.calls == []


def test_manual_title_set_during_model_call_is_not_overwritten(sandbox):
    sandbox.add("target", "Old title", "llm")
    titler, llm = make_titler(sandbox)
    llm.before_response = lambda: sandbox.conn.execute(
        "UPDATE sessions SET title = ?, title_source = 'user' WHERE id = ?",
        ("Manual replacement", "target"),
    )
    assert titler.evaluate("target", force=True)["action"] == "skipped"
    assert sandbox.get_session_title("target") == "Manual replacement"
    assert sandbox.get_session_title_source("target") == "user"
    assert sandbox.write_attempts == 0


@pytest.mark.parametrize("source", [None, "derived", "llm"])
def test_manual_takeover_at_transaction_boundary_fails_without_suffix(sandbox, source):
    old = "Old title" if source is not None else None
    sandbox.add("target", old, source)
    titler, _ = make_titler(sandbox)
    sandbox.before_write = lambda: sandbox.conn.execute(
        "UPDATE sessions SET title = ?, title_source = 'user' WHERE id = ?",
        ("Manual replacement", "target"),
    )
    assert titler._write(sandbox, "target", CHINESE_TITLE, expected_title=old) is None
    assert sandbox.get_session_title("target") == "Manual replacement"
    assert sandbox.get_session_title_source("target") == "user"
    assert sandbox.write_attempts == 1
    assert sandbox.transaction_log == ["BEGIN", "COMMIT"]


def test_cas_rejects_external_llm_rename_without_suffix_retry(sandbox):
    sandbox.add("target", "Old title", "llm")
    titler, llm = make_titler(sandbox)
    llm.before_response = lambda: sandbox.conn.execute(
        "UPDATE sessions SET title = ? WHERE id = ?",
        ("External automatic title", "target"),
    )
    assert titler.evaluate("target", force=True)["action"] == "failed"
    assert sandbox.get_session_title("target") == "External automatic title"
    assert sandbox.get_session_title_source("target") == "llm"
    assert sandbox.write_attempts == 1
    assert sandbox.transaction_log == ["BEGIN", "COMMIT"]


def test_pending_then_approve_renames_with_full_title(sandbox):
    sandbox.add("target", "Old title", "llm")
    titler, llm = make_titler(sandbox, rename_confirmations=1)
    assert titler.evaluate("target", force=True) == {
        "action": "pending", "candidate": CHINESE_TITLE
    }
    assert titler._pending["target"]["title"] == CHINESE_TITLE
    assert sandbox.get_session_title("target") == "Old title"
    assert sandbox.write_attempts == 0
    llm.action, llm.title = "approve", ""
    assert titler.evaluate("target", force=True) == {
        "action": "renamed", "title": CHINESE_TITLE
    }
    assert sandbox.get_session_title("target") == CHINESE_TITLE
    assert sandbox.get_session_title_source("target") == "llm"
    assert sandbox.write_attempts == 1
    assert "target" not in titler._pending


def test_full_pending_title_still_requires_cooldown_then_approval(sandbox):
    sandbox.add("target", "Old title", "llm")
    titler, llm = make_titler(sandbox, rename_confirmations=1, min_interval_minutes=5)
    assert titler.evaluate("target") == {"action": "pending", "candidate": CHINESE_TITLE}
    llm.action, llm.title = "approve", ""
    assert titler.evaluate("target") == {"action": "throttled"}
    assert sandbox.get_session_title("target") == "Old title"
    assert titler._pending["target"]["title"] == CHINESE_TITLE
    assert sandbox.write_attempts == 0
    assert len(llm.calls) == 1

    titler._last_eval["target"] -= 301
    assert titler.evaluate("target") == {"action": "renamed", "title": CHINESE_TITLE}
    assert sandbox.get_session_title("target") == CHINESE_TITLE
    assert sandbox.write_attempts == 1
    assert len(llm.calls) == 2


def test_full_title_write_still_obeys_automatic_rename_limit(sandbox):
    sandbox.add("target", "Old title", "llm")
    titler, llm = make_titler(sandbox, max_renames_per_session=1)
    assert titler.evaluate("target", force=True) == {
        "action": "renamed", "title": CHINESE_TITLE
    }
    llm.title = IDENTIFIER_TITLE
    assert titler.evaluate("target", force=True) == {
        "action": "skipped", "reason": "max_renames_per_session reached",
        "renames": 1, "limit": 1,
    }
    assert sandbox.get_session_title("target") == CHINESE_TITLE
    assert sandbox.write_attempts == 1
    assert len(llm.calls) == 1


@pytest.mark.parametrize("source", [None, "derived", "llm"])
@pytest.mark.parametrize("title", [CHINESE_TITLE, IDENTIFIER_TITLE, "A" * 96])
def test_duplicate_suffix_keeps_the_entire_base(sandbox, source, title):
    sandbox.add("occupied", title, "llm")
    old = "Old title" if source is not None else None
    sandbox.add("target", old, source)
    titler, _ = make_titler(sandbox, max_display_width=10, max_title_length=10)
    expected = title + " (2)"
    assert len(expected) <= 100
    assert titler._write(sandbox, "target", title, expected_title=old) == expected
    assert sandbox.get_session_title("target") == expected
    assert sandbox.get_session_title_source("target") == "llm"
    assert sandbox.get_session_title("occupied") == title
    assert sandbox.write_attempts == 2
    assert sandbox.transaction_log == ["BEGIN", "ROLLBACK", "BEGIN", "COMMIT"]


@pytest.mark.parametrize("source", [None, "llm"])
@pytest.mark.parametrize("length", [97, 100])
def test_duplicate_that_cannot_fit_suffix_is_rejected_without_base_crop(
    sandbox, source, length
):
    title = "A" * length
    sandbox.add("occupied", title, "llm")
    old = "Old title" if source else None
    sandbox.add("target", old, source)
    titler, _ = make_titler(sandbox)
    assert titler._prepare_candidate(title) == title
    assert titler._write(sandbox, "target", title, expected_title=old) is None
    assert sandbox.get_session_title("target") == old
    assert sandbox.get_session_title_source("target") == source
    assert sandbox.get_session_title("occupied") == title
    assert sandbox.write_attempts == 1
    assert sandbox.transaction_log == ["BEGIN", "ROLLBACK"]


@pytest.mark.parametrize("source", [None, "llm"])
def test_multiple_duplicate_suffixes_are_tried_in_order(sandbox, source):
    title = IDENTIFIER_TITLE
    for i, occupied in enumerate([title, title + " (2)", title + " (3)"]):
        sandbox.add(f"occupied-{i}", occupied, "llm")
    old = "Old title" if source else None
    sandbox.add("target", old, source)
    titler, _ = make_titler(sandbox)
    assert titler._write(sandbox, "target", title, expected_title=old) == title + " (4)"
    assert sandbox.get_session_title("target") == title + " (4)"
    assert sandbox.write_attempts == 4
    assert sandbox.transaction_log == ["BEGIN", "ROLLBACK"] * 3 + ["BEGIN", "COMMIT"]


@pytest.mark.parametrize("source", [None, "llm"])
@pytest.mark.parametrize("length", [95, 96])
def test_two_digit_suffix_obeys_storage_limit_without_shortening(sandbox, source, length):
    title = "A" * length
    sandbox.add("occupied-base", title, "llm")
    for i in range(2, 10):
        sandbox.add(f"occupied-{i}", f"{title} ({i})", "llm")
    old = "Old title" if source else None
    sandbox.add("target", old, source)
    titler, _ = make_titler(sandbox)

    expected = title + " (10)" if length == 95 else None
    assert len(title + " (10)") == (100 if length == 95 else 101)
    assert titler._write(sandbox, "target", title, expected_title=old) == expected
    assert sandbox.get_session_title("target") == (expected if expected else old)
    assert sandbox.get_session_title_source("target") == ("llm" if expected else source)
    assert sandbox.write_attempts == (10 if expected else 9)
    assert sandbox.transaction_log == ["BEGIN", "ROLLBACK"] * 9 + (
        ["BEGIN", "COMMIT"] if expected else []
    )


@pytest.mark.parametrize("final_available", [True, False])
def test_all_suffixes_through_19_are_attempted_without_cropping(sandbox, final_available):
    title = IDENTIFIER_TITLE
    sandbox.add("occupied-base", title, "llm")
    for i in range(2, 19 if final_available else 20):
        sandbox.add(f"occupied-{i}", f"{title} ({i})", "llm")
    sandbox.add("target")
    titler, _ = make_titler(sandbox)
    expected = f"{title} (19)" if final_available else None
    assert titler._write(sandbox, "target", title) == expected
    assert sandbox.get_session_title("target") == expected
    assert sandbox.write_attempts == 19
    assert not sandbox.conn.in_transaction


def test_non_unique_sqlite_error_does_not_trigger_suffixes(sandbox, monkeypatch):
    sandbox.add("target", "Old title", "llm")
    titler, _ = make_titler(sandbox)
    attempts = []

    def broken_writer(fn):
        attempts.append(fn)
        raise sqlite3.OperationalError("synthetic disk write error")

    monkeypatch.setattr(sandbox, "_execute_write", broken_writer)
    assert titler._write(sandbox, "target", CHINESE_TITLE, expected_title="Old title") is None
    assert sandbox.get_session_title("target") == "Old title"
    assert len(attempts) == 1


@pytest.mark.parametrize("source", [None, "llm"])
def test_non_unique_integrity_constraint_does_not_trigger_suffixes(sandbox, source):
    old = "Old title" if source else None
    sandbox.add("target", old, source)
    sandbox.conn.execute(
        "CREATE TRIGGER deny_title BEFORE UPDATE OF title ON sessions "
        "BEGIN SELECT RAISE(ABORT, 'synthetic unrelated constraint'); END"
    )
    titler, _ = make_titler(sandbox)
    assert titler._write(sandbox, "target", CHINESE_TITLE, expected_title=old) is None
    assert sandbox.get_session_title("target") == old
    assert sandbox.get_session_title_source("target") == source
    assert sandbox.write_attempts == 1
    assert sandbox.transaction_log == ["BEGIN", "ROLLBACK"]


@pytest.mark.parametrize("source", [None, "llm"])
def test_unrelated_value_error_does_not_trigger_suffixes(sandbox, monkeypatch, source):
    old = "Old title" if source else None
    sandbox.add("target", old, source)
    attempts = []

    def broken_writer(fn):
        attempts.append(fn)
        raise ValueError("synthetic unrelated validation error")

    monkeypatch.setattr(sandbox, "_execute_write", broken_writer)
    titler, _ = make_titler(sandbox)
    assert titler._write(sandbox, "target", CHINESE_TITLE, expected_title=old) is None
    assert sandbox.get_session_title("target") == old
    assert len(attempts) == 1


@pytest.mark.parametrize("source", [None, "llm"])
def test_audit_table_unique_failure_is_not_a_title_collision(sandbox, source):
    old = "Old title" if source else None
    sandbox.add("target", old, source)
    sandbox.conn.execute("CREATE TABLE audit (value TEXT UNIQUE)")
    sandbox.conn.execute("INSERT INTO audit (value) VALUES ('occupied')")
    sandbox.conn.execute(
        "CREATE TRIGGER audit_title BEFORE UPDATE OF title ON sessions "
        "BEGIN INSERT INTO audit (value) VALUES ('occupied'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match=r"^UNIQUE constraint failed: audit\.value$"):
        sandbox.conn.execute("UPDATE sessions SET title = 'probe' WHERE id = 'target'")

    titler, _ = make_titler(sandbox)
    assert titler._write(sandbox, "target", CHINESE_TITLE, expected_title=old) is None
    assert sandbox.get_session_title("target") == old
    assert sandbox.get_session_title_source("target") == source
    assert sandbox.write_attempts == 1
    assert sandbox.transaction_log == ["BEGIN", "ROLLBACK"]
    assert [tuple(row) for row in sandbox.conn.execute("SELECT value FROM audit")] == [
        ("occupied",)
    ]


@pytest.mark.parametrize("source", [None, "llm"])
def test_session_id_unique_failure_is_not_a_title_collision(sandbox, source):
    old = "Old title" if source else None
    sandbox.add("occupied-id", "Another session", "llm")
    sandbox.add("target", old, source)
    sandbox.conn.execute(
        "CREATE TRIGGER change_session_id BEFORE UPDATE OF title ON sessions "
        "BEGIN UPDATE sessions SET id = 'occupied-id' WHERE id = NEW.id; END"
    )
    with pytest.raises(sqlite3.IntegrityError, match=r"^UNIQUE constraint failed: sessions\.id$"):
        sandbox.conn.execute("UPDATE sessions SET title = 'probe' WHERE id = 'target'")

    titler, _ = make_titler(sandbox)
    assert titler._write(sandbox, "target", CHINESE_TITLE, expected_title=old) is None
    assert sandbox.get_session_title("target") == old
    assert sandbox.get_session_title_source("target") == source
    assert sandbox.get_session_title("occupied-id") == "Another session"
    assert sandbox.write_attempts == 1
    assert sandbox.transaction_log == ["BEGIN", "ROLLBACK"]


def test_title_source_unique_failure_is_not_a_title_collision(sandbox):
    sandbox.add("occupied", "Another session", "llm")
    sandbox.add("target")
    sandbox.conn.execute("CREATE UNIQUE INDEX unique_source ON sessions(title_source)")
    with pytest.raises(sqlite3.IntegrityError, match=r"^UNIQUE constraint failed: sessions\.title_source$"):
        sandbox.conn.execute("UPDATE sessions SET title_source = 'llm' WHERE id = 'target'")

    titler, _ = make_titler(sandbox)
    assert titler._write(sandbox, "target", CHINESE_TITLE) is None
    assert sandbox.get_session_title("target") is None
    assert sandbox.get_session_title_source("target") is None
    assert sandbox.write_attempts == 1
    assert sandbox.transaction_log == ["BEGIN", "ROLLBACK"]


@pytest.mark.parametrize("source", [None, "llm"])
@pytest.mark.parametrize("message", [
    "resource already in use",
    "Title 'other' is already in use by session occupied",
])
def test_other_resource_or_other_title_value_error_does_not_retry(
    sandbox, monkeypatch, source, message
):
    old = "Old title" if source else None
    sandbox.add("target", old, source)
    attempts = []

    def broken_writer(fn):
        attempts.append(fn)
        raise ValueError(message)

    monkeypatch.setattr(sandbox, "_execute_write", broken_writer)
    titler, _ = make_titler(sandbox)
    assert titler._write(sandbox, "target", CHINESE_TITLE, expected_title=old) is None
    assert sandbox.get_session_title("target") == old
    assert sandbox.get_session_title_source("target") == source
    assert len(attempts) == 1
