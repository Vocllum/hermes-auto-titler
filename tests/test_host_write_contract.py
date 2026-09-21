"""Host write-path contract: the atomic CAS branch and its legacy fallback.

``_write`` picks between two host generations at runtime:

* modern host  → ``db._execute_write(cb)`` runs the plugin's ``_atomic_llm_cas``
  inside the host's single-writer transaction (the llm→llm rename path);
* legacy host  → no ``_execute_write``, so the plugin falls back to a strictly
  two-step fail-closed write (``set_session_title`` → readback →
  ``set_session_title_source``).

The CAS callback is raw SQL executed against the real host connection, so these
tests drive it against a real ``sqlite3`` database with the same ``sessions``
schema the host uses — not a mock of ``conn``.

``RecordingHost`` records every host attribute the plugin resolves while
evaluating. The suite's fakes must provide all of them; when the plugin gains or
renames a host call, ``test_host_stub_covers_every_host_api_the_plugin_calls``
goes red instead of the plugin silently breaking against a real ``SessionDB``.

# 纪律：新增 self.db.* 调用点必须同批带一条执行得到它的测试。
# 契约按调用点录制，未被测试走到的新调用点会静默退回盲区
# （原子 CAS 分支就是这样在 250 条旧测试下零覆盖）。
"""

import sqlite3
import sys
from types import SimpleNamespace

from hermes_auto_titler.config import DEFAULTS
from hermes_auto_titler.titler import AutoTitler

MSGS = [
    {"role": "user", "content": "帮我排查后台任务重复触发"},
    {"role": "assistant", "content": "已定位到重复事件"},
]


class RecordingHost:
    """Proxy recording every host attribute the plugin resolves on ``db``."""

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "accessed", set())

    def __getattr__(self, name):
        self.accessed.add(name)
        return getattr(self._inner, name)


class RealSqliteHost:
    """Host surface backed by a real ``sqlite3`` connection.

    ``_execute_write`` is a faithful model of the host's ``SessionDB._execute_write``
    (``hermes_state.py:940``): it issues its own ``BEGIN IMMEDIATE``, runs
    ``fn(conn)``, and commits *itself* — the host docstring is explicit that
    "commit is handled here (callers must not commit)". The callback must not
    commit, because the host would then see no active transaction and its own
    ``commit()``/``rollback()`` would no longer bracket the write.

    ``fn`` must also stay idempotent under retry: the host replays the whole
    callback when it hits a lock, so the CAS is a single idempotent UPDATE.

    ``modern=False`` models a legacy host that genuinely lacks the attribute —
    the plugin resolves it with ``getattr(db, "_execute_write", None)``, so the
    class must not define it at all (see ``__getattr__`` below).
    """

    TITLE_SOURCE_DERIVED = "derived"
    TITLE_SOURCE_LLM = "llm"
    TITLE_SOURCE_USER = "user"
    MAX_TITLE_LENGTH = 100

    def __init__(self, *, modern=True):
        # isolation_level=None mirrors the host writer connection
        # (hermes_state.py _open_writer_conn): autocommit, so the only
        # transaction in play is the one _execute_write opens explicitly.
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE sessions ("
            " id TEXT PRIMARY KEY, title TEXT, title_source TEXT)"
        )
        self.modern = modern
        self.writes = 0
        if modern:
            # Only a modern host exposes _execute_write; a legacy one must not,
            # so the plugin's getattr(db, "_execute_write", None) resolves to
            # None and it takes the fallback path.
            self._execute_write = self._execute_write_modern

    def _execute_write_modern(self, fn):
        # Host contract (hermes_state.py:977-987): BEGIN IMMEDIATE here,
        # commit here, rollback here on failure. The caller only runs SQL.
        self.writes += 1
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            result = fn(self.conn)
            if not self.conn.in_transaction:
                raise AssertionError(
                    "plugin callback committed inside _execute_write — the host "
                    "owns the commit; a callback that commits itself leaves no "
                    "transaction for the host to commit or roll back"
                )
            self.conn.commit()
            return result
        except BaseException:
            try:
                self.conn.rollback()
            except Exception:
                pass
            raise

    def __getattr__(self, name):
        # Reached only for attributes the instance/class does not define; a
        # legacy host must not resolve _execute_write at all.
        if name == "_execute_write":
            raise AttributeError("legacy host has no _execute_write")
        raise AttributeError(name)

    def add_session(self, sid, title, source):
        # The host's own write helpers commit on the host's behalf (the DB is
        # autocommit here); the plugin must never commit itself.
        self.conn.execute(
            "INSERT INTO sessions (id, title, title_source) VALUES (?, ?, ?)",
            (sid, title, source),
        )
        self.conn.commit()

    # -- host API the plugin calls -------------------------------------
    def get_messages_as_conversation(self, session_id, include_ancestors=False):
        return MSGS

    def get_session_title(self, sid):
        row = self.conn.execute(
            "SELECT title FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
        return row["title"] if row else None

    def get_session_title_source(self, sid):
        row = self.conn.execute(
            "SELECT title_source FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
        return row["title_source"] if row else None

    def set_auto_title(self, sid, title, *, source):
        self.writes += 1
        cur = self.conn.execute(
            "UPDATE sessions SET title = ?, title_source = ? "
            "WHERE id = ? AND (title IS NULL OR title_source IN ('derived', 'llm'))",
            (title, source, sid),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def set_session_title(self, sid, title):
        self.writes += 1
        cur = self.conn.execute(
            "UPDATE sessions SET title = ?, title_source = 'user' WHERE id = ?",
            (title, sid),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def set_session_title_source(self, sid, source):
        self.writes += 1
        self.conn.execute(
            "UPDATE sessions SET title_source = ? WHERE id = ?", (source, sid)
        )
        self.conn.commit()

    def list_sessions_rich(self, limit=20, min_message_count=0, include_children=False):
        cur = self.conn.execute(
            "SELECT id, title, title_source FROM sessions ORDER BY id LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]

    def record_auxiliary_usage(self, session_id, task, **kwargs):
        # Best-effort usage accounting; recorded so the plugin can be verified
        # to call it through the real host surface.
        self.usage = getattr(self, "usage", [])
        self.usage.append({"session_id": session_id, "task": task, **kwargs})


class FakeLlm:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text=self.text, model="m", provider="p", usage=None)


def _dec(action, title=""):
    import json

    return json.dumps({"action": action, "title": title})


def _titler(db, text, cfg=None):
    ctx = SimpleNamespace(llm=FakeLlm(text))
    # rename_confirmations=0: a rename candidate applies in the same evaluate()
    # call, so these tests exercise the write path directly (the two-turn
    # review protocol has its own coverage elsewhere).
    return AutoTitler(
        ctx, {**DEFAULTS, "rename_confirmations": 0, **(cfg or {})}, db=db
    )


def test_modern_host_llm_rename_lands_through_atomic_cas():
    # llm → llm rename on a modern host must go through the single-transaction
    # CAS, leaving title and source consistent with no crash window.
    host = RealSqliteHost(modern=True)
    host.add_session("s1", "旧标题", "llm")
    t = _titler(host, _dec("rename", "新标题"))

    r = t.evaluate("s1", force=True)

    assert r["action"] == "renamed"
    assert host.get_session_title("s1") == "新标题"
    assert host.get_session_title_source("s1") == "llm"
    # set_auto_title is the None/derived path only; an llm→llm rename must not
    # touch it (it would stamp the wrong provenance).
    assert host.writes == 1


def test_cas_aborts_when_title_changed_under_it():
    # TOCTOU: the row's title no longer matches the snapshot the CAS compares
    # against, so the first UPDATE matches zero rows and the plugin fails
    # closed. The retry re-snapshots against the new value (it must never
    # compare against a stale one).
    host = RealSqliteHost(modern=True)
    host.add_session("s1", "旧标题", "llm")
    t = _titler(host, _dec("rename", "新标题"))

    cas_attempts = []
    real_exec_write = host._execute_write

    def racing_exec_write(fn):
        cas_attempts.clear()

        def spy(conn):
            conn.set_trace_callback(
                lambda sql: cas_attempts.append(sql)
                if "UPDATE sessions" in sql else None
            )
            try:
                return fn(conn)
            finally:
                conn.set_trace_callback(None)

        # A competing writer lands between the plugin's snapshot and its CAS.
        host.conn.execute(
            "UPDATE sessions SET title = '外部写入', title_source = 'llm' WHERE id = 's1'"
        )
        host.conn.commit()
        return real_exec_write(spy)

    host._execute_write = racing_exec_write
    r = t._write(host, "s1", "新标题")

    # The first CAS compared against the stale snapshot and matched 0 rows; the
    # retry then re-snapshotted against 外部写入.
    assert len(cas_attempts) >= 1
    assert host.get_session_title("s1") != "新标题"  # never blindly overwrote
    assert host.get_session_title_source("s1") == "llm"


def test_cas_aborts_when_source_flipped_to_user_under_it():
    # user authority wins even mid-CAS: the WHERE clause pins title_source so a
    # flip to 'user' matches zero rows.
    host = RealSqliteHost(modern=True)
    host.add_session("s1", "旧标题", "llm")
    t = _titler(host, _dec("rename", "新标题"))
    t._pending["s1"] = {"title": "新标题"}

    host.conn.execute(
        "UPDATE sessions SET title = '用户手改', title_source = 'user' WHERE id = 's1'"
    )
    host.conn.commit()

    r = t._write(host, "s1", "新标题")

    assert r is None
    assert host.get_session_title("s1") == "用户手改"
    assert host.get_session_title_source("s1") == "user"


def test_legacy_host_falls_back_to_two_step_fail_closed_write():
    # No _execute_write: the two-step path must still write and recover llm
    # provenance, and must refuse when the readback disagrees.
    host = RealSqliteHost(modern=False)
    host.add_session("s1", "旧标题", "llm")
    t = _titler(host, _dec("rename", "新标题"))

    r = t._write(host, "s1", "新标题")

    assert r == "新标题"
    assert host.get_session_title("s1") == "新标题"
    assert host.get_session_title_source("s1") == "llm"
    # write (set_session_title) + source recovery; readback is a plain read
    assert host.writes == 2


def test_legacy_fallback_aborts_when_readback_disagrees():
    host = RealSqliteHost(modern=False)
    host.add_session("s1", "旧标题", "llm")
    t = _titler(host, _dec("rename", "新标题"))

    # Another writer lands between set_session_title and the readback.
    real_set = host.set_session_title

    def racing_set(sid, title):
        ok = real_set(sid, title)
        host.conn.execute(
            "UPDATE sessions SET title = '外部写入' WHERE id = ?", (sid,)
        )
        host.conn.commit()
        return ok

    host.set_session_title = racing_set
    r = t._write(host, "s1", "新标题")

    assert r is None
    assert host.get_session_title("s1") == "外部写入"


def test_untitled_session_uses_set_auto_title_not_cas():
    # None/derived source must take the host's single-transaction set_auto_title
    # (title + source written atomically), never the CAS.
    host = RealSqliteHost(modern=True)
    host.add_session("s1", None, None)
    t = _titler(host, _dec("rename", "新标题"))

    r = t._write(host, "s1", "新标题")

    assert r == "新标题"
    assert host.get_session_title("s1") == "新标题"
    assert host.get_session_title_source("s1") == "llm"
    assert host.writes == 1  # set_auto_title only — the CAS never ran


def test_cas_callback_never_commits_inside_host_transaction():
    # The host owns the transaction: it does BEGIN IMMEDIATE, runs fn(conn),
    # then commits itself ("callers must not commit", hermes_state.py:940).
    # A plugin callback that commits leaves the host with no transaction to
    # commit or roll back — its rollback path silently dies and the write is
    # already durable, so a later failure can no longer undo it.
    host = RealSqliteHost(modern=True)
    host.add_session("s1", "旧标题", "llm")

    seen = {}
    real_exec_write = host._execute_write

    def probe_exec_write(fn):
        cas_attempts.clear()

        def spy(conn):
            # snapshot BEFORE running the plugin callback, while the host's
            # BEGIN IMMEDIATE transaction is still open
            seen["in_tx_inside"] = host.conn.in_transaction
            result = fn(conn)
            seen["in_tx_after_callback"] = host.conn.in_transaction
            return result

        return real_exec_write(spy)

    cas_attempts = []
    host._execute_write = probe_exec_write
    t = _titler(host, _dec("rename", "新标题"))
    r = t._write(host, "s1", "新标题")

    assert r == "新标题"
    # The host's BEGIN IMMEDIATE was open when the callback started and still
    # open when it returned — the plugin never committed on its own, so the
    # host's commit/rollback still brackets the write.
    assert seen["in_tx_inside"] is True
    assert seen["in_tx_after_callback"] is True
    # The host's own commit then closed the transaction.
    assert host.conn.in_transaction is False


def test_host_stub_covers_every_host_api_the_plugin_calls():
    # Tripwire: the host stub must model every host attribute the plugin
    # resolves. Recorded at runtime (not read from source), so it reddens on
    # plugin-side drift — a new or renamed host call — instead of the plugin
    # failing silently against a real SessionDB.
    expected_host_api = {
        "get_messages_as_conversation",
        "get_session_title",
        "get_session_title_source",
        "set_auto_title",
        "set_session_title",
        "set_session_title_source",
        "_execute_write",
        "list_sessions_rich",
        "record_auxiliary_usage",
    }

    accessed = set()
    for modern in (True, False):
        host = RealSqliteHost(modern=modern)
        host.add_session("s1", "旧标题", "llm")
        rec = RecordingHost(host)
        t = _titler(rec, _dec("rename", "新标题"))
        t.evaluate("s1", force=True)          # llm → llm rename (CAS path)
        t._write(rec, "s1", "新标题")
        t.retitle_all(dry_run=True)            # list_sessions_rich path
        # None/derived source → set_auto_title path
        host.add_session("s2", None, None)
        t._write(rec, "s2", "第二标题")
        accessed |= rec.accessed

    host_api_used = {a for a in accessed if not a.startswith("__")}
    uncovered = host_api_used - expected_host_api
    assert not uncovered, (
        "plugin resolved host attributes the stub does not model: "
        f"{sorted(uncovered)} — add them to the host stub (see tests/conftest.py)"
    )
    # Every modelled attribute must actually have been resolved while driving
    # the paths above — a declared-but-unused entry is dead weight in the stub.
    never_used = expected_host_api - host_api_used
    assert not never_used, (
        "stub models host attributes the plugin never resolves on these paths: "
        f"{sorted(never_used)} — drop them or drive the path that uses them"
    )
    # The stub must actually be reachable for the write paths it models.
    host = RealSqliteHost(modern=True)
    for name in expected_host_api:
        assert hasattr(host, name), f"host stub missing {name}"
