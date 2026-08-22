"""AutoTitler 核心逻辑测试。"""

import contextvars
import sys
import threading
import time
import types
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, "..")

from hermes_auto_titler.config import DEFAULTS
from hermes_auto_titler.messages import display_width, load_context, load_context_with_summary
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
        # 模拟「插件 set_session_title 之后、恢复来源之前用户 /title 抢先」的竞态
        self.override_after_set_session_title = None

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
        self.source = "user"  # 模拟上游：user 级写
        if self.override_after_set_session_title:
            self.title, self.source = self.override_after_set_session_title
        return True

    def set_session_title_source(self, sid, source):
        self.calls.append(("set_session_title_source", source))
        self.source = source

    def list_sessions_rich(self, limit=20, min_message_count=0, include_children=False):
        return self.sessions


class FakeLlm:
    def __init__(self, text, usage=None, model="fake-model", provider="fake-provider"):
        self.text = text
        self.usage = usage
        self.model = model
        self.provider = provider
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            text=self.text, model=self.model, provider=self.provider, usage=self.usage
        )


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


class RecordingThread:
    """同步替身：记录 target 但不真正启动，测试手动 run_now 驱动（确定性）。"""

    instances: "list[RecordingThread]" = []

    def __init__(self, target=None, args=(), kwargs=None, *, daemon=None, name=""):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        RecordingThread.instances.append(self)
        self.started = False

    def start(self):
        self.started = True

    def run_now(self):
        self.target(*self.args, **self.kwargs)


@pytest.fixture
def recording_threads(monkeypatch):
    RecordingThread.instances = []
    monkeypatch.setattr("hermes_auto_titler.titler.threading.Thread", RecordingThread)
    return RecordingThread


def run_recorded(rt) -> None:
    for th in list(rt.instances):
        if not getattr(th, "ran", False):
            th.ran = True
            th.run_now()


def _blocking_llm(entered, release):
    class BlockingLlm:
        def __init__(self):
            self.calls = []

        def complete(self, **kw):
            self.calls.append(kw)
            entered.set()
            release.wait(5)
            return SimpleNamespace(text=_dec("keep"))

    return BlockingLlm()


def _wait_inflight_clear(t, sid, timeout=5.0):
    deadline = time.time() + timeout
    while sid in t._inflight and time.time() < deadline:
        time.sleep(0.005)


MSGS = [
    {"role": "user", "content": "帮我看看 Test 空转的问题"},
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
    t, _ = make_titler(db, text=_dec("rename", "Test 空转排查"))
    r = t.evaluate("s1", force=True)
    assert r["action"] == "renamed"
    assert db.title == "Test 空转排查"
    assert ("set_auto_title", "Test 空转排查", "llm") in db.calls


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
    t, _ = make_titler(db, text=_dec("rename", "Test 空转排查"))
    r = t.evaluate("s1", force=True)
    assert r["action"] == "renamed"
    assert db.title == "Test 空转排查"
    assert ("set_session_title", "Test 空转排查") in db.calls
    assert ("set_session_title_source", "llm") in db.calls  # 恢复可升级性


def test_keep_does_not_write():
    db = FakeDB(messages=MSGS, title="Test 空转排查", source="llm")
    t, _ = make_titler(db, text=_dec("keep"))
    r = t.evaluate("s1", force=True)
    assert r["action"] == "keep"
    assert db.calls == []


def test_rename_to_same_title_is_keep():
    db = FakeDB(messages=MSGS, title="Test 空转排查", source="llm")
    t, _ = make_titler(db, text=_dec("rename", "Test 空转排查"))
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


def test_every_n_turns_trigger(recording_threads):
    db = FakeDB(messages=MSGS, title=None)
    t, ctx = make_titler(db, text=_dec("keep"), cfg={"every_n_turns": 3})
    t.on_session_end(session_id="s1", completed=True)
    assert len(recording_threads.instances) == 0  # 第 1 轮不评估
    t.on_session_end(session_id="s1", completed=True)
    assert len(recording_threads.instances) == 0  # 第 2 轮不评估
    t.on_session_end(session_id="s1", completed=True)
    assert len(recording_threads.instances) == 1  # 第 3 轮提交评估
    assert ctx.llm.calls == []  # 评估在 worker 中，hook 未同步执行
    run_recorded(recording_threads)
    assert len(ctx.llm.calls) == 1


def test_close_signal_with_reason_evaluates_throttled():
    db = FakeDB(messages=MSGS, title=None)
    t, ctx = make_titler(db, text=_dec("keep"), cfg={"every_n_turns": 10})
    t.on_session_end(session_id="s1", reason="shutdown", interrupted=True)
    assert len(ctx.llm.calls) == 1  # 真实关闭信号 → 评估（首次无节流）
    t.on_session_end(session_id="s1", reason="shutdown", interrupted=True)
    assert len(ctx.llm.calls) == 1  # force=False：min_interval 节流生效


def test_bare_interruption_not_counted_or_evaluated(recording_threads):
    db = FakeDB(messages=MSGS, title=None)
    t, ctx = make_titler(db, text=_dec("keep"), cfg={"every_n_turns": 1})
    t.on_session_end(session_id="s1", completed=False, interrupted=True)
    t.on_session_end(session_id="s1", completed=False, failed=True)
    t.on_session_end(session_id="s1", completed=False)
    assert recording_threads.instances == []
    assert ctx.llm.calls == []
    assert t._turns.get("s1") is None  # 未计轮数
    t.on_session_end(session_id="s1", completed=True)
    assert t._turns["s1"] == 1  # 完成轮次从 1 起计
    assert len(recording_threads.instances) == 1


def test_bg_review_thread_ignored(recording_threads):
    db = FakeDB(messages=MSGS, title=None)
    t, ctx = make_titler(db, text=_dec("keep"), cfg={"every_n_turns": 1})
    cur = threading.current_thread()
    old_name = cur.name
    cur.name = "bg-review"
    try:
        t.on_session_end(session_id="s1", completed=True, platform="desktop")
        t.on_session_finalize(session_id="s1", platform="desktop", reason="session_boundary")
    finally:
        cur.name = old_name
    assert recording_threads.instances == []
    assert t._turns.get("s1") is None
    assert ctx.llm.calls == []


def test_subagent_platform_ignored(recording_threads):
    db = FakeDB(messages=MSGS, title=None)
    t, ctx = make_titler(db, text=_dec("keep"), cfg={"every_n_turns": 1})
    t.on_session_end(session_id="s1", completed=True, platform="subagent")
    assert recording_threads.instances == []
    assert t._turns.get("s1") is None
    # 前台轮次不受影响
    t.on_session_end(session_id="s1", completed=True, platform="desktop")
    assert t._turns["s1"] == 1
    assert len(recording_threads.instances) == 1


def test_on_session_end_returns_promptly(recording_threads):
    db = FakeDB(messages=MSGS, title=None)
    t, ctx = make_titler(db, text=_dec("keep"), cfg={"every_n_turns": 1})
    started = time.time()
    t.on_session_end(session_id="s1", completed=True)
    elapsed = time.time() - started
    assert ctx.llm.calls == []  # 评估未在 hook 内同步执行
    assert len(recording_threads.instances) == 1
    assert elapsed < 1.0


def test_inflight_dedupe_prevents_duplicate_submission(recording_threads):
    db = FakeDB(messages=MSGS, title=None)
    t, ctx = make_titler(db, text=_dec("keep"), cfg={"every_n_turns": 1})
    t.on_session_end(session_id="s1", completed=True)  # n=1 → 提交
    assert len(recording_threads.instances) == 1
    t._inflight.add("s1")  # 模拟 worker 仍在飞行
    t.on_session_end(session_id="s1", completed=True)  # n=2 → in-flight 去重
    t.on_session_end(session_id="s1", completed=True)  # n=3
    assert len(recording_threads.instances) == 1
    run_recorded(recording_threads)
    assert len(ctx.llm.calls) == 1


def test_async_worker_evaluates_and_dedupes():
    import threading as _th

    entered, release = _th.Event(), _th.Event()
    llm = _blocking_llm(entered, release)
    ctx = SimpleNamespace(llm=llm)
    t = AutoTitler(ctx, {**DEFAULTS, "every_n_turns": 1}, db=FakeDB(messages=MSGS, title=None))
    started = time.time()
    t.on_session_end(session_id="s1", completed=True)
    # hook 已返回；若同步执行，主线程此刻应仍卡在 complete()（模型被阻塞 ≥5s）
    assert time.time() - started < 2
    assert not release.is_set()
    assert entered.wait(5)  # worker 已进入模型调用
    t.on_session_end(session_id="s1", completed=True)  # n=2：in-flight → 不重复
    release.set()
    _wait_inflight_clear(t, "s1")
    assert len(llm.calls) == 1


def test_finalize_evaluates_once_throttled():
    db = FakeDB(messages=MSGS, title=None)
    t, ctx = make_titler(db, text=_dec("keep"))
    t.on_session_finalize(session_id="s1", platform="cli", reason="session_boundary")
    assert len(ctx.llm.calls) == 1
    t.on_session_finalize(session_id="s1", platform="cli", reason="session_boundary")
    assert len(ctx.llm.calls) == 1  # force=False 节流，不重复


def test_finalize_skips_when_eval_in_flight():
    import threading as _th

    entered, release = _th.Event(), _th.Event()
    llm = _blocking_llm(entered, release)
    t = AutoTitler(SimpleNamespace(llm=llm), {**DEFAULTS, "every_n_turns": 1}, db=FakeDB(messages=MSGS, title=None))
    t.on_session_end(session_id="s1", completed=True)
    assert entered.wait(5)
    t.on_session_finalize(session_id="s1", platform="cli", reason="session_boundary")
    release.set()
    _wait_inflight_clear(t, "s1")
    assert len(llm.calls) == 1  # 关闭不重复 in-flight 评估


def test_close_signal_then_finalize_does_not_double_call():
    db = FakeDB(messages=MSGS, title=None)
    t, ctx = make_titler(db, text=_dec("keep"))
    t.on_session_end(session_id="s1", reason="shutdown", interrupted=True)
    assert len(ctx.llm.calls) == 1
    t.on_session_finalize(session_id="s1", platform="cli", reason="session_boundary")
    assert len(ctx.llm.calls) == 1  # 双重关闭信号仍只评估一次（节流）


def test_finalize_respects_on_close_disabled():
    db = FakeDB(messages=MSGS, title=None)
    t, ctx = make_titler(db, text=_dec("keep"), cfg={"on_close": False})
    t.on_session_finalize(session_id="s1", platform="cli", reason="session_boundary")
    t.on_session_end(session_id="s1", reason="shutdown", interrupted=True)
    assert ctx.llm.calls == []


def test_early_turn_eval_submits_early_turns(recording_threads):
    db = FakeDB(messages=MSGS, title=None)
    t, _ = make_titler(db, text=_dec("keep"), cfg={"every_n_turns": 3, "early_turn_eval": True})
    t.on_session_end(session_id="s1", completed=True)  # n=1 < 3 → 提交
    run_recorded(recording_threads)  # 完成评估（清 in-flight）
    t.on_session_end(session_id="s1", completed=True)  # n=2 < 3 → 提交
    run_recorded(recording_threads)
    t.on_session_end(session_id="s1", completed=True)  # n=3 == every → 提交
    run_recorded(recording_threads)
    assert len(recording_threads.instances) == 3
    t.on_session_end(session_id="s1", completed=True)  # n=4 → 不提交
    t.on_session_end(session_id="s1", completed=True)  # n=5 → 不提交
    assert len(recording_threads.instances) == 3
    t.on_session_end(session_id="s1", completed=True)  # n=6 → every-N → 提交
    run_recorded(recording_threads)
    assert len(recording_threads.instances) == 4


def test_early_turn_eval_noop_when_every_n_is_one(recording_threads):
    db = FakeDB(messages=MSGS, title=None)
    t, _ = make_titler(db, text=_dec("keep"), cfg={"every_n_turns": 1, "early_turn_eval": True})
    for _ in range(4):
        t.on_session_end(session_id="s1", completed=True)
        run_recorded(recording_threads)
    assert len(recording_threads.instances) == 4  # 每轮都提交：early 无额外效果


def test_early_turns_still_throttled_by_min_interval(recording_threads):
    db = FakeDB(messages=MSGS, title=None)
    t, ctx = make_titler(db, text=_dec("keep"), cfg={"every_n_turns": 5, "early_turn_eval": True})
    t.on_session_end(session_id="s1", completed=True)  # n=1 → 提交
    run_recorded(recording_threads)  # 评估 #1
    assert len(ctx.llm.calls) == 1
    t.on_session_end(session_id="s1", completed=True)  # n=2 → 提交
    run_recorded(recording_threads)  # min_interval 内 → throttled
    assert len(ctx.llm.calls) == 1


def test_early_turn_eval_off_keeps_old_cadence(recording_threads):
    db = FakeDB(messages=MSGS, title=None)
    t, _ = make_titler(db, text=_dec("keep"), cfg={"every_n_turns": 3, "early_turn_eval": False})
    t.on_session_end(session_id="s1", completed=True)
    t.on_session_end(session_id="s1", completed=True)
    assert len(recording_threads.instances) == 0
    t.on_session_end(session_id="s1", completed=True)
    assert len(recording_threads.instances) == 1


def test_conflict_adds_suffix():
    db = FakeDB(messages=MSGS, title=None, conflict_titles={"Test 空转排查"})
    t, _ = make_titler(db, text=_dec("rename", "Test 空转排查"))
    r = t.evaluate("s1", force=True)
    assert r["action"] == "renamed"
    assert r["title"] == "Test 空转排查 (2)"
    assert db.title == "Test 空转排查 (2)"


def test_collision_suffix_trims_base_to_fit_char_cap():
    db = FakeDB(messages=MSGS, title=None, conflict_titles={"Test 空"})
    t, _ = make_titler(db, text=_dec("rename", "Test 空转排查"), cfg={"max_title_length": 6})
    r = t.evaluate("s1", force=True)
    assert r["action"] == "renamed"
    assert r["title"] == "Te (2)"
    assert len(r["title"]) <= 6


def test_collision_suffix_fits_display_width():
    db = FakeDB(messages=MSGS, title=None, conflict_titles={"一二三四五"})
    t, _ = make_titler(
        db, text=_dec("rename", "一二三四五"),
        cfg={"max_title_length": 16, "max_display_width": 10},
    )
    r = t.evaluate("s1", force=True)
    assert r["action"] == "renamed"
    assert r["title"] == "一二三 (2)"
    assert display_width(r["title"]) <= 10


def test_malformed_model_output_keeps():
    db = FakeDB(messages=MSGS, title="Test 空转排查", source="llm")
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


def test_llm_restore_skipped_when_user_title_lands_during_two_step():
    # 竞态：插件 set_session_title 之后、恢复 llm 来源之前用户 /title 抢先
    db = FakeDB(messages=MSGS, title="旧标题", source="llm")
    db.override_after_set_session_title = ("用户抢先的新标题", "user")
    t, ctx = make_titler(db, text=_dec("rename", "新标题"))
    r = t.evaluate("s1", force=True)
    assert ("set_session_title_source", "llm") not in db.calls  # 不恢复来源
    assert db.title == "用户抢先的新标题"
    assert db.source == "user"
    assert r["action"] == "skipped"  # 权威方胜出，不算 failed


def test_write_time_user_title_race_protected():
    db = FakeDB(messages=MSGS, title=None, source=None)

    class RaceLlm:
        def __init__(self):
            self.calls = []

        def complete(self, **kw):
            self.calls.append(kw)
            # 模拟 LLM 调用（最长 30s）期间用户 /title 落地
            db.title = "用户手改"
            db.source = "user"
            return SimpleNamespace(text=_dec("rename", "模型标题"))

    t = AutoTitler(SimpleNamespace(llm=RaceLlm()), {**DEFAULTS}, db=db)
    r = t.evaluate("s1", force=True)
    assert r["action"] == "skipped"
    assert db.title == "用户手改"
    assert db.source == "user"
    assert db.calls == []  # 未发生任何写库调用


def test_write_time_legacy_title_race_protected():
    db = FakeDB(messages=MSGS, title=None, source=None)

    class LegacyRaceLlm:
        def complete(self, **kw):
            # LLM 调用期间出现 legacy NULL provenance 标题
            db.title = "旧自动标题"
            db.source = None
            return SimpleNamespace(text=_dec("rename", "新标题"))

    t = AutoTitler(SimpleNamespace(llm=LegacyRaceLlm()), {**DEFAULTS}, db=db)
    r = t.evaluate("s1", force=True)
    assert r["action"] == "skipped"
    assert db.title == "旧自动标题"
    assert db.calls == []


def test_normalized_candidate_equal_to_current_is_keep():
    db = FakeDB(messages=MSGS, title="Test 空转排查", source="llm")
    t, ctx = make_titler(db, text=_dec("rename", '  "Test 空转排查。"  '))
    r = t.evaluate("s1", force=True)
    assert r["action"] == "keep"  # 清洗后与当前相同 → keep，不加后缀、不写库
    assert db.calls == []


def test_title_prefix_and_trailing_punctuation_cleaned():
    db = FakeDB(messages=MSGS, title="旧标题", source="llm")
    t, ctx = make_titler(db, text=_dec("rename", "Title: Test 空转排查。"))
    r = t.evaluate("s1", force=True)
    assert r["action"] == "renamed"
    assert r["title"] == "Test 空转排查"


def test_truncated_candidate_equal_to_current_is_keep():
    current = "一二三四五六七八九十123456"  # 恰好 16 字符
    db = FakeDB(messages=MSGS, title=current, source="llm")
    t, ctx = make_titler(
        db, text=_dec("rename", current + "7890"), cfg={"max_title_length": 16}
    )
    r = t.evaluate("s1", force=True)
    assert r["action"] == "keep"  # 截断后与当前相同 → keep


def test_parse_decision_tolerates_markdown_fence():
    from hermes_auto_titler.titler import _parse_decision

    text = '```json\n{"action": "rename", "title": "Test 空转排查"}\n```'
    action, title = _parse_decision(text)
    assert action == "rename"
    assert title == "Test 空转排查"
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


def test_write_truncates_by_display_width():
    db = FakeDB(messages=MSGS, title=None)
    # 显式 max_title_length=16：21 个「一」→ 先按字符截到 16 字。
    t, _ = make_titler(
        db, text=_dec("rename", "一" * 21), cfg={"max_title_length": 16}
    )
    r = t.evaluate("s1", force=True)
    assert r["action"] == "renamed"
    assert len(r["title"]) == 16
    assert display_width(r["title"]) <= 40  # 列宽上限也不超


def test_write_complete_style_wider_budget():
    db = FakeDB(messages=MSGS, title=None)
    # complete 风格列宽 +12：40+12 = 52 列 → 26 个中文字
    t, _ = make_titler(db, text=_dec("rename", "一" * 30), cfg={"title_style": "complete"})
    r = t.evaluate("s1", force=True)
    assert display_width(r["title"]) <= 52


def test_derived_long_title_forces_rename_even_when_model_keeps():
    long_title = "开发个小插件，让 Hermes 每次对话结束都会思考需不需要重命名会话标题。有别的类似项目吗，别…"
    db = FakeDB(messages=MSGS, title=long_title, source="derived")
    t, ctx = make_titler(db, text=_dec("keep", long_title))  # 模型说 keep
    r = t.evaluate("s1", force=True)
    # force_rename 下模型被要求给新标题；若给了不同标题则 rename
    assert r["action"] in ("renamed", "keep")
    if r["action"] == "renamed":
        assert db.source == "llm"


def test_derived_force_is_fail_safe_when_model_ignores_must_rename():
    # 即使 prompt 要求 derived 必须升级，provider 若仍返回 keep，也要 fail-safe 不写库。
    db = FakeDB(messages=MSGS, title="Test 空转排查", source="derived")
    t, _ = make_titler(db, text=_dec("keep"))
    r = t.evaluate("s1", force=True)
    assert r["action"] == "keep"


def test_short_derived_title_is_provisional_and_forces_model_upgrade():
    db = FakeDB(messages=MSGS, title="这个文件夹是做什么的", source="derived")
    t, ctx = make_titler(db, text=_dec("keep"))
    t.evaluate("s1", force=True)
    system = ctx.llm.calls[0]["messages"][0]["content"]
    assert "provisional deterministic fallback" in system
    assert "action MUST be rename" in system


def test_title_generation_uses_zero_temperature():
    db = FakeDB(messages=MSGS, title=None)
    t, ctx = make_titler(db, text=_dec("keep"))
    t.evaluate("s1", force=True)
    assert ctx.llm.calls[0]["temperature"] == 0


def test_title_generation_requests_max_tokens_64():
    db = FakeDB(messages=MSGS, title=None)
    t, ctx = make_titler(db, text=_dec("keep"))
    t.evaluate("s1", force=True)
    # 这里只验证插件 API 请求；宿主可能按 provider 兼容策略省略 wire 参数。
    assert ctx.llm.calls[0]["max_tokens"] == 64


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
    assert "largest share" in system
    assert "earliest sustained" not in system
    assert "final validation, cleanup, or parameter tuning" in system
    assert "dominant language" in system
    assert "express them in that language" in system
    assert "natural word order" in system
    assert "established display name and standard spacing" in system
    assert "reference symbols such as '#'" in system
    assert "Never abbreviate, clip, or leave a word or phrase incomplete" in system
    assert "full hard budget for a complete label" in system
    assert "ordinary task, action, and topic words" in system
    assert "ingest, deploy, config, fix, plugin, and update" in system
    assert "do not invent a topic" in system
    assert "byte-for-byte" in system
    assert "hyphens" in system
    assert "Current title:" not in user_prompt  # 原标题不喂给模型
    assert "Opening (" in user_prompt


def test_default_limit_preserves_literal_repository_identifier_and_intent():
    db = FakeDB(messages=MSGS, title=None)
    expected = "hermes-auto-titler补丁审验"
    t, _ = make_titler(db, text=_dec("rename", expected))
    r = t.evaluate("s1", force=True)
    assert r["action"] == "renamed"
    assert r["title"] == expected


def test_generate_blind_renders_summary_as_primary_historical_context():
    messages = [
        {"role": "user", "content": "[Session Arc Summary (d1, node 73)] # 当前焦点：X 项目开发"},
        {"role": "user", "content": "压缩后的可见开头"},
        {"role": "assistant", "content": "继续处理"},
    ]
    db = FakeDB(messages=messages, title=None)
    t, ctx = make_titler(db, text=_dec("rename", "X 项目开发"))
    recent, all_user, opening, summary = load_context_with_summary(
        db, "s1", recent_turns=2, include_all_user=True, opening_turns=1
    )
    action, title = t._generate(
        None, recent, all_user, opening, blind=True, earlier_summary=summary
    )
    assert (action, title) == ("rename", "X 项目开发")
    prompt = ctx.llm.calls[0]["messages"][1]["content"]
    opening_block = prompt.split("Opening (", 1)[1].split("Earlier history summary", 1)[0]
    assert "Session Arc Summary" not in opening_block
    assert "Earlier history summary (primary historical context" in prompt
    assert "weak hint" not in prompt
    assert "Session Arc Summary" in prompt


def test_generate_nonblind_with_summary_anchors_subject_on_summary():
    """压缩会话日常评估：摘要升级为主题锚点，失真的可见 opening 降级为局部续段。"""
    messages = [
        {"role": "user", "content": "[Session Arc Summary] 主线：账号体系注册运营"},
        {"role": "user", "content": "为什么 163 邮箱收不到验证码，帮我配置一下"},
        {"role": "assistant", "content": "查 keychain 和 IMAP"},
    ]
    db = FakeDB(messages=messages, title="LINE辅助邮箱配置")
    t, ctx = make_titler(db, text=_dec("rename", "账号体系注册运营"))
    recent, all_user, opening, summary = load_context_with_summary(
        db, "s1", recent_turns=2, include_all_user=True, opening_turns=1
    )
    action, title = t._generate(
        "LINE辅助邮箱配置", recent, all_user, opening,
        force_rename=False, blind=False, earlier_summary=summary,
    )
    assert (action, title) == ("rename", "账号体系注册运营")
    system = ctx.llm.calls[0]["messages"][0]["content"]
    user_prompt = ctx.llm.calls[0]["messages"][1]["content"]
    # 有摘要时：opening 标记为局部续段，不再自称 primary subject source
    assert "Visible opening (top of the visible message window" in user_prompt
    assert "NOT the original session opening" in user_prompt
    assert "Opening (the session's starting turns" not in user_prompt
    # 摘要标记为历史主题锚点，不再是 weak hint
    assert "historical subject anchor" in user_prompt
    assert "weak hint" not in user_prompt
    # hierarchy 含压缩会话锚定规则（system 侧）
    assert "compacted session the earlier history summary anchors the subject" in system


def test_generate_forwards_provider_from_config():
    """配置了 provider 时，complete 调用必须显式传 provider（防宿主 auto 静默回退）。"""
    db = FakeDB(messages=MSGS, title=None)
    t, ctx = make_titler(db, text=_dec("keep"))
    t.cfg["provider"] = "nous"
    t.cfg["model"] = "tencent/hy3:free"
    t.evaluate("s1", force=True)
    call = ctx.llm.calls[0]
    assert call["provider"] == "nous"
    assert call["model"] == "tencent/hy3:free"


def test_evaluate_blind_with_summary_keeps_user_continuation_but_omits_assistant_tail():
    messages = [
        {"role": "user", "content": "[Session Arc Summary] 主线：Hermes 自动标题插件开发"},
        {"role": "user", "content": "后续持续转向 WSP 搜索配置"},
        {"role": "assistant", "content": "参数已调整"},
    ]
    db = FakeDB(messages=messages, title=None)
    t, ctx = make_titler(db, text=_dec("rename", "Hermes自动标题插件"))
    result = t.evaluate("s1", force=True, blind=True)
    assert result["action"] == "renamed"
    prompt = ctx.llm.calls[0]["messages"][1]["content"]
    assert "Hermes 自动标题插件开发" in prompt
    assert "Post-summary user continuation" in prompt
    assert "后续持续转向 WSP 搜索配置" in prompt
    assert "参数已调整" not in prompt


class SessionStateDB(FakeDB):
    """retitle_all 批处理测试：每个会话独立的 title/source 状态与真实写回。

    单例 FakeDB 的 title/source 是全局的，无法表达「每行各自的来源」，
    会让 user/legacy 断言依赖列表顺序或巧合而假通过——这里改为按 sid 存
    状态，写回也真实落到对应会话。
    """

    def __init__(self, sessions, state, messages=None):
        super().__init__(messages=messages if messages is not None else MSGS)
        self.sessions = sessions
        self.state = dict(state)
        self.calls = []

    def list_sessions_rich(self, limit=20, min_message_count=0, include_children=False):
        return self.sessions

    def get_session_title(self, sid):
        return self.state.get(sid, (None, None))[0]

    def get_session_title_source(self, sid):
        return self.state.get(sid, (None, None))[1]

    def set_auto_title(self, sid, title, *, source):
        self.calls.append(("set_auto_title", sid, title, source))
        self.state[sid] = (title, source)
        return True

    def set_session_title(self, sid, title):
        self.calls.append(("set_session_title", sid, title))
        self.state[sid] = (title, "user")
        return True

    def set_session_title_source(self, sid, source):
        self.calls.append(("set_session_title_source", sid, source))
        t, _ = self.state[sid]
        self.state[sid] = (t, source)


def test_retitle_all_skips_user_and_uses_blind():
    sessions = [
        {"id": "s1", "title": "旧标题", "message_count": 5},
        {"id": "s2", "title": "我手改的", "message_count": 5},
        {"id": "s3", "title": "老标题", "message_count": 5},
    ]
    state = {"s1": ("旧标题", "llm"), "s2": ("我手改的", "user"), "s3": ("老标题", None)}
    db = SessionStateDB(sessions, state)
    t, ctx = make_titler(db, text=_dec("rename", "盲改标题"))
    results = t.retitle_all()
    by_id = {r["session_id"]: r for r in results}
    assert by_id["s2"]["action"] == "skipped"  # user 手改不动
    assert by_id["s2"]["reason"] == "user title"
    assert by_id["s3"]["action"] == "skipped"  # legacy NULL 保护
    assert by_id["s1"]["action"] == "renamed"
    assert db.state["s1"] == ("盲改标题", "llm")  # 真实落库且恢复 llm 来源
    assert db.state["s2"] == ("我手改的", "user")  # 未被动
    assert db.state["s3"] == ("老标题", None)
    # blind：prompt 里没有当前标题（只有 s1 会调模型）
    assert len(ctx.llm.calls) == 1
    for call in ctx.llm.calls:
        assert "Current title:" not in call["messages"][1]["content"]


def test_retitle_all_skips_user_and_dry_run():
    sessions = [
        {"id": "s-user", "title": "手改"},
        {"id": "s-auto", "title": "旧自动"},
        {"id": "s-legacy", "title": "老标题"},
    ]
    state = {
        "s-user": ("手改", "user"),
        "s-auto": ("旧自动", "llm"),
        "s-legacy": ("老标题", None),
    }
    db = SessionStateDB(sessions, state)
    t, ctx = make_titler(db, text=_dec("rename", "新标题"))
    results = t.retitle_all()
    by_id = {r["session_id"]: r for r in results}
    assert by_id["s-user"]["action"] == "skipped"
    assert by_id["s-user"]["reason"] == "user title"
    assert by_id["s-legacy"]["action"] == "skipped"  # legacy NULL 保护
    assert by_id["s-auto"]["action"] == "renamed"
    assert db.state["s-auto"] == ("新标题", "llm")
    # dry-run 不做模型调用；user 行在 dry-run 之前按实现顺序跳过
    t2, ctx2 = make_titler(db, text=_dec("rename", "新标题"))
    results2 = t2.retitle_all(dry_run=True)
    by2 = {r["session_id"]: r for r in results2}
    assert by2["s-user"]["action"] == "skipped"  # user 先于 dry-run 跳过
    assert by2["s-user"]["reason"] == "user title"
    assert by2["s-auto"]["action"] == "dry-run"
    assert by2["s-legacy"]["action"] == "dry-run"  # legacy 只在 dry-run 前做 user 检查
    assert ctx2.llm.calls == []


def test_retitle_all_respects_recent_eval_throttle():
    db = SessionStateDB(
        sessions=[{"id": "s1", "title": "旧标题", "message_count": 5}],
        state={"s1": (None, None)},
    )
    t, ctx = make_titler(db, text=_dec("keep"))
    t._last_eval["s1"] = time.time()  # 同一实例刚评估过
    results = t.retitle_all()
    assert results[0]["action"] == "throttled"
    assert ctx.llm.calls == []


# -- 用量记账 ---------------------------------------------------------------

def test_auxiliary_usage_recorded_through_sessiondb():
    usage = SimpleNamespace(
        input_tokens=1500, output_tokens=40, total_tokens=1540,
        cache_read_tokens=120, cache_write_tokens=80, cost_usd=0.0012,
    )
    db = FakeDB(messages=MSGS, title=None)
    db.usage_records = []
    db.record_auxiliary_usage = (
        lambda sid, task, **kw: db.usage_records.append({"session_id": sid, "task": task, **kw})
    )
    ctx = SimpleNamespace(
        llm=FakeLlm(_dec("keep"), usage=usage, model="deepseek-v4-flash", provider="opencode-go")
    )
    t = AutoTitler(ctx, {**DEFAULTS}, db=db)
    t.evaluate("s1", force=True)
    assert len(db.usage_records) == 1
    rec = db.usage_records[0]
    assert rec["session_id"] == "s1"
    assert rec["task"] == "hermes_auto_titler"
    assert rec["model"] == "deepseek-v4-flash"
    assert rec["billing_provider"] == "opencode-go"
    assert rec["input_tokens"] == 1500
    assert rec["output_tokens"] == 40
    assert rec["cache_read_tokens"] == 120
    assert rec["cache_write_tokens"] == 80
    assert rec["estimated_cost_usd"] == 0.0012


def test_auxiliary_usage_noop_without_api():
    db = FakeDB(messages=MSGS, title=None)  # 无 record_auxiliary_usage（老宿主/测试 fake）
    t, ctx = make_titler(db, text=_dec("keep"))
    r = t.evaluate("s1", force=True)
    assert r["action"] == "keep"
    assert len(ctx.llm.calls) == 1  # 记账缺失不影响评估


# -- 命令与生命周期注册 -------------------------------------------------------

def test_status_includes_early_turn_eval():
    from hermes_auto_titler.commands import make_handler

    db = FakeDB(messages=MSGS)
    t, _ = make_titler(db, cfg={"early_turn_eval": True})
    out = make_handler(t)("status")
    assert "early_turn_eval=True" in out


def test_config_command_rejects_invalid_and_accepts_new_keys(monkeypatch):
    from hermes_auto_titler.commands import make_handler

    saved = []
    monkeypatch.setattr(
        "hermes_auto_titler.config.save_config",
        lambda cfg, path=None: saved.append(dict(cfg)),
    )
    db = FakeDB(messages=MSGS)
    t, _ = make_titler(db)
    h = make_handler(t)
    assert "值无效" in h("config strategy bogus")
    assert t.cfg["strategy"] == "conservative"
    assert "值无效" in h("config enabled maybe")  # 非法 bool 明确拒绝，不静默变 False
    assert t.cfg["enabled"] is True
    assert "值无效" in h("config every_n_turns abc")
    assert t.cfg["every_n_turns"] == 4
    h("config early_turn_eval true")
    assert t.cfg["early_turn_eval"] is True
    assert saved and saved[-1]["early_turn_eval"] is True
    h("config retitle_summary_chars 2000")
    assert t.cfg["retitle_summary_chars"] == 2000
    h("config opening_turns 0")
    assert t.cfg["opening_turns"] == 1  # 边界钳制


def test_register_registers_hooks_and_command(monkeypatch):
    import hermes_auto_titler as pkg

    hooks, cmds = [], []

    class FakeCtx:
        def register_hook(self, name, fn):
            hooks.append(name)

        def register_command(self, name, handler, **kw):
            cmds.append(name)

    monkeypatch.setattr(pkg, "load_config", lambda: {**DEFAULTS, "enabled": True})
    pkg.register(FakeCtx())
    assert "on_session_end" in hooks
    assert "on_session_finalize" in hooks
    assert "autotitler" in cmds

    hooks.clear()
    cmds.clear()
    monkeypatch.setattr(pkg, "load_config", lambda: {**DEFAULTS, "enabled": False})
    pkg.register(FakeCtx())
    assert hooks == []  # 禁用时零 hook
    assert "autotitler" in cmds


# -- 内部执行过滤（cron / 大小写 / 标志不一致） -------------------------------

def test_cron_platform_ignored(recording_threads):
    db = FakeDB(messages=MSGS, title=None)
    t, ctx = make_titler(db, text=_dec("keep"), cfg={"every_n_turns": 1})
    t.on_session_end(session_id="s1", completed=True, platform="cron")
    assert recording_threads.instances == []
    assert t._turns.get("s1") is None
    # 大小写不敏感（宿主 cron/scheduler 构造 platform='cron'）
    t.on_session_end(session_id="s1", completed=True, platform="Cron")
    assert recording_threads.instances == []
    assert t._turns.get("s1") is None
    # finalize 同样排除 cron
    t.on_session_finalize(session_id="s1", platform="cron", reason="session_boundary")
    assert ctx.llm.calls == []
    # 前台轮次不受影响
    t.on_session_end(session_id="s1", completed=True, platform="desktop")
    assert t._turns["s1"] == 1
    assert len(recording_threads.instances) == 1


def test_subagent_platform_case_insensitive(recording_threads):
    db = FakeDB(messages=MSGS, title=None)
    t, ctx = make_titler(db, text=_dec("keep"), cfg={"every_n_turns": 1})
    t.on_session_end(session_id="s1", completed=True, platform="SubAgent")
    assert recording_threads.instances == []
    assert t._turns.get("s1") is None
    t.on_session_finalize(session_id="s1", platform="SUBAGENT", reason="session_boundary")
    assert ctx.llm.calls == []


def test_inconsistent_flags_failed_or_interrupted_not_counted(recording_threads):
    db = FakeDB(messages=MSGS, title=None)
    t, ctx = make_titler(db, text=_dec("keep"), cfg={"every_n_turns": 1})
    # completed=True 但 failed/interrupted 为真：标志不一致，仍不算完整轮次
    t.on_session_end(session_id="s1", completed=True, failed=True)
    t.on_session_end(session_id="s1", completed=True, interrupted=True)
    assert recording_threads.instances == []
    assert t._turns.get("s1") is None
    assert ctx.llm.calls == []
    t.on_session_end(session_id="s1", completed=True)
    assert t._turns["s1"] == 1
    assert len(recording_threads.instances) == 1


# -- early_turn_eval 来源门 ---------------------------------------------------

class BrokenSourceDB(FakeDB):
    def get_session_title_source(self, sid):
        raise RuntimeError("db down")


def _early_titler(db, **cfg):
    return make_titler(db, text=_dec("keep"), cfg={"every_n_turns": 3, "early_turn_eval": True, **cfg})


def test_early_gate_untitled_submits(recording_threads):
    db = FakeDB(messages=MSGS, title=None, source=None)
    t, _ = _early_titler(db)
    t.on_session_end(session_id="s1", completed=True)  # n=1 < 3
    assert len(recording_threads.instances) == 1


def test_early_gate_derived_submits(recording_threads):
    db = FakeDB(messages=MSGS, title="旧标题", source="derived")
    t, _ = _early_titler(db)
    t.on_session_end(session_id="s1", completed=True)  # n=1 < 3
    assert len(recording_threads.instances) == 1


def test_early_gate_llm_skips_early_but_boundary_eligible(recording_threads):
    db = FakeDB(messages=MSGS, title="旧标题", source="llm")
    t, ctx = _early_titler(db)
    t.on_session_end(session_id="s1", completed=True)  # n=1：llm 不提前
    t.on_session_end(session_id="s1", completed=True)  # n=2：llm 不提前
    assert recording_threads.instances == []
    assert ctx.llm.calls == []
    t.on_session_end(session_id="s1", completed=True)  # n=3：正常边界，llm 仍合格
    assert len(recording_threads.instances) == 1
    run_recorded(recording_threads)
    assert len(ctx.llm.calls) == 1


def test_early_gate_user_skips(recording_threads):
    db = FakeDB(messages=MSGS, title="手改", source="user")
    t, ctx = _early_titler(db)
    t.on_session_end(session_id="s1", completed=True)  # n=1：user 不提前
    t.on_session_end(session_id="s1", completed=True)  # n=2：user 不提前
    assert recording_threads.instances == []
    # 正常边界仍提交；evaluate 内部对 user 保护，不产生模型调用
    t.on_session_end(session_id="s1", completed=True)  # n=3
    assert len(recording_threads.instances) == 1
    run_recorded(recording_threads)
    assert ctx.llm.calls == []


def test_early_gate_legacy_skips(recording_threads):
    db = FakeDB(messages=MSGS, title="旧自动标题", source=None)  # legacy NULL 来源
    t, ctx = _early_titler(db)
    t.on_session_end(session_id="s1", completed=True)  # n=1：legacy 已有标题不提前
    t.on_session_end(session_id="s1", completed=True)  # n=2
    assert recording_threads.instances == []
    # 正常边界仍提交；evaluate 内部对 legacy 保护，不产生模型调用
    t.on_session_end(session_id="s1", completed=True)  # n=3
    assert len(recording_threads.instances) == 1
    run_recorded(recording_threads)
    assert ctx.llm.calls == []


def test_early_gate_fail_safe_on_source_error(recording_threads):
    db = BrokenSourceDB(messages=MSGS, title=None, source=None)
    t, _ = _early_titler(db)
    t.on_session_end(session_id="s1", completed=True)  # n=1：来源发现失败 → 不提前提交
    assert recording_threads.instances == []
    t.on_session_end(session_id="s1", completed=True)  # n=2
    assert recording_threads.instances == []
    t.on_session_end(session_id="s1", completed=True)  # n=3：正常边界不受影响
    assert len(recording_threads.instances) == 1


# -- 异步健壮性：Context 传播 / 线程失败 / DB 单例 ----------------------------

def test_worker_context_propagated_via_host_wrapper(monkeypatch, recording_threads):
    fake_tools = types.ModuleType("tools")
    fake_tc = types.ModuleType("tools.thread_context")
    wrapped = []

    def fake_propagate(target):
        wrapped.append(True)
        ctx = contextvars.copy_context()

        def runner(*args, **kwargs):
            return ctx.run(target, *args, **kwargs)

        return runner

    fake_tc.propagate_context_to_thread = fake_propagate
    monkeypatch.setitem(sys.modules, "tools", fake_tools)
    monkeypatch.setitem(sys.modules, "tools.thread_context", fake_tc)

    var = contextvars.ContextVar("autotitler_host_var", default="unset")
    seen = []

    class ContextLlm:
        def complete(self, **kw):
            seen.append(var.get())
            return SimpleNamespace(text=_dec("keep"))

    db = FakeDB(messages=MSGS, title=None)
    t = AutoTitler(
        SimpleNamespace(llm=ContextLlm()), {**DEFAULTS, "every_n_turns": 1}, db=db
    )
    var.set("profile-A")
    t.on_session_end(session_id="s1", completed=True)
    assert wrapped == [True]  # 优先走宿主 wrapper
    assert len(recording_threads.instances) == 1
    run_recorded(recording_threads)
    assert seen == ["profile-A"]  # Context 经宿主 wrapper 传播进 worker


def test_worker_context_fallback_stdlib(monkeypatch, recording_threads):
    real_import = __import__

    def no_tools(name, *args, **kwargs):
        if name == "tools" or name.startswith("tools."):
            raise ImportError(f"no host tools in test env: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", no_tools)

    var = contextvars.ContextVar("autotitler_fallback_var", default="unset")
    seen = []

    class ContextLlm:
        def complete(self, **kw):
            seen.append(var.get())
            return SimpleNamespace(text=_dec("keep"))

    db = FakeDB(messages=MSGS, title=None)
    t = AutoTitler(
        SimpleNamespace(llm=ContextLlm()), {**DEFAULTS, "every_n_turns": 1}, db=db
    )
    var.set("profile-B")
    t.on_session_end(session_id="s1", completed=True)
    assert len(recording_threads.instances) == 1
    run_recorded(recording_threads)
    assert seen == ["profile-B"]  # 标准库 contextvars 兜底同样传播 Context


@pytest.mark.parametrize("fail_in", ["init", "start"])
def test_worker_thread_failure_clears_inflight(monkeypatch, fail_in):
    RecordingThread.instances = []  # 本测试不用 fixture，手动清零全局实例表

    class BoomThread:
        def __init__(self, *args, **kwargs):
            if fail_in == "init":
                raise RuntimeError("cannot construct thread")

        def start(self):
            raise RuntimeError("cannot start thread")

    monkeypatch.setattr("hermes_auto_titler.titler.threading.Thread", BoomThread)
    db = FakeDB(messages=MSGS, title=None)
    t, ctx = make_titler(db, text=_dec("keep"), cfg={"every_n_turns": 1})
    t.on_session_end(session_id="s1", completed=True)  # hook 不抛异常
    assert "s1" not in t._inflight  # 失败后 in-flight 标记已清理，可重试
    assert ctx.llm.calls == []
    # 线程恢复后再次提交成功
    monkeypatch.setattr("hermes_auto_titler.titler.threading.Thread", RecordingThread)
    t.on_session_end(session_id="s1", completed=True)  # n=2
    assert len(RecordingThread.instances) == 1


def test_get_db_singleton_under_concurrent_first_access(monkeypatch):
    import hermes_auto_titler.titler as titler_mod

    class FakeSessionDB:
        instances = 0

        def __init__(self):
            # 主动释放 GIL，确保没有 _db_lock 时多个首次访问会重叠构造；
            # 避免测试仅因构造太快被线程调度偶然串行而假通过。
            time.sleep(0.02)
            FakeSessionDB.instances += 1

    monkeypatch.setattr(titler_mod, "SessionDB", FakeSessionDB)
    monkeypatch.setattr(titler_mod, "_db", None)
    results = []

    def worker():
        results.append(titler_mod.get_db())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len({id(r) for r in results}) == 1  # 全部拿到同一个实例
    assert FakeSessionDB.instances == 1  # 并发首次构造只建一个 DB


# -- 写回竞态：每次 apply 前刷新来源 ------------------------------------------

def test_user_title_lands_between_write_attempts_not_overwritten():
    # 竞态：用户 /title 在第一次写尝试（唯一性冲突 ValueError）之后、
    # 重试写之前落地。apply 每次尝试都刷新来源与保护，user 权威胜出。
    db = FakeDB(messages=MSGS, title="旧标题", source="llm", conflict_titles={"新标题"})

    def flaky_source(sid):
        # 首次写尝试失败（calls 里已有 set_session_title）后，来源翻转为 user
        if any(c[0] == "set_session_title" for c in db.calls):
            db.title = "用户手改"
            db.source = "user"
        return db.source

    db.get_session_title_source = flaky_source

    t, _ = make_titler(db, text=_dec("rename", "新标题"))
    r = t.evaluate("s1", force=True)
    assert r["action"] == "skipped"
    assert r["reason"] == "user title is authoritative (set during evaluation)"
    assert db.title == "用户手改"  # 重试没有覆盖用户标题
    assert db.source == "user"
    assert not any(c[0] == "set_session_title" and c[1] == "新标题 (2)" for c in db.calls)


# -- 标题规范化与截断边界 ------------------------------------------------------

def test_clean_title_wrapped_quote_after_prefix():
    db = FakeDB(messages=MSGS, title="旧标题", source="llm")
    t, _ = make_titler(db, text=_dec("rename", 'Title: "Test 空转排查。"'))
    r = t.evaluate("s1", force=True)
    assert r["action"] == "renamed"
    assert r["title"] == "Test 空转排查"
    assert db.title == "Test 空转排查"


def test_clean_title_chinese_prefix_quoted():
    db = FakeDB(messages=MSGS, title="旧标题", source="llm")
    t, _ = make_titler(db, text=_dec("rename", '"标题：Foo。"'))
    r = t.evaluate("s1", force=True)
    assert r["action"] == "renamed"
    assert r["title"] == "Foo"
    assert db.title == "Foo"


def test_clean_title_nested_wrapped_quotes():
    db = FakeDB(messages=MSGS, title="旧标题", source="llm")
    t, _ = make_titler(db, text=_dec("rename", '「Title: "Foo"。」'))
    r = t.evaluate("s1", force=True)
    assert r["action"] == "renamed"
    assert r["title"] == "Foo"


def test_truncation_boundary_punctuation_stripped():
    db = FakeDB(messages=MSGS, title=None)
    # 18 字符 > 上限 16：截到 "abcdefghijklmno:" 后剥掉边界冒号
    t, _ = make_titler(db, text=_dec("rename", "abcdefghijklmno:xyz"), cfg={"max_title_length": 16})
    r = t.evaluate("s1", force=True)
    assert r["action"] == "renamed"
    assert r["title"] == "abcdefghijklmno"
    assert len(r["title"]) <= 16


def test_truncation_boundary_quote_stripped():
    db = FakeDB(messages=MSGS, title=None)
    t, _ = make_titler(db, text=_dec("rename", "abcdefghijklmno'xyz"), cfg={"max_title_length": 16})
    r = t.evaluate("s1", force=True)
    assert r["action"] == "renamed"
    assert r["title"] == "abcdefghijklmno"
    assert not r["title"].endswith("'")


# -- 命令准确性 -----------------------------------------------------------------

def test_config_command_enabled_message_notes_restart(monkeypatch):
    from hermes_auto_titler.commands import make_handler

    saved = []
    monkeypatch.setattr(
        "hermes_auto_titler.config.save_config",
        lambda cfg, path=None: saved.append(dict(cfg)),
    )
    db = FakeDB(messages=MSGS)
    t, _ = make_titler(db)
    h = make_handler(t)
    out = h("config enabled 0")
    assert t.cfg["enabled"] is False
    assert "需重启" in out  # 初始 false→true 需重启才能注册 hook
    out2 = h("config enabled 1")
    assert t.cfg["enabled"] is True
    assert "需重启" in out2


# -- 滞后机制：候选标题需连续确认才写库 -----------------------------------------

def _confirmations_titler(confirmations=2, per_hour=0, title="旧标题"):
    db = FakeDB(messages=MSGS, title=title, source="llm")
    t, ctx = make_titler(
        db,
        text=_dec("rename", "新标题"),
        cfg={"rename_confirmations": confirmations, "renames_per_hour": per_hour},
    )
    return db, t, ctx


def test_first_candidate_is_pending_not_written():
    # 第一次给出候选：不写库，返回 pending
    db, t, ctx = _confirmations_titler()
    r = t.evaluate("s1", force=True)
    assert r["action"] == "pending"
    assert r.get("candidate") == "新标题"
    assert db.title == "旧标题"  # 未写
    assert db.calls == []  # 无任何写库调用


def test_second_same_candidate_confirms_rename():
    # 连续两次相同候选 → 真正改名
    db, t, ctx = _confirmations_titler()
    assert t.evaluate("s1", force=True)["action"] == "pending"
    r = t.evaluate("s1", force=True)
    assert r["action"] == "renamed"
    assert db.title == "新标题"


def test_different_candidates_reset_confirmation():
    # 候选变化：重新计数（震荡信号被机械拦截）
    db, t, ctx = _confirmations_titler()
    assert t.evaluate("s1", force=True)["action"] == "pending"
    # 第二次模型改主意，给出另一个候选 → pending 新候选，计数重置为 1
    ctx.llm.text = _dec("rename", "另一标题")
    r = t.evaluate("s1", force=True)
    assert r["action"] == "pending"
    assert r.get("candidate") == "另一标题"
    assert db.title == "旧标题"
    # 第三次回到「另一标题」→ 确认
    r = t.evaluate("s1", force=True)
    assert r["action"] == "renamed"
    assert db.title == "另一标题"


def test_keep_clears_pending_candidate():
    # 中途一次 keep：清空 pending（模型自己否决了之前的候选）
    db, t, ctx = _confirmations_titler()
    assert t.evaluate("s1", force=True)["action"] == "pending"
    ctx.llm.text = _dec("keep")
    assert t.evaluate("s1", force=True)["action"] == "keep"
    assert t._pending.get("s1") is None
    # 再次 rename 需要重新攒满确认数
    ctx.llm.text = _dec("rename", "新标题")
    assert t.evaluate("s1", force=True)["action"] == "pending"
    assert db.title == "旧标题"
    assert t.evaluate("s1", force=True)["action"] == "renamed"


def test_current_title_equal_to_candidate_still_keep():
    # 候选与当前标题相同 = keep，且应清掉 stale pending
    db = FakeDB(messages=MSGS, title="新标题", source="llm")
    t, ctx = make_titler(db, text=_dec("rename", "新标题"),
                         cfg={"rename_confirmations": 2})
    t._pending["s1"] = {"title": "新标题", "count": 1}
    assert t.evaluate("s1", force=True)["action"] == "keep"
    assert t._pending.get("s1") is None


def test_confirmations_1_keeps_single_shot_behavior():
    # 默认配置（1）：单次评估直接改名，与历史行为完全一致
    db, t, ctx = _confirmations_titler(confirmations=1)
    r = t.evaluate("s1", force=True)
    assert r["action"] == "renamed"
    assert db.title == "新标题"


def test_derived_upgrade_bypasses_confirmation():
    # derived/无标题的首次升级是补漏不是折腾：旁路滞后立即写
    db = FakeDB(messages=MSGS, title="旧兜底", source="derived")
    t, ctx = make_titler(db, text=_dec("rename", "正式标题"),
                         cfg={"rename_confirmations": 3})
    r = t.evaluate("s1", force=True)
    assert r["action"] == "renamed"
    assert db.title == "正式标题"

    db2 = FakeDB(messages=MSGS, title=None, source=None)
    t2, ctx2 = make_titler(db2, text=_dec("rename", "首个标题"),
                           cfg={"rename_confirmations": 3})
    r2 = t2.evaluate("s2", force=True)
    assert r2["action"] == "renamed"
    assert db2.title == "首个标题"


def test_close_eval_bypasses_confirmation():
    # close 终局评估直接提交：会话结束时全貌已知，不再要求二次确认
    db, t, ctx = _confirmations_titler()
    r = t.evaluate("s1", force=True, blind=True)  # blind=retitle-all/close 路径
    assert r["action"] == "renamed"
    assert db.title == "新标题"


def test_renames_per_hour_cap_blocks_and_recovers():
    # 每小时频次上限：窗口内第 3 次（上限 2）被拒；窗口滑过后恢复
    db, t, ctx = _confirmations_titler(per_hour=2)
    now = 1000.0
    with patch("hermes_auto_titler.titler.time.time", return_value=now):
        assert t.evaluate("s1", force=True)["action"] == "pending"
    with patch("hermes_auto_titler.titler.time.time", return_value=now + 60):
        assert t.evaluate("s1", force=True)["action"] == "renamed"  # 写 #1
    # 攒第二个候选并确认 → 写 #2（窗口内第 2 次，仍允许）
    db.title = "又旧了"
    with patch("hermes_auto_titler.titler.time.time", return_value=now + 120):
        assert t.evaluate("s1", force=True)["action"] == "pending"
    with patch("hermes_auto_titler.titler.time.time", return_value=now + 180):
        assert t.evaluate("s1", force=True)["action"] == "renamed"  # 写 #2
    # 第三个候选确认时触顶 → capped 不写
    db.title = "第三版旧标题"
    with patch("hermes_auto_titler.titler.time.time", return_value=now + 240):
        assert t.evaluate("s1", force=True)["action"] == "pending"
    with patch("hermes_auto_titler.titler.time.time", return_value=now + 300):
        r = t.evaluate("s1", force=True)
        assert r["action"] == "capped"
        assert db.title == "第三版旧标题"
    # 窗口滑过（最早一次写 >3600s 前）→ 恢复写入
    with patch("hermes_auto_titler.titler.time.time", return_value=now + 60 + 3600 + 1):
        r = t.evaluate("s1", force=True)
        assert r["action"] == "renamed"
        assert db.title == "新标题"


def test_user_race_during_confirmation_clears_pending():
    # 等待二次确认期间用户 /title 抢先：放弃 pending，用户权威胜出
    db, t, ctx = _confirmations_titler()
    assert t.evaluate("s1", force=True)["action"] == "pending"
    db.title, db.source = "用户手改", "user"
    r = t.evaluate("s1", force=True)
    assert r["action"] == "skipped"
    assert r["reason"] == "user title is authoritative"
    assert t._pending.get("s1") is None


# -- 提示词防漂移规范 ------------------------------------------------------------

def test_prompt_contains_stability_rules_on_normal_eval():
    # 日常评估注入稳定性规则；盲改（终局）不注入
    db = FakeDB(messages=MSGS, title="旧标题", source="llm")
    t, ctx = make_titler(db)
    t.evaluate("s1", force=True)
    system = ctx.llm.calls[0]["messages"][0]["content"]
    assert "Stability rules" in system
    assert "MULTIPLE later user requests" in system

    db2 = FakeDB(messages=MSGS, title="旧标题", source="llm")
    t2, ctx2 = make_titler(db2)
    t2.evaluate("s1", force=True, blind=True)
    blind_system = ctx2.llm.calls[0]["messages"][0]["content"]
    assert "Stability rules" not in blind_system


def test_prompt_conservative_rule_prefers_keep_when_uncertain():
    db = FakeDB(messages=MSGS, title="旧标题", source="llm")
    t, ctx = make_titler(db)
    t.evaluate("s1", force=True)
    system = ctx.llm.calls[0]["messages"][0]["content"]
    assert "When uncertain, keep" in system
