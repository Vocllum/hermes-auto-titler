"""消息提取与配置加载测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_auto_titler.config import (
    DEFAULTS,
    disable_builtin_title_generation,
    load_config,
    save_config,
)
from hermes_auto_titler.messages import (
    clean_captured_text,
    load_context,
    load_context_with_summary,
    message_text,
)


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


def test_load_context_applies_per_turn_role_quota():
    conv = [
        {"role": "assistant", "content": "orphan-before-user"},
        {"role": "user", "content": "m1"},
        {"role": "assistant", "content": "a1-step"},
        {"role": "assistant", "content": "a1-final"},
        {"role": "user", "content": "m2"},
        {"role": "assistant", "content": "a2-step"},
        {"role": "assistant", "content": "a2-final"},
        {"role": "user", "content": "m3"},
        {"role": "assistant", "content": "a3-step"},
        {"role": "assistant", "content": "a3-final"},
    ]
    recent, all_user, opening = load_context(
        FakeDB(conv), "s1", recent_turns=2, include_all_user=True, opening_turns=2
    )
    assert opening == [
        ("user", "m1"), ("assistant", "a1-final"),
        ("user", "m2"), ("assistant", "a2-final"),
    ]
    assert recent == [
        ("user", "m2"), ("assistant", "a2-final"),
        ("user", "m3"), ("assistant", "a3-final"),
    ]
    assert all_user == [("user", "m1"), ("user", "m2"), ("user", "m3")]
    assert all("orphan" not in text and "-step" not in text for _, text in opening + recent)


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
        {"role": "user", "content": "MEMORY_MAINTENANCE_SUMMARY\n\n定时记忆维护完成"},
        {"role": "user", "content": "[Your active task list was preserved across context compression]\n- [>] verify"},
        {"role": "user", "content": "真实提问一"},
        {"role": "assistant", "content": "[ASYNC DELEGATION BATCH COMPLETE — deleg_abc] 一堆子代理结果"},
        {"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted..."},
        {"role": "user", "content": "真实提问二"},
        {"role": "assistant", "content": "正常回复"},
        {"role": "user", "content": "[System note: Your previous turn was interrupted mid-run]"},
        {"role": "user", "content": "[Recent Summary (d0, node 1)] ## 当前状态"},
    ]
    db = FakeDB(conv)
    recent, all_user, opening, summary = load_context_with_summary(
        db, "s1", recent_turns=2, include_all_user=True, opening_turns=2
    )
    roles_texts = [t for _, t in recent]
    assert "真实提问一" in roles_texts
    assert "真实提问二" in roles_texts
    assert "正常回复" in roles_texts
    # 纯系统通知噪声全部被过滤；摘要不混入 opening
    assert all("[System" not in t and "[ASYNC" not in t and "[CONTEXT" not in t
               and "[Your active task list" not in t
               for _, t in recent + opening + all_user)
    assert all_user == [("user", "真实提问一"), ("user", "真实提问二")]
    assert all("[Recent" not in t for _, t in recent + opening + all_user)
    # 有真实 opening 时，不提供 summary hint；摘要不能冒充 opening。
    assert summary is None
    assert opening[0] == ("user", "真实提问一")


def test_clean_captured_text_extracts_real_message_from_handoff_wrapper():
    wrapped = "[STILL IN PROGRESS — continue the task] 真实用户意图"
    assert clean_captured_text(wrapped) == "真实用户意图"

    compacted = (
        "[CONTEXT COMPACTION — REFERENCE ONLY]\n"
        "这里是旧摘要，不应进入标题输入。\n"
        "--- END OF CONTEXT SUMMARY — respond to the message below ---\n\n"
        "[STILL IN PROGRESS — continue the task] 当前真实请求"
    )
    assert clean_captured_text(compacted) == "当前真实请求"


def test_clean_captured_text_discards_unfinished_handoff_and_system_notice():
    assert clean_captured_text("[CONTEXT COMPACTION — REFERENCE ONLY] 还没有结束") is None
    assert clean_captured_text("[System: model changed]") is None


def test_load_context_deduplicates_replayed_adjacent_user_messages():
    conv = [
        {"role": "user", "content": "[STILL IN PROGRESS — replay] 同一个请求"},
        {"role": "user", "content": "同一个请求"},
        {"role": "assistant", "content": "已处理"},
        {"role": "user", "content": "同一个请求"},
    ]
    _, all_user, opening, _ = load_context_with_summary(
        FakeDB(conv), "s1", recent_turns=2, include_all_user=True, opening_turns=2
    )
    assert all_user == [("user", "同一个请求"), ("user", "同一个请求")]
    assert opening == [
        ("user", "同一个请求"),
        ("assistant", "已处理"),
        ("user", "同一个请求"),
    ]


def test_load_context_summary_hint_only_when_opening_is_missing():
    conv = [
        {"role": "user", "content": "[Session Arc Summary (d1, node 73)] # 当前焦点：X 项目开发"},
        {"role": "user", "content": "m1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "[Recent Summary (d0, node 5)] # 更早的历史"},
        {"role": "user", "content": "m2"},
    ]
    _, all_user, opening, summary = load_context_with_summary(
        FakeDB(conv), "s1", recent_turns=2, include_all_user=True, opening_turns=2
    )
    # 摘要永远不进入 opening；最早摘要单独作为弱提示
    assert opening[0] == ("user", "m1")
    assert ("assistant", "a1") in opening
    assert summary.startswith("[Session Arc Summary")
    assert all_user == [("user", "m1"), ("user", "m2")]

    # 真实 opening 在摘要之前时，不提供 summary hint
    real_opening = [
        {"role": "user", "content": "真正的开头"},
        {"role": "assistant", "content": "开头回复"},
        {"role": "user", "content": "[Session Arc Summary (d1, node 2)] 压缩摘要"},
        {"role": "user", "content": "后续消息"},
    ]
    _, _, op2, summary2 = load_context_with_summary(
        FakeDB(real_opening), "s1", recent_turns=2, include_all_user=True, opening_turns=1
    )
    assert summary2 is None
    assert op2 == [("user", "真正的开头"), ("assistant", "开头回复")]


def test_load_context_recognizes_durable_summary_prefix():
    """Hermes 0.20+ 的 Durable Summary 压缩标记必须被识别为摘要（2026-08-12）。"""
    conv = [
        {"role": "user", "content": "[Durable Summary (d2, node 137)] # 当前重点：标题插件审查"},
        {"role": "user", "content": "m1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "m2"},
    ]
    _, all_user, opening, summary = load_context_with_summary(
        FakeDB(conv), "s1", recent_turns=2, include_all_user=True, opening_turns=2
    )
    assert opening[0] == ("user", "m1")
    assert summary.startswith("[Durable Summary")
    assert all_user == [("user", "m1"), ("user", "m2")]


def test_load_context_summary_hint_truncated():
    conv = [
        {"role": "user", "content": "[Recent Summary (d0, node 1)] " + "y" * 500},
        {"role": "user", "content": "m1"},
    ]
    _, _, opening, summary = load_context_with_summary(
        FakeDB(conv), "s1", recent_turns=2, include_all_user=True, opening_turns=1,
        preview_chars=200,
    )
    assert opening == [("user", "m1")]
    assert summary is not None and len(summary) <= 201
    assert summary.endswith("…")


def test_load_context_summary_hint_summary_chars_override():
    conv = [
        {"role": "user", "content": "[Recent Summary (d0, node 1)] " + "y" * 500},
        {"role": "user", "content": "m1"},
    ]
    # summary_chars>0 时摘要按它截断，而不是 preview_chars
    _, _, _, summary = load_context_with_summary(
        FakeDB(conv), "s1", recent_turns=2, include_all_user=True, opening_turns=1,
        preview_chars=200, summary_chars=400,
    )
    assert summary is not None and len(summary) <= 401
    assert summary.endswith("…")
    # summary_chars=0（默认）沿用 preview_chars
    _, _, _, summary2 = load_context_with_summary(
        FakeDB(conv), "s1", recent_turns=2, include_all_user=True, opening_turns=1,
        preview_chars=200,
    )
    assert len(summary2) <= 201


def test_load_context_filters_cron_maintenance_marker():
    conv = [
        {"role": "user", "content": "真实任务"},
        {"role": "assistant", "content": "MEMORY_MAINTENANCE_SUMMARY\\n\\n已完成记忆维护"},
        {"role": "user", "content": "继续真实任务"},
    ]
    recent, all_user, opening = load_context(
        FakeDB(conv), "s1", recent_turns=2, include_all_user=True, opening_turns=2
    )
    texts = [t for _, t in recent + opening + all_user]
    assert all("MEMORY_MAINTENANCE_SUMMARY" not in t for t in texts)
    assert all_user == [("user", "真实任务"), ("user", "继续真实任务")]


def test_display_width_counts_columns():
    from hermes_auto_titler.messages import char_cols, display_width

    # 中文/全角 = 2 列，英文/数字/半角 = 1 列
    assert char_cols("中") == 2
    assert char_cols("a") == 1
    assert char_cols("1") == 1
    assert char_cols("：") == 2  # 全角冒号
    assert char_cols(":") == 1  # 半角冒号
    assert display_width("搜索方案对比") == 12
    assert display_width("Alpha Instrument") == 16
    # 中文 12 + 全角冒号 2 + Firecrawl 9 + 空格 1 + vs 2 + 空格 1 + AnySearch 9
    assert display_width("搜索方案对比：Firecrawl vs AnySearch") == 36


def test_truncate_to_width_breaks_at_boundary():
    from hermes_auto_titler.messages import truncate_to_width

    # 不超限原样
    assert truncate_to_width("简短标题", 40) == "简短标题"
    # 超限回退到最近分隔符（空格），不留尾随分隔符
    assert truncate_to_width("Codex opencode-go 配置迁移", 20) == "Codex opencode-go"
    # 超限回退到标点
    assert truncate_to_width("搜索方案对比：Firecrawl vs AnySearch", 14) == "搜索方案对比"
    # 纯长词（无分隔符）硬切，不产生半列：12 列 = 6 个中文字
    assert truncate_to_width("这是一个非常非常长的中文标题测试用例", 12) == "这是一个非常"
    # 宽度精确边界：恰好等于 max_cols 不截
    assert truncate_to_width("中文标题", 8) == "中文标题"
    assert truncate_to_width("中文标题啊", 8) == "中文标题"  # 第 9 列放不下第 5 个字


def test_smart_preview_extracts_first_last_sentence():
    from hermes_auto_titler.messages import smart_preview

    # 正常句子：保留首句 + 尾句
    text = "第一句说明意图。中间有大量执行细节，这里省略。最后一句是结论！"
    out = smart_preview(text, 20)
    assert "第一句说明意图" in out and "最后一句是结论" in out
    assert "中间有大量执行细节" not in out
    # 短消息原样
    assert smart_preview("短消息", 300) == "短消息"
    # 无句子边界长串：硬切前 2/3 + 后 1/3
    blob = "x" * 1000
    out = smart_preview(blob, 90)
    assert out.startswith("x" * 60) and out.endswith("x" * 30)
    assert "…" in out
    # 换行也算边界
    multi = "第一行\n第二行\n第三行"
    out = smart_preview(multi, 12)
    assert "第一行" in out and "第三行" in out

    # 真实故障回归：长命令/配置后跟倒数第二句核心请求，尾部窗口必须完整容纳
    realistic = (
        "我昨天运行过git clone https://github.com/MDX-Tom/gpt-instruct.git\n"
        "cd gpt-instruct\n\n"
        "# 预览稳定版，不写入配置\n"
        "python3 codex-instruct.py --apply --version gpt-5.6-v45 --dry-run\n\n"
        "# 部署当前稳定版\n"
        "python3 codex-instruct.py --apply --version gpt-5.6-v45\n\n"
        "# 部署 gpt-6-astra-v1 正式版\n"
        "python3 codex-instruct.py --apply --version gpt-6-v1。帮我清理掉。Windows"
    )
    res = smart_preview(realistic, 300)
    assert "git clone" in res
    assert "帮我清理掉" in res
    assert "Windows" in res


def test_sample_user_messages_head_tail():
    from hermes_auto_titler.messages import sample_user_messages

    users = [(f"user", f"m{i}") for i in range(100)]
    out = sample_user_messages(users, 40)
    assert len(out) == 40
    # 开头 1 条（起点锚点）+ 最近 39 条（当前意图）
    assert out[0] == ("user", "m0")
    assert out[1] == ("user", "m61")
    assert out[-1] == ("user", "m99")
    # 不超限或 0 = 不限
    assert sample_user_messages(users, 0) == users
    assert len(sample_user_messages(users[:20], 40)) == 20
    # 回归：threshold=1 时 tail=0，users[-0:] 会返回全部（101 条）——必须只留 1 条
    out1 = sample_user_messages(users, 1)
    assert len(out1) == 1 and out1[0] == ("user", "m0")


def test_load_context_user_message_limits():
    conv = [{"role": "user", "content": f"消息{i}内容。"} for i in range(20)]
    db = FakeDB(conv)
    _, all_user, _ = load_context(
        db, "s1", recent_turns=1, include_all_user=True,
        user_message_threshold=8, user_message_preview_chars=10,
    )
    assert len(all_user) == 8  # 前 2 + 后 6
    assert all_user[0][1].startswith("消息0")
    assert all_user[-1][1].startswith("消息19")
    # 首尾句提取生效（短消息原样，超长的被提取）
    assert all(len(t) <= 20 for _, t in all_user)


def test_config_load_defaults_and_override(tmp_path):
    cfg = load_config(path=tmp_path / "missing.yaml")
    assert cfg["enabled"] is True
    assert cfg["every_n_turns"] == 4
    assert cfg["strategy"] == "conservative"
    # 18 字符的显式仓库名 hermes-auto-titler 必须能原样存活，
    # 另留少量中文意图空间；12 字符仍只是软目标。
    assert cfg["max_title_length"] == 24

    p = tmp_path / "config.yaml"
    p.write_text("strategy: aggressive\nmax_title_length: 30\n", encoding="utf-8")
    cfg2 = load_config(path=p)
    assert cfg2["strategy"] == "aggressive"
    assert cfg2["max_title_length"] == 30
    assert cfg2["every_n_turns"] == 4  # 未覆盖的键保持默认

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


def test_config_has_new_keys(tmp_path):
    cfg = load_config(path=tmp_path / "missing.yaml")
    assert cfg["early_turn_eval"] is False
    assert cfg["first_title_mode"] == "builtin"
    assert cfg["retitle_summary_chars"] == 12000


def test_disable_builtin_title_generation_updates_host_config(monkeypatch):
    host = {"auxiliary": {"title_generation": {"enabled": True, "model": "Free"}}}
    saved = []
    fake = type("HostConfig", (), {
        "load_config": staticmethod(lambda: host),
        "save_config": staticmethod(lambda cfg: saved.append(cfg)),
    })
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake)

    assert disable_builtin_title_generation() is True
    assert host["auxiliary"]["title_generation"]["enabled"] is False
    assert saved == [host]
    assert disable_builtin_title_generation() is False


def test_disable_builtin_title_generation_creates_missing_sections(monkeypatch):
    host = {}
    saved = []
    fake = type("HostConfig", (), {
        "load_config": staticmethod(lambda: host),
        "save_config": staticmethod(lambda cfg: saved.append(cfg)),
    })
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake)

    assert disable_builtin_title_generation() is True
    assert saved == [{"auxiliary": {"title_generation": {"enabled": False}}}]


def test_config_coerces_bool_and_int_strings(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "enabled: \"true\"\nevery_n_turns: \"4\"\nearly_turn_eval: \"false\"\non_close: \"on\"\n",
        encoding="utf-8",
    )
    cfg = load_config(path=p)
    assert cfg["enabled"] is True
    assert cfg["every_n_turns"] == 4
    assert cfg["early_turn_eval"] is False
    assert cfg["on_close"] is True


def test_config_invalid_values_fall_back_to_defaults(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "every_n_turns: abc\nenabled: maybe\nstrategy: bogus\ntitle_style: weird\n"
        "opening_turns: 0\nrecent_turns: 0\nmin_interval_minutes: -1\n",
        encoding="utf-8",
    )
    cfg = load_config(path=p)
    assert cfg["every_n_turns"] == 4  # 非法整数回退默认，不崩溃
    assert cfg["enabled"] is True  # 非法 bool 回退默认，不静默变 False
    assert cfg["strategy"] == "conservative"  # 非法枚举回退默认
    assert cfg["title_style"] == "concise"
    assert cfg["opening_turns"] == 1  # 钳制下限
    assert cfg["recent_turns"] == 1
    assert cfg["min_interval_minutes"] == 0.0


def test_config_yaml_list_does_not_crash(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("- just\n- a\n- list\n", encoding="utf-8")
    cfg = load_config(path=p)
    assert cfg["enabled"] is True
    assert cfg["every_n_turns"] == 4


# -- 滞后机制配置：rename_confirmations / renames_per_hour ----------------------

def test_rename_confirmations_defaults_and_clamps(tmp_path):
    p = tmp_path / "config.yaml"

    # 缺省：0（关闭确认，单次直接写）
    p.write_text("enabled: true\n", encoding="utf-8")
    cfg = load_config(path=p)
    assert cfg["rename_confirmations"] == 0

    # 合法值 1/2/3
    p.write_text("rename_confirmations: 1\n", encoding="utf-8")
    assert load_config(path=p)["rename_confirmations"] == 1
    p.write_text("rename_confirmations: 2\n", encoding="utf-8")
    assert load_config(path=p)["rename_confirmations"] == 2
    p.write_text("rename_confirmations: 3\n", encoding="utf-8")
    assert load_config(path=p)["rename_confirmations"] == 3

    # 下限钳到 0，非负整数原样保留（不设多余的人为上限）
    p.write_text("rename_confirmations: 0\n", encoding="utf-8")
    assert load_config(path=p)["rename_confirmations"] == 0
    p.write_text("rename_confirmations: -2\n", encoding="utf-8")
    assert load_config(path=p)["rename_confirmations"] == 0
    p.write_text("rename_confirmations: 99\n", encoding="utf-8")
    assert load_config(path=p)["rename_confirmations"] == 99

    # 非法浮点回退默认
    p.write_text("rename_confirmations: 1.5\n", encoding="utf-8")
    cfg = load_config(path=p)
    assert cfg["rename_confirmations"] == DEFAULTS["rename_confirmations"]


def test_renames_per_hour_zero_disables_cap(tmp_path):
    p = tmp_path / "config.yaml"
    # 缺省 0 = 不设频次上限
    p.write_text("enabled: true\n", encoding="utf-8")
    cfg = load_config(path=p)
    assert cfg["renames_per_hour"] == 0
    # 负值钳到 0
    p.write_text("renames_per_hour: -3\n", encoding="utf-8")
    assert load_config(path=p)["renames_per_hour"] == 0
    p.write_text("renames_per_hour: 6\n", encoding="utf-8")
    assert load_config(path=p)["renames_per_hour"] == 6



# -- 配置加固：数值布尔 / NaN / inf / 非字符串 model / 负长度 -------------------

def test_config_rejects_numeric_bool_outside_01(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("enabled: 2\non_close: -1\nearly_turn_eval: 0.5\n", encoding="utf-8")
    cfg = load_config(path=p)
    assert cfg["enabled"] is True  # 回退默认，不静默按 truthy/falsy 解释
    assert cfg["on_close"] is True
    assert cfg["early_turn_eval"] is False


def test_config_accepts_numeric_bool_01(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("enabled: 0\non_close: 1\nearly_turn_eval: 0\n", encoding="utf-8")
    cfg = load_config(path=p)
    assert cfg["enabled"] is False
    assert cfg["on_close"] is True
    assert cfg["early_turn_eval"] is False


def test_config_rejects_fractional_integer_values(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("every_n_turns: 2.5\nopening_turns: 1.5\n", encoding="utf-8")
    cfg = load_config(path=p)
    assert cfg["every_n_turns"] == DEFAULTS["every_n_turns"]
    assert cfg["opening_turns"] == DEFAULTS["opening_turns"]


def test_config_rejects_non_finite_interval(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("min_interval_minutes: .nan\n", encoding="utf-8")
    cfg = load_config(path=p)
    assert cfg["min_interval_minutes"] == 5.0  # 非有限值回退默认
    p.write_text("min_interval_minutes: .inf\n", encoding="utf-8")
    cfg2 = load_config(path=p)
    assert cfg2["min_interval_minutes"] == 5.0


def test_config_rejects_nonstring_model(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("model: 42\n", encoding="utf-8")
    cfg = load_config(path=p)
    assert cfg["model"] == ""
    p.write_text("model: [a, b]\n", encoding="utf-8")
    cfg2 = load_config(path=p)
    assert cfg2["model"] == ""
    p.write_text("model: true\n", encoding="utf-8")
    cfg3 = load_config(path=p)
    assert cfg3["model"] == ""


def test_config_clamps_negative_lengths_to_zero(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "preview_chars: -5\nuser_message_threshold: -3\n"
        "user_message_preview_chars: -100\nretitle_summary_chars: -1\n",
        encoding="utf-8",
    )
    cfg = load_config(path=p)
    assert cfg["preview_chars"] == 0
    assert cfg["user_message_threshold"] == 0
    assert cfg["user_message_preview_chars"] == 0
    assert cfg["retitle_summary_chars"] == 0


def test_config_command_rejects_nan_inf_and_bad_numeric_bool(monkeypatch, tmp_path):
    from hermes_auto_titler.commands import make_handler
    from hermes_auto_titler.config import load_config
    from hermes_auto_titler.titler import AutoTitler

    saved = []
    monkeypatch.setattr(
        "hermes_auto_titler.config.save_config",
        lambda cfg, path=None: saved.append(dict(cfg)),
    )
    db = type("DB", (), {})()
    t = AutoTitler(
        type("Ctx", (), {"llm": None})(),
        load_config(path=tmp_path / "missing.yaml"),
        db=db,
    )
    h = make_handler(t)
    # NaN / inf（YAML 与命令两条入口共用 coerce_value 校验）
    for bad in ("nan", "inf", "-inf"):
        assert "值无效" in h(f"config min_interval_minutes {bad}")
        assert t.cfg["min_interval_minutes"] == 5.0
    # 数值布尔只接受 0/1
    for bad in ("2", "-1", "0.5"):
        assert "值无效" in h(f"config enabled {bad}")
        assert t.cfg["enabled"] is True
    h("config enabled 0")
    assert t.cfg["enabled"] is False
    h("config enabled 1")
    assert t.cfg["enabled"] is True
    # 负长度钳到 0（命令入口与 YAML 一致）
    h("config preview_chars -5")
    assert t.cfg["preview_chars"] == 0
    h("config retitle_summary_chars -1")
    assert t.cfg["retitle_summary_chars"] == 0
    h("config user_message_threshold -3")
    assert t.cfg["user_message_threshold"] == 0
