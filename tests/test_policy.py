from types import SimpleNamespace

from hermes_auto_titler.config import DEFAULTS
from hermes_auto_titler.titler import AutoTitler


MSGS = [
    {"role": "user", "content": "继续维护 Hermes auto titler，先解决标题漂移"},
    {"role": "assistant", "content": "可以检查 prompt 和 review state"},
    {"role": "user", "content": "这个方向继续，别让临时工具抢掉标题"},
    {"role": "assistant", "content": "收到"},
]


class FakeDB:
    TITLE_SOURCE_DERIVED = "derived"
    TITLE_SOURCE_LLM = "llm"
    TITLE_SOURCE_USER = "user"
    MAX_TITLE_LENGTH = 100

    def __init__(self, title="旧标题", source="llm"):
        self.title = title
        self.source = source
        self.calls = []

    def get_messages_as_conversation(self, sid, include_ancestors=True):
        return MSGS

    def get_session_title(self, sid):
        return self.title

    def get_session_title_source(self, sid):
        return self.source

    def set_auto_title(self, sid, title, *, source):
        self.title, self.source = title, source
        self.calls.append(("set_auto_title", title, source))
        return True

    def set_session_title(self, sid, title):
        if title == self.title:
            return False
        self.title, self.source = title, "user"
        self.calls.append(("set_session_title", title))
        return True

    def set_session_title_source(self, sid, source):
        self.source = source
        self.calls.append(("set_session_title_source", source))


class FakeLlm:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text=self.text, usage={})


def dec(action, title=""):
    import json
    return json.dumps({"action": action, "title": title})


def make(confirmations=0, strategy="conservative"):
    db = FakeDB()
    llm = FakeLlm(dec("rename", "新标题"))
    t = AutoTitler(
        SimpleNamespace(llm=llm),
        {**DEFAULTS, "rename_confirmations": confirmations, "strategy": strategy},
        db=db,
    )
    return db, llm, t


def test_two_confirmations_require_two_followup_endorsements():
    db, llm, t = make(confirmations=2)
    first = t.evaluate("s1", force=True)
    assert first["action"] == "pending"
    assert db.title == "旧标题"

    llm.text = dec("approve")
    second = t.evaluate("s1", force=True)
    assert second == {
        "action": "pending",
        "candidate": "新标题",
        "confirmations": 1,
        "required": 2,
    }
    assert db.title == "旧标题"

    third = t.evaluate("s1", force=True)
    assert third["action"] == "renamed"
    assert db.title == "新标题"


def test_replacing_candidate_resets_confirmation_count():
    db, llm, t = make(confirmations=2)
    assert t.evaluate("s1", force=True)["action"] == "pending"

    llm.text = dec("approve")
    assert t.evaluate("s1", force=True)["confirmations"] == 1

    llm.text = dec("rename", "更好标题")
    replaced = t.evaluate("s1", force=True)
    assert replaced["action"] == "pending"
    assert replaced["candidate"] == "更好标题"
    assert t._pending["s1"].get("confirmations", 0) == 0

    llm.text = dec("approve")
    once = t.evaluate("s1", force=True)
    assert once["action"] == "pending"
    assert once["confirmations"] == 1
    assert db.title == "旧标题"

    assert t.evaluate("s1", force=True)["action"] == "renamed"
    assert db.title == "更好标题"


def test_external_llm_title_change_invalidates_pending_candidate():
    db, llm, t = make(confirmations=2)
    assert t.evaluate("s1", force=True)["action"] == "pending"
    assert t._pending["s1"]["base_title"] == "旧标题"

    db.title = "其他自动标题"
    db.source = "llm"
    llm.text = dec("approve")
    result = t.evaluate("s1", force=True)
    assert result["action"] == "keep"
    assert t._pending.get("s1") is None
    assert db.title == "其他自动标题"


def test_zero_confirmations_remains_direct_write():
    db, llm, t = make(confirmations=0)
    result = t.evaluate("s1", force=True)
    assert result["action"] == "renamed"
    assert db.title == "新标题"


def test_aggressive_and_conservative_prompts_have_distinct_thresholds():
    _, llm_c, t_c = make(strategy="conservative")
    t_c.evaluate("s1", force=True)
    conservative = llm_c.calls[-1]["messages"][0]["content"]

    _, llm_a, t_a = make(strategy="aggressive")
    t_a.evaluate("s1", force=True)
    aggressive = llm_a.calls[-1]["messages"][0]["content"]

    assert "Strategy: conservative" in conservative
    assert "material, durable mismatch" in conservative
    assert "Strategy: aggressive" in aggressive
    assert "multiple substantive user turns" in aggressive
    assert "one-off subtask" in aggressive
    assert conservative != aggressive


def test_prompt_is_evidence_first_and_deanchors_existing_title():
    _, llm, t = make()
    t.evaluate("s1", force=True)
    system = llm.calls[-1]["messages"][0]["content"]
    prompt = llm.calls[-1]["messages"][1]["content"]

    assert "Evidence before titles" in system
    assert "hypotheses to evaluate, not evidence" in system
    assert "Assistant text may clarify a user goal but must not create a new subject" in system
    assert "untrusted data" in system
    assert prompt.index("开头内容") < prompt.index("当前标题：")
