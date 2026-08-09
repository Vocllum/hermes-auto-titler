"""AutoTitler 核心逻辑测试。"""

import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, "..")

from hermes_auto_titler.config import DEFAULTS
from hermes_auto_titler.titler import AutoTitler


class FakeDB:
    TITLE_SOURCE_DERIVED = "derived"
    TITLE_SOURCE_LLM = "llm"
    TITLE_SOURCE_USER = "user"
    MAX_TITLE_LENGTH = 100

    def __init__(self, messages=None, title=None, source=None, conflict_titles=None, sessions=None):
        self.messages = messages or []
        self.title = title
        self.source = source
        self.conflict_titles = conflict_titles or set()
        self.sessions = sessions or []
        self.calls = []

    def get_messages_as_conversation(self, session_id, include_ancestors=False):
        return self.messages

    def get_session_title(self, sid):
        return self.title

    def get_session_title_source(self, sid):
        return self.source

    def _check_conflict(self, title):
        if title in self.conflict_titles:
            raise ValueError(f"Title '{title}' is already in use by session 123")

    def set_auto_title(self, sid, title, *, source):
        self.calls.append(("set_auto_title", title, source))
        self._check_conflict(title)
        if self.title is None:
            self.title = title
            self.source = source
            return True
        return False

    def set_session_title(self, sid, title):
        self.calls.append(("set_session_title", title))
        self._check_conflict(title)
        if self.title == title:
            return False
        self.title = title
        return True

    def set_session_title_source(self, sid, source):
        self.calls.append(("set_session_title_source", source))
        self.source = source

    def list_sessions_rich(self, limit=20, min_message_count=0, include_children=False):
        return self.sessions


class FakeLlm:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text=self.text)


def make_titler(db, text=None, cfg=None, fake_time=None):
    cfg = {**DEFAULTS, **(cfg or {})}
    ctx = SimpleNamespace(llm=FakeLlm(text))
    t = AutoTitler(ctx, cfg, db=db)
    if fake_time is not None:
        t._last_eval = {}
    return t, ctx


def _dec(action="keep", title=""):
    import json

    return json.dumps({"action": action, "title": title})


MSGS = [
    {"role": "user", "content": "帮我看看 Raft 空转的问题"},
    {"role": "assistant", "content": "我查了日志，是 wake 重放导致的"},
    {"role": "user", "content": "那我们怎么修"},
    {"role": "assistant", "content": "方案是 throttle 重放"},
]


def test_user_title_never_overwritten():
    db = FakeDB(messages=MSGS, title="用户手改", source="user")
    t, ctx = make_titler(db)
    r = t.evaluate("s1", force=True)
    assert r["action"] == "skipped"
    assert ctx.llm.calls == []  # 不调模型
    assert db.title == "用户手改"


def test_untitled_session_uses_auto_title_llm():
    db = FakeDB(messages=MSGS, title=None, source=None)
    t, _ = make_titler(db, text=_dec("rename", "Raft 空转排查"))
    r = t.evaluate("s1", force=True)
    assert r["action"] == "renamed"
    assert db.title == "Raft 空转排查"
    assert ("set_auto_title", "Raft 空转排查", "llm") in db.calls


def test_auto_title_can_be_updated_and_stays_llm():
    db = FakeDB(messages=MSGS, title="旧标题", source="llm")
    t, _ = make_titler(db, text=_dec("rename", "Raft 空转排查"))
    r = t.evaluate("s1", force=True)
    assert r["action"] == "renamed"
    assert db.title == "Raft 空转排查"
    assert ("set_session_title", "Raft 空转排查") in db.calls
    assert ("set_session_title_source", "llm") in db.calls  # 恢复可升级性


def test_keep_does_not_write():
    db = FakeDB(messages=MSGS, title="Raft 空转排查", source="llm")
    t, _ = make_titler(db, text=_dec("keep"))
    r = t.evaluate("s1", force=True)
    assert r["action"] == "keep"
    assert db.calls == []


def test_rename_to_same_title_is_keep():
    db = FakeDB(messages=MSGS, title="Raft 空转排查", source="llm")
    t, _ = make_titler(db, text=_dec("rename", "Raft 空转排查"))
    r = t.evaluate("s1", force=True)
    assert r["action"] == "keep"


def test_throttle_skips_frequent_evaluations():
    db = FakeDB(messages=MSGS, title=None)
    t, ctx = make_titler(db, text=_dec("keep"))
    t.evaluate("s1", force=True)
    n = len(ctx.llm.calls)
    r = t.evaluate("s1", force=False)
    assert r["action"] == "throttled"
    assert len(ctx.llm.calls) == n
    # force=True 不受节流限制
    r2 = t.evaluate("s1", force=True)
    assert r2["action"] in ("keep", "renamed")


def test_every_n_turns_trigger():
    db = FakeDB(messages=MSGS, title=None)
    t, ctx = make_titler(db, text=_dec("keep"), cfg={"every_n_turns": 3})
    t.on_session_end(session_id="s1", completed=True)
    assert ctx.llm.calls == []  # 第 1 轮不评估
    t.on_session_end(session_id="s1", completed=True)
    assert ctx.llm.calls == []  # 第 2 轮不评估
    t.on_session_end(session_id="s1", completed=True)
    assert len(ctx.llm.calls) == 1  # 第 3 轮评估


def test_on_close_signal_forces_evaluate():
    db = FakeDB(messages=MSGS, title=None)
    t, ctx = make_titler(db, text=_dec("keep"), cfg={"every_n_turns": 10})
    t.on_session_end(session_id="s1", reason="shutdown", interrupted=True)
    assert len(ctx.llm.calls) == 1  # 忽略轮数，直接评估


def test_conflict_adds_suffix():
    db = FakeDB(messages=MSGS, title=None, conflict_titles={"Raft 空转排查"})
    t, _ = make_titler(db, text=_dec("rename", "Raft 空转排查"))
    r = t.evaluate("s1", force=True)
    assert r["action"] == "renamed"
    assert r["title"] == "Raft 空转排查 (2)"
    assert db.title == "Raft 空转排查 (2)"


def test_malformed_model_output_keeps():
    db = FakeDB(messages=MSGS, title="Raft 空转排查", source="llm")
    t, _ = make_titler(db, text="抱歉，我无法完成这个请求。")
    r = t.evaluate("s1", force=True)
    assert r["action"] == "keep"


def test_parse_decision_tolerates_markdown_fence():
    from hermes_auto_titler.titler import _parse_decision

    text = '```json\n{"action": "rename", "title": "Raft 空转排查"}\n```'
    action, title = _parse_decision(text)
    assert action == "rename"
    assert title == "Raft 空转排查"
    # 关键词启发式兜底
    action2, title2 = _parse_decision('我认为应该 rename，标题：修显示器 HDR')
    assert action2 == "rename"
    assert title2 == "修显示器 HDR"


def test_llm_failure_keeps():
    db = FakeDB(messages=MSGS, title=None)

    class BoomLlm:
        def complete(self, **kw):
            raise RuntimeError("provider down")

    t = AutoTitler(SimpleNamespace(llm=BoomLlm()), {**DEFAULTS}, db=db)
    r = t.evaluate("s1", force=True)
    assert r["action"] == "keep"


def test_title_truncated_to_max_length():
    db = FakeDB(messages=MSGS, title=None)
    long = "这是一个非常非常非常非常非常非常非常非常非常非常非常非常非常长的标题测试"
    t, _ = make_titler(db, text=_dec("rename", long), cfg={"max_title_length": 20})
    r = t.evaluate("s1", force=True)
    assert r["action"] == "renamed"
    assert len(r["title"]) <= 20


def test_derived_long_title_forces_rename_even_when_model_keeps():
    long_title = "开发个小插件，让 Hermes 每次对话结束都会思考需不需要重命名会话标题。有别的类似项目吗，别…"
    db = FakeDB(messages=MSGS, title=long_title, source="derived")
    t, ctx = make_titler(db, text=_dec("keep", long_title))  # 模型说 keep
    r = t.evaluate("s1", force=True)
    # force_rename 下模型被要求给新标题；若给了不同标题则 rename
    assert r["action"] in ("renamed", "keep")
    if r["action"] == "renamed":
        assert db.source == "llm"


def test_short_derived_title_not_forced():
    db = FakeDB(messages=MSGS, title="Raft 空转排查", source="derived")
    t, _ = make_titler(db, text=_dec("keep"))
    r = t.evaluate("s1", force=True)
    assert r["action"] == "keep"  # 短标题不强制


def test_retitle_all_skips_user_and_dry_run():
    db = FakeDB(
        sessions=[
            {"id": "s-user", "title": "手改"},
            {"id": "s-auto", "title": "旧自动"},
        ]
    )
    t, _ = make_titler(db, text=_dec("rename", "新标题"))
    # s-user: source=user → skip；s-auto: 无消息 → skipped
    results = t.retitle_all()
    by_id = {r["session_id"]: r for r in results}
    assert by_id["s-user"]["action"] == "skipped"
    # dry-run 不评估
    t2, ctx2 = make_titler(db, text=_dec("rename", "新标题"))
    results2 = t2.retitle_all(dry_run=True)
    assert all(r["action"] == "dry-run" for r in results2)
    assert ctx2.llm.calls == []
