"""消息提取与配置加载测试。"""

import sys

sys.path.insert(0, "..")

from hermes_auto_titler.config import DEFAULTS, load_config, save_config
from hermes_auto_titler.messages import load_context, message_text


def test_message_text_plain_string():
    assert message_text("hello") == "hello"


def test_message_text_parts_filters_attachments():
    content = [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "url": "data:..."},
        {"type": "file", "name": "report.pdf"},
    ]
    text = message_text(content)
    assert "看图" in text
    assert "[图片]" in text
    assert "[文件: report.pdf]" in text
    assert "data:" not in text


class FakeDB:
    def __init__(self, conv):
        self.conv = conv

    def get_messages_as_conversation(self, session_id, include_ancestors=False):
        return self.conv


def test_load_context_recent_turns_and_all_user():
    conv = [
        {"role": "user", "content": "m1"},
        {"role": "assistant", "content": "a1"},
        {"role": "tool", "content": "tool-output"},
        {"role": "user", "content": "m2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "m3"},
        {"role": "assistant", "content": "a3"},
    ]
    db = FakeDB(conv)
    recent, all_user, opening = load_context(db, "s1", recent_turns=2, include_all_user=True)
    assert recent == [("user", "m2"), ("assistant", "a2"), ("user", "m3"), ("assistant", "a3")]
    assert all_user == [("user", "m1"), ("user", "m2"), ("user", "m3")]
    # 开头默认取前 2 轮 user 消息及其 assistant 回应
    assert opening == [("user", "m1"), ("assistant", "a1"), ("user", "m2"), ("assistant", "a2")]
    # tool 消息被过滤；include_all_user=False 时返回空
    _, none, _ = load_context(db, "s1", recent_turns=2, include_all_user=False)
    assert none == []
    # opening_turns 可调
    _, _, op1 = load_context(db, "s1", recent_turns=2, include_all_user=True, opening_turns=1)
    assert op1 == [("user", "m1"), ("assistant", "a1")]


def test_load_context_ignore_model_messages():
    conv = [
        {"role": "user", "content": "m1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "m2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "m3"},
    ]
    db = FakeDB(conv)
    recent, all_user, opening = load_context(
        db, "s1", recent_turns=2, include_all_user=True, ignore_model_messages=True
    )
    assert recent == [("user", "m2"), ("user", "m3")]
    assert all_user == [("user", "m1"), ("user", "m2"), ("user", "m3")]
    assert opening == [("user", "m1"), ("user", "m2")]


def test_load_context_preview_chars():
    conv = [
        {"role": "user", "content": "短消息"},
        {"role": "assistant", "content": "x" * 500},
        {"role": "user", "content": "y" * 500},
    ]
    db = FakeDB(conv)
    recent, all_user, opening = load_context(
        db, "s1", recent_turns=2, include_all_user=True, preview_chars=200
    )
    # opening/recent 的超长消息被截断到 200 字符 + 省略号；短消息原样
    for _, text in recent + opening:
        assert len(text) <= 201
    assert any(t == "x" * 200 + "…" for _, t in recent + opening)
    assert any(t == "y" * 200 + "…" for _, t in recent + opening)
    assert any(t == "短消息" for _, t in opening)
    # 用户消息（意图轨迹）不截断
    assert len(all_user[0][1]) == 3
    assert len(all_user[1][1]) == 500
    # preview_chars=0 不截断
    recent0, _, _ = load_context(
        db, "s1", recent_turns=1, include_all_user=True, preview_chars=0
    )
    assert any(len(t) == 500 for _, t in recent0)


def test_load_context_filters_system_noise():
    conv = [
        {"role": "user", "content": "[System: The active model has changed to deepseek-v4-flash]"},
        {"role": "user", "content": "真实提问一"},
        {"role": "assistant", "content": "[ASYNC DELEGATION BATCH COMPLETE — deleg_abc] 一堆子代理结果"},
        {"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted..."},
        {"role": "user", "content": "真实提问二"},
        {"role": "assistant", "content": "正常回复"},
        {"role": "user", "content": "[System note: Your previous turn was interrupted mid-run]"},
        {"role": "user", "content": "[Recent Summary (d0, node 1)] ## 当前状态"},
    ]
    db = FakeDB(conv)
    recent, all_user, opening = load_context(
        db, "s1", recent_turns=2, include_all_user=True, opening_turns=2
    )
    roles_texts = [t for _, t in recent]
    assert "真实提问一" in roles_texts
    assert "真实提问二" in roles_texts
    assert "正常回复" in roles_texts
    # 系统噪声全部被过滤
    assert all("[System" not in t and "[ASYNC" not in t and "[CONTEXT" not in t
               and "[Recent" not in t for _, t in recent + opening + all_user)
    assert all_user == [("user", "真实提问一"), ("user", "真实提问二")]


def test_config_load_defaults_and_override(tmp_path):
    cfg = load_config(path=tmp_path / "missing.yaml")
    assert cfg["enabled"] is True
    assert cfg["every_n_turns"] == 3
    assert cfg["strategy"] == "conservative"

    p = tmp_path / "config.yaml"
    p.write_text("strategy: aggressive\nmax_title_length: 30\n", encoding="utf-8")
    cfg2 = load_config(path=p)
    assert cfg2["strategy"] == "aggressive"
    assert cfg2["max_title_length"] == 30
    assert cfg2["every_n_turns"] == 3  # 未覆盖的键保持默认

    save_config(cfg2, path=p)
    cfg3 = load_config(path=p)
    assert cfg3["strategy"] == "aggressive"
    assert cfg3["max_title_length"] == 30
    assert cfg3["enabled"] is True


def test_config_sanitizes_bad_values(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("strategy: nonsense\nmax_title_length: 99999\nevery_n_turns: 0\n", encoding="utf-8")
    cfg = load_config(path=p)
    assert cfg["strategy"] == "conservative"
    assert cfg["max_title_length"] == 100  # 上限 100
    assert cfg["every_n_turns"] == 1  # 下限 1
