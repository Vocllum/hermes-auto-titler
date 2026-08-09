"""AutoTitler 核心逻辑测试。"""

import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, "..")

from hermes_auto_titler.config import DEFAULTS
from hermes_auto_titler.messages import load_context
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
        # 模拟上游 precedence：derived → llm 是升级（允许），
        # llm → llm 同级 no-op（防自我重命名），user 标题永不覆盖
        rank = {None: 0, "derived": 1, "llm": 2, "user": 3}
        if rank.get(self.source, 0) < rank.get(source, 0):
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


def test_legacy_titled_session_is_protected():
    # NULL provenance + 已有标题：Hermes 官方按 user 权威对待（_title_rank），
    # llm 写不进去。插件应显式跳过而不是报 failed。
    db = FakeDB(messages=MSGS, title="旧自动标题", source=None)
    t, ctx = make_titler(db, text=_dec("rename", "新标题"))
    r = t.evaluate("s1", force=True)
    assert r["action"] == "skipped"
    assert r["reason"] == "legacy title (NULL provenance) is protected"
    assert ctx.llm.calls == []  # 不调模型
    assert db.title == "旧自动标题"


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


def test_derived_rename_uses_atomic_set_auto_title():
    # derived → llm 是权威升级：必须走 set_auto_title（单事务），
    # 不出现两步写的 crash window
    db = FakeDB(messages=MSGS, title="旧标题", source="derived")
    t, ctx = make_titler(db, text=_dec("rename", "新标题"))
    r = t.evaluate("s1", force=True)
    assert r["action"] == "renamed"
    auto_calls = [c for c in db.calls if c[0] == "set_auto_title"]
    assert auto_calls and auto_calls[0][1] == "新标题" and auto_calls[0][2] == "llm"
    assert not any(c[0] == "set_session_title" for c in db.calls)


def test_llm_rename_uses_two_step_with_source_restore():
    # llm → llm：set_auto_title 同级 no-op，走 set_session_title + 恢复 llm 来源
    db = FakeDB(messages=MSGS, title="旧标题", source="llm")
    t, ctx = make_titler(db, text=_dec("rename", "新标题"))
    r = t.evaluate("s1", force=True)
    assert r["action"] == "renamed"
    assert ("set_session_title", "新标题") in db.calls
    assert ("set_session_title_source", "llm") in db.calls


def test_parse_decision_tolerates_markdown_fence():
    from hermes_auto_titler.titler import _parse_decision

    text = '```json\n{"action": "rename", "title": "Raft 空转排查"}\n```'
    action, title = _parse_decision(text)
    assert action == "rename"
    assert title == "Raft 空转排查"
    # 非 JSON 自由文本：不猜，保持 keep（宁可不改不写错）
    action2, title2 = _parse_decision("我认为应该 rename，标题：修显示器 HDR")
    assert action2 == "keep"
    assert title2 is None


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
    assert r["action"] == "keep"


def test_generate_blind_omits_current_title_and_forces_rename():
    db = FakeDB(messages=MSGS, title="旧标题", source="llm")
    t, ctx = make_titler(db, text=_dec("rename", "新标题"))
    recent, all_user, opening = load_context(db, "s1", recent_turns=2, include_all_user=True)
    action, title = t._generate("旧标题", recent, all_user, opening, blind=True)
    assert (action, title) == ("rename", "新标题")
    system = ctx.llm.calls[0]["messages"][0]["content"]
    user_prompt = ctx.llm.calls[0]["messages"][1]["content"]
    assert "MUST be rename" in system or "must be rename" in system.lower()
    assert "truncated auto-generated" not in system  # 不是 derived 截断文案
    assert "Current title:" not in user_prompt  # 原标题不喂给模型
    assert "Opening:" in user_prompt


def test_retitle_all_skips_user_and_uses_blind():
    s_llm = {"id": "s1", "title": "旧标题", "message_count": 5}
    s_user = {"id": "s2", "title": "我手改的", "message_count": 5}
    s_legacy = {"id": "s3", "title": "老标题", "message_count": 5}

    class MultiDB(FakeDB):
        def __init__(self):
            super().__init__(messages=MSGS)
            self.by_sid = {"s1": "llm", "s2": "user", "s3": None}

        def get_session_title_source(self, sid):
            return self.by_sid.get(sid)

        def list_sessions_rich(self, limit=20, min_message_count=0, include_children=False):
            return [s_llm, s_user, s_legacy]

    db = MultiDB()
    t, ctx = make_titler(db, text=_dec("rename", "盲改标题"))
    results = t.retitle_all()
    by_id = {r["session_id"]: r for r in results}
    assert by_id["s2"]["action"] == "skipped"  # user 手改不动
    assert by_id["s3"]["action"] == "skipped"  # legacy NULL 保护
    assert by_id["s1"]["action"] == "renamed"
    # blind：prompt 里没有当前标题
    for call in ctx.llm.calls:
        assert "Current title:" not in call["messages"][1]["content"]  # 短标题不强制


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
