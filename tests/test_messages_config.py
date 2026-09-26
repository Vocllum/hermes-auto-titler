"""消息提取与配置加载测试。"""

import sys
from pathlib import Path
from typing import List

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_auto_titler.config import (
    DEFAULTS,
    load_config,
    save_config,
)
from hermes_auto_titler.policy import AutoTitler
from hermes_auto_titler.messages import (
    clean_captured_text,
    has_compaction_handoff,
    is_compaction_carrier,
    load_context,
    load_context_with_summary,
    message_text,
    strip_group_chat_envelope,
    summary_preview,
    unpack_system_wrapper,
    _extract_compaction_summary,
    _summary_sections,
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


class _NoLlmCtx:
    """Minimal ctx: the config-set path never reaches the LLM."""

    llm = None

    def get_config(self, key, default=None):
        return default


def test_manifest_settings_order_and_defaults_match_runtime():
    root = Path(__file__).resolve().parent.parent
    schema = yaml.safe_load((root / "plugin.yaml").read_text())["config_schema"]
    keys = list(schema)
    assert len(keys) == len(set(keys)) == len(DEFAULTS) - 1  # internal 'model' is reserved
    assert keys[:9] == [
        "enabled", "first_title_mode", "strategy", "title_style",
        "every_n_turns", "on_close", "rename_confirmations",
        "title_model", "provider",
    ]
    assert keys[-1] == "max_renames_per_session"
    for key, field in schema.items():
        expected = DEFAULTS[key]
        # Hermes' numeric field serializes an optional None default as zero.
        if key == "max_title_length":
            expected = 0
        assert field["default"] == expected, key


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
    # opening 不与 recent 重复：m2/a2 已进 recent，opening 只保留它之前的轮次
    assert opening == [("user", "m1"), ("assistant", "a1")]
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
    # 可见轮次有 3 轮、recent_turns=2 时 opening 只有第 1 轮不重叠
    assert opening == [("user", "m1"), ("assistant", "a1-final")]
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
    assert opening == [("user", "m1")]


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
    assert any(t == "x" * 200 + "…" for _, t in recent)
    assert any(t == "y" * 200 + "…" for _, t in recent)
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
    # 摘要不冒充 opening、recent 或用户意图轨迹；它作为独立历史锚点恒常透传，
    # 即使可见历史里已经有真实 opening（旧的 not-saw_visible_opening 门禁会在
    # 出现任意真实用户消息时把摘要整体丢弃，实测 141/225 个会话因此看不到摘要）。
    assert opening[0] == ("user", "真实提问一")
    assert summary == "[Recent Summary (d0, node 1)] ## 当前状态"


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


def test_load_context_prefers_latest_compaction_summary_by_timestamp():
    # Replay/rotation can make transcript row order differ from event chronology.
    old = "## Historical Task Snapshot\nold history"
    new = "## Historical Task Snapshot\nnewer history"
    conv = [
        {
            "role": "assistant",
            "content": "[CONTEXT COMPACTION — REFERENCE ONLY]\navoid repeating it:\n" + new + "\n--- END OF CONTEXT SUMMARY ---",
            "timestamp": 200,
        },
        {
            "role": "user",
            "content": "[CONTEXT COMPACTION — REFERENCE ONLY]\navoid repeating it:\n" + old + "\n--- END OF CONTEXT SUMMARY ---",
            "timestamp": 100,
        },
    ]
    _, _, _, summary = load_context_with_summary(
        FakeDB(conv), "s1", recent_turns=2, include_all_user=True, preview_chars=0, summary_chars=0
    )
    assert summary is not None
    assert summary.startswith(new)
    assert old in summary


def test_clean_captured_text_strips_group_envelope_after_replay_marker():
    # Regression from SessionDB session 20260920_223346_3281b7: a persisted
    # [STILL IN PROGRESS] line precedes the ordinary group-chat envelope.
    wrapped = (
        "[STILL IN PROGRESS — this is the active request, restated after the "
        "compaction boundary because it was not finished yet. Continue it; "
        "do not start over.]\n"
        "[Group chat: \"Lattice\"] You are @lynn, one participant in a group chat "
        "with @aperture, @eclipse, @voxel, @zenith and the user.\n\n"
        "New messages in the room since your last turn (oldest first):\n"
        "  You (user): continue the AutoTitler review\n\n"
        "Rules for this room:\n"
        "- Reply only when you have something new.\n"
    )

    assert clean_captured_text(wrapped) == "You (user): continue the AutoTitler review"


def test_clean_captured_text_truncates_injected_tail_after_system_wrapper():
    # Mirrors all five task-list payloads found in the 352-session reproduction set.
    cases = [
        (
            "[System: The active model for this chat has changed to combo/Free via provider opencodex.]\n"
            "真实请求一\n\n[Your active task list was preserved across context compression]\n- [>] 任务清单",
            "真实请求一",
        ),
        (
            "[System: The active model for this chat has changed to combo/Free via provider opencodex.]\n"
            "真实请求二\n[Your active task list was preserved across context compression]\n- [>] 继续项",
            "真实请求二",
        ),
        (
            "[System: The active model for this chat has changed to combo/Free via provider opencodex.]\n"
            "真实请求三\n[Your active task list was preserved across context compression]\n- [>] 检查状态",
            "真实请求三",
        ),
        (
            "[System: The active model for this chat has changed to combo/Free via provider opencodex.]\n"
            "真实请求四\n[Your active task list was preserved across context compression]\n- [>] 完成验证",
            "真实请求四",
        ),
        (
            "[System note: Your previous turn was interrupted mid-run]\n"
            "真实请求五\n[Your active task list was preserved across context compression]\n- [>] 继续项",
            "真实请求五",
        ),
        (
            "[System: The active model for this chat has changed to combo/Free via provider opencodex.]\n"
            "真实请求六\n[Skills pruned during compression — reload before acting]\n"
            "[SKILL_PRUNED: private skill body]",
            "真实请求六",
        ),
    ]
    for wrapped, expected in cases:
        assert clean_captured_text(wrapped) == expected


def test_clean_captured_text_truncates_task_list_in_unwrapped_message():
    assert clean_captured_text(
        "普通用户请求\n[Your active task list was preserved across context compression]\n- [>] 任务清单"
    ) == "普通用户请求"


def test_clean_captured_text_discards_unfinished_handoff_and_system_notice():
    assert clean_captured_text("[CONTEXT COMPACTION — REFERENCE ONLY] 还没有结束") is None
    assert clean_captured_text("[System: model changed]") is None
    assert clean_captured_text("[Skills pruned during compression — reload before acting]") is None
    assert clean_captured_text("[SKILL_PRUNED: private skill body]") is None


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
    # 只有 1 轮真实 opening，且它已在 recent 里：opening 降级保留该轮而非重复或消失
    assert opening == [("user", "同一个请求")]


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
    # 摘要永远不进入 opening；它是独立的历史锚点，恒常透传
    # 可见轮次只有 2 轮、recent_turns=2 时 opening 落在 recent 内：
    # 降级保留首批轮次的用户消息，仍满足 opening[0] 这个 subject 线索锚点
    assert opening[0] == ("user", "m1")
    assert summary.startswith("[Session Arc Summary")
    assert all_user == [("user", "m1"), ("user", "m2")]

    # 真实 opening 在摘要之前时，摘要依然作为历史锚点透传（不再被门禁灭顶），
    # 但它绝不进入 opening / recent / 用户意图轨迹
    real_opening = [
        {"role": "user", "content": "真正的开头"},
        {"role": "assistant", "content": "开头回复"},
        {"role": "user", "content": "[Session Arc Summary (d1, node 2)] 压缩摘要"},
        {"role": "user", "content": "后续消息"},
    ]
    _, _, op2, summary2 = load_context_with_summary(
        FakeDB(real_opening), "s1", recent_turns=2, include_all_user=True, opening_turns=1
    )
    assert summary2 == "[Session Arc Summary (d1, node 2)] 压缩摘要"
    # 2 个真实轮次、recent_turns=2 时 opening 全部落在 recent 内：
    # 降级只保留首批轮次的用户消息，assistant 回复由 recent 段承载
    assert op2 == [("user", "真正的开头")]


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


REAL_SHAPED_SUMMARY = (
    "[CONTEXT COMPRESSION — REFERENCE ONLY] 已整理压缩块，以下是完整历史。\n\n"
    "## Historical Task Snapshot\n"
    "User asked (deterministic, from compacted turns): 这是前 300 字符里的快照复述段，"
    "用来占位，模拟真实压缩块里先出现的框架指令与历史复述。\n\n"
    "## Goal\n"
    "1. 【已完成】把测试补齐。\n"
    "2. 【进行中】修复压缩会话的消息提取路径。\n\n"
    "## Blocked\n"
    "- 探针输出被上下文压缩丢失，需要 session_search 恢复，勿猜。\n\n"
    "## User Messages (verbatim, newest first)\n"
    "> 问题解决后先提交不 release，直到我宣布下一版本\n"
)


def test_summary_sections_splits_on_h2_only():
    text = (
        "# Title (h1, not a split point)\n"
        "intro\n"
        "## First\n"
        "body-one\n"
        "### Sub (h3, not a split point)\n"
        "still-first\n"
        "## Second\n"
        "body-two\n"
    )
    assert _summary_sections(text) == [
        "## First\nbody-one\n### Sub (h3, not a split point)\nstill-first",
        "## Second\nbody-two",
    ]


def test_summary_preview_keeps_tail_sections_a_linear_cut_drops():
    """真实压缩块形状：`## User Messages` 在 10k+ 处，线性前切必然丢掉它。"""
    out = summary_preview(REAL_SHAPED_SUMMARY, 1200)
    # 旧行为：s[:1200] 只有 Goal，User Messages 整节消失
    assert "## Goal" in out
    assert "先提交不 release" in out
    # 预算不被突破（smart_preview 的省略号最多放宽 3）
    assert len(out) <= 1203


def test_summary_preview_drops_middle_sections_when_budget_forces_it():
    """预算只够头尾各一节时，中段必须消失（真实 40k 摘要的形态）。"""
    big = REAL_SHAPED_SUMMARY + "\n" + "\n\n".join(
        "## Middle Section %d\n" % i + ("filler line. " * 40) for i in range(6)
    )
    out = summary_preview(big, 1200)
    kept = [line for line in out.splitlines() if line.startswith("## ")]
    assert kept[0] == "## Historical Task Snapshot"
    # 中段只能以补充分支的窗口形式出现，不能整节入选
    assert not any(s == "## Middle Section %d" % i for i in range(6) for s in kept)
    assert len(out) <= 1203
    # 尾部节超过 tail_budget(480)，以窗口形式到达；其内容必须在
    assert "先提交不 release" in out


def test_summary_preview_falls_back_when_template_has_no_sections():
    """自定义压缩模板没有 markdown 小节时退回句子边界窗口，而不是空串。"""
    blob = "alpha beta. " * 200
    out = summary_preview(blob, 60)
    assert out and len(out) <= 63
    assert out.startswith("alpha beta")


def test_summary_preview_short_text_is_untouched():
    assert summary_preview(REAL_SHAPED_SUMMARY, 100000) == REAL_SHAPED_SUMMARY
    assert summary_preview("", 100) == ""


def test_summary_preview_never_half_cuts_a_tail_section():
    out = summary_preview(REAL_SHAPED_SUMMARY, 900)
    tail = "## User Messages (verbatim, newest first)"
    if tail in out:
        # 整节要么完整保留，要么不出现；半截节会伪造一个结尾
        assert "先提交不 release" in out


# 真实摘要的节长度跨度：79..24732 字符，16 节里 3 节超过 2k。
# 均匀短节的 fixture 造不出这个 bug——尾池必须够到头部已收的节才暴露。
REAL_DENSITY_SECTIONS = [
    ("Historical Task Snapshot", 1480),
    ("Goal", 492),
    ("Constraints & Preferences", 842),
    ("Completed Actions", 2273),
    ("Active State", 627),
    ("Blocked", 79),
    ("Key Decisions", 1227),
    ("Errors & Fixes", 666),
    ("Resolved Questions", 332),
    ("Relevant Files", 798),
    ("Critical Context", 670),
    ("Detailed Session Log (oldest first)", 3400),
    ("Anchor Index (mechanically extracted, exact)", 2618),
    ("User Messages (verbatim, newest first)", 24732),
    ("Context Recovery", 361),
]


def _real_density_summary(scale: int = 1) -> str:
    return "\n\n".join(
        "## %s\n%s" % (name, ("detail line. " * 4000)[:size * scale])
        for name, size in REAL_DENSITY_SECTIONS
    )


def _headings(text: str) -> List[str]:
    return [line for line in text.splitlines() if line.startswith("## ")]


def test_summary_preview_never_injects_the_same_section_twice():
    """真实跨度下尾池会绕过中段够到头部已收的节；同节注入两遍比 opening/recent
    重叠更直接——prompt 里同一段文字 literally 出现两次。"""
    text = _real_density_summary(scale=2)
    for budget in (1200, 2000, 3200, 12000):
        out = summary_preview(text, budget)
        headings = _headings(out)
        assert len(headings) == len(set(headings)), (
            "budget %d duplicated %r" % (budget, sorted(set(headings) - set(set(headings))))
        )
        assert len(out) <= budget + 1


def test_summary_preview_head_pool_wins_then_takes_from_the_tail():
    """头池顺序、尾池倒序，且尾池不许把头池的节再拿一次。"""
    text = _real_density_summary(scale=2)
    out = summary_preview(text, 12000)
    headings = _headings(out)
    names = [name for name, _ in REAL_DENSITY_SECTIONS]
    positions = [names.index(h[3:]) for h in headings if h[3:] in names]
    assert positions == sorted(positions), "sections must stay in document order"
    assert headings[0] == "## Historical Task Snapshot"
    assert headings[-1] == "## Context Recovery"


def test_summary_preview_full_coverage_returns_text_unchanged():
    """预算够吃下全部小节时直接返回，不再走尾池补满分支。"""
    text = _real_density_summary(scale=1)
    assert summary_preview(text, 10 ** 6) == text
    assert len(summary_preview(text, len(text))) == len(text)


def test_summary_preview_fallback_never_repeats_head_sections():
    """尾池一节都装不下时走 smart_preview 补满；对整篇原文取首句会把头池
    已收的小节再带一遍（连标题行都拼出一个假小节）。补满只能覆盖未选中的节。"""
    text = "\n\n".join([
        "## A_head\n" + "aaa " * 40,
        "## B_goal\n" + "bbb " * 40,
        "## C_mid\n" + "ccc " * 6666,      # 20000+ 巨节，谁都不装得下
        "## D_tail\n" + "ddd " * 200,      # 超过 tail_budget(480)
        "## E_tail2\n" + "eee " * 300,     # 超过 tail_budget(480)
    ])
    out = summary_preview(text, 1200)
    # 标题和正文都要判：只判标题数会漏掉「正文重复但标题去重成功」的形状。
    # 注意 "aaa " 是 39 而非 40：小节被 strip()，最后一个 "aaa" 后面没有空格，
    # 不能拿 40 当期望值。
    assert out.count("## A_head") == 1
    assert out.count("aaa ") == 39
    assert out.count("## B_goal") == 1
    assert out.count("bbb ") == 39
    # 尾节内容仍要到达 prompt，不能因为去重把尾部整个丢掉
    assert "eee " in out
    assert len(out) <= 1201


def test_summary_preview_fallback_heading_line_is_same_section_as_its_body():
    """补满窗口不能把标题行粘到另一节的正文上。

    对剩余全文取首句时，首句是某个节的标题行、尾窗是另一节的正文，拼出的
    `## X … <别的节的正文>` 读起来像合法小节，下游按 `## ` 解析会把那段正文
    误判成 X 的内容。窗口只能取单个未选中的节，标题行与正文必须同源。
    """
    text = "\n\n".join([
        "## A_head\n" + "aaa " * 40,
        "## B_goal\n" + "bbb " * 40,
        "## C_mid\n" + "ccc " * 6666,      # 20000+ 巨节，谁都不装得下
        "## D_tail\n" + "ddd " * 200,      # 超过 tail_budget(480)
        "## E_tail2\n" + "eee " * 300,     # 超过 tail_budget(480)
    ])
    for budget in (100, 200, 300, 600, 1200):
        out = summary_preview(text, budget)
        for line in out.splitlines():
            if line.startswith("## ") and " … " in line:
                # 脏形状的判据：标题名对应的正文 token 不能出现在这行里
                heading = line.split(" … ")[0]
                bodies = {
                    "## A_head": "aaa", "## B_goal": "bbb",
                    "## C_mid": "ccc", "## D_tail": "ddd", "## E_tail2": "eee",
                }
                assert bodies[heading] in line, (
                    "fallback glued %r onto another section's body: %r" % (heading, line)
                )
    # 预算紧到只剩窗口时，标题行必须来自未选中的最后一个节
    out = summary_preview(text, 100)
    assert out.startswith("## A_head\n")
    assert "## E_tail2 … eee" in out


def test_summary_preview_fallback_branch_is_actually_reached():
    """上面那条必须真的走进 `if not tail:`；否则断言是空转的。"""
    text = "\n\n".join([
        "## A_head\n" + "aaa " * 40,
        "## B_goal\n" + "bbb " * 40,
        "## C_mid\n" + "ccc " * 6666,
        "## D_tail\n" + "ddd " * 200,
        "## E_tail2\n" + "eee " * 300,
    ])
    sections = _summary_sections(text)
    head_budget, tail_budget = int(1200 * 0.6), 1200 - int(1200 * 0.6)
    taken, used = set(), 0
    for idx, section in enumerate(sections):
        if used >= head_budget:
            break
        if len(section) <= head_budget - used:
            taken.add(idx)
            used += len(section)
        elif not taken:
            taken.add(idx)
            used = head_budget
    fits = [
        idx for idx in range(len(sections) - 1, -1, -1)
        if idx not in taken and len(sections[idx]) <= tail_budget
    ]
    assert not fits, "fixture must leave the tail pool empty for the fallback to run"


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
    assert summary is not None and len(summary) <= 203


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
    assert summary is not None and len(summary) <= 403
    # summary_chars=0（默认）沿用 preview_chars
    _, _, _, summary2 = load_context_with_summary(
        FakeDB(conv), "s1", recent_turns=2, include_all_user=True, opening_turns=1,
        preview_chars=200,
    )
    assert len(summary2) <= 203


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
    # 确定性分层采样：head 包含起点，tail 包含最近意图，中段均匀分布
    assert out[0] == ("user", "m0")
    assert out[1] == ("user", "m1")
    assert out[-1] == ("user", "m99")
    # 验证中段被均匀覆盖（而不是跳过前 60 条）
    assert any(item[1] in ("m21", "m34", "m47", "m60") for item in out)
    # 不超限或 0 = 不限
    assert sample_user_messages(users, 0) == users
    assert len(sample_user_messages(users[:20], 40)) == 20
    # 回归：threshold=1 时只留 1 条
    out1 = sample_user_messages(users, 1)
    assert len(out1) == 1 and out1[0] == ("user", "m0")


def test_load_context_user_message_limits():
    conv = [{"role": "user", "content": f"消息{i}内容。"} for i in range(20)]
    db = FakeDB(conv)
    _, all_user, _ = load_context(
        db, "s1", recent_turns=1, include_all_user=True,
        user_message_threshold=8, user_message_preview_chars=10,
    )
    assert len(all_user) == 8
    assert all_user[0][1].startswith("消息0")
    assert all_user[-1][1].startswith("消息19")
    # 首尾句提取生效（短消息原样，超长的被提取）
    assert all(len(t) <= 20 for _, t in all_user)


def test_config_load_defaults_and_override(tmp_path):
    cfg = load_config(path=tmp_path / "missing.yaml")
    assert cfg["enabled"] is True
    assert cfg["every_n_turns"] == 2
    assert cfg["strategy"] == "conservative"
    # 另留少量中文意图空间；12 字符仍只是软目标。
    if cfg["max_title_length"] is not None:
        p = tmp_path / "config.yaml"
        p.write_text("strategy: aggressive\nmax_title_length: 24\n", encoding="utf-8")
        cfg = load_config(p)
        assert cfg["max_title_length"] == 24
    else:
        assert cfg["max_title_length"] is None

    p = tmp_path / "config.yaml"
    p.write_text("strategy: aggressive\nmax_title_length: 30\n", encoding="utf-8")
    cfg2 = load_config(path=p)
    assert cfg2["strategy"] == "aggressive"
    assert cfg2["max_title_length"] == 30
    assert cfg2["every_n_turns"] == 2  # 未覆盖的键保持默认

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
    # 0.3 默认零入侵：首轮归宿主内建标题器
    assert cfg["first_title_mode"] == "builtin"
    assert cfg["retitle_summary_chars"] == 12000


def test_title_model_alias_feeds_internal_model_key(tmp_path):
    """宿主保留 model 这个 settings 根，对外只能声明 title_model。

    title_model 读入后必须归一化回内部 model 键，否则 Desktop 里配了
    模型覆盖却完全不生效。
    """
    p = tmp_path / "config.yaml"
    p.write_text("title_model: vendor/model-x\n", encoding="utf-8")
    cfg = load_config(path=p)
    assert cfg["title_model"] == "vendor/model-x"
    assert cfg["model"] == "vendor/model-x"


def test_internal_model_wins_over_title_model(tmp_path):
    """显式写内部 model 时不让 title_model 覆盖它。"""
    p = tmp_path / "config.yaml"
    p.write_text("model: internal/pick\ntitle_model: alias/pick\n", encoding="utf-8")
    cfg = load_config(path=p)
    assert cfg["model"] == "internal/pick"


def test_save_config_mirrors_title_model_into_model(tmp_path):
    """落盘只保留一份 model，不把别名写进文件造成双份真相。"""
    p = tmp_path / "config.yaml"
    cfg = {**DEFAULTS, "title_model": "vendor/model-x"}
    save_config(cfg, path=p)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert "title_model" not in data
    assert data["model"] == "vendor/model-x"


def test_config_example_matches_runtime_defaults(tmp_path, caplog):
    example = Path(__file__).resolve().parent.parent / "config.yaml.example"
    data = yaml.safe_load(example.read_text(encoding="utf-8"))
    assert set(data) <= set(DEFAULTS)
    assert data["every_n_turns"] == 2
    assert "renames_per_hour" not in data

    legacy = tmp_path / "config.yaml"
    legacy.write_text("renames_per_hour: 6\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        cfg = load_config(legacy)
    assert cfg["every_n_turns"] == 2
    assert "unknown key 'renames_per_hour' ignored" in caplog.text


# -- 0.3 非侵入契约：插件永不改写宿主配置 --------------------------------

def test_config_module_no_longer_writes_host_config():
    # 卸载无法回归默认标题命名的根因，就是这两个写宿主 config 的函数。
    # 它们必须不再存在，否则任何未来的调用点都会把副作用带回来。
    import hermes_auto_titler.config as config_mod

    assert not hasattr(config_mod, "disable_builtin_title_generation")
    assert not hasattr(config_mod, "enable_builtin_title_generation")


def test_register_never_touches_host_title_generation_config(monkeypatch):
    # 加载期（plugin 与 builtin 两种模式）都不得调用宿主 config 写入路径。
    import hermes_auto_titler as pkg

    host_writes = []

    def _host_save(*args, **kwargs):
        host_writes.append(args)
        raise AssertionError("plugin must not write host config")

    fake_host = type("HostConfig", (), {
        "load_config": staticmethod(lambda: {"auxiliary": {"title_generation": {"enabled": True}}}),
        "save_config": staticmethod(_host_save),
    })
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_host)

    hooks, cmds, lifecycle = [], [], []

    class FakeCtx:
        def register_hook(self, name, fn):
            hooks.append(name)

        def register_command(self, name, handler, **kw):
            cmds.append(name)

        def get_config(self, key, default=None):
            return default

    monkeypatch.setattr(pkg, "load_config", lambda **kw: {**DEFAULTS, "enabled": True})
    monkeypatch.setattr(pkg.AutoTitler, "restore_state", lambda self: lifecycle.append("restore"))
    monkeypatch.setattr(pkg.AutoTitler, "start_retry_loop", lambda self: lifecycle.append("start"))

    for mode in ("builtin", "plugin"):
        monkeypatch.setattr(
            pkg, "load_config",
            lambda mode=mode, **kw: {**DEFAULTS, "enabled": True, "first_title_mode": mode},
        )
        pkg.register(FakeCtx())

    assert host_writes == []
    assert lifecycle == ["restore", "start", "restore", "start"]


def test_first_title_mode_switch_reports_no_host_write(monkeypatch, tmp_path):
    # 运行期切换 first_title_mode 只落本地配置，不再顺带改宿主开关。
    from hermes_auto_titler.commands import make_handler

    p = tmp_path / "config.yaml"
    saved = []
    monkeypatch.setattr(
        "hermes_auto_titler.config.save_config", lambda cfg, path=None: saved.append(dict(cfg))
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.config", type("H", (), {
        "load_config": staticmethod(lambda: {}),
        "save_config": staticmethod(lambda cfg: (_ for _ in ()).throw(
            AssertionError("switch must not write host config"))),
    }))

    db = FakeDB([{"role": "user", "content": "帮我排查后台任务重复触发"}])
    t = AutoTitler(_NoLlmCtx(), {**DEFAULTS, "first_title_mode": "builtin"}, db=db)
    h = make_handler(t)
    out = h("config first_title_mode plugin")

    assert "Invalid" not in out
    assert t.cfg["first_title_mode"] == "plugin"
    assert saved and saved[-1]["first_title_mode"] == "plugin"
    assert "never" in out  # 明确告知用户宿主配置未被改动


def test_config_coerces_bool_and_int_strings(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "enabled: \"true\"\nevery_n_turns: \"2\"\nignore_model_messages: \"false\"\non_close: \"on\"\n",
        encoding="utf-8",
    )
    cfg = load_config(path=p)
    assert cfg["enabled"] is True
    assert cfg["every_n_turns"] == 2
    assert cfg["ignore_model_messages"] is False
    assert cfg["on_close"] is True


def test_config_invalid_values_fall_back_to_defaults(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "every_n_turns: abc\nenabled: maybe\nstrategy: bogus\ntitle_style: weird\n"
        "opening_turns: 0\nrecent_turns: 0\nmin_interval_minutes: -1\n",
        encoding="utf-8",
    )
    cfg = load_config(path=p)
    assert cfg["every_n_turns"] == 2  # 非法整数回退默认，不崩溃
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
    assert cfg["every_n_turns"] == 2


# -- 滞后机制配置：rename_confirmations ------------------------------------------

def test_rename_confirmations_defaults_and_clamps(tmp_path):
    p = tmp_path / "config.yaml"

    # 0.2 缺省：1（候选再获得 1 次后续背书才写入）
    p.write_text("enabled: true\n", encoding="utf-8")
    cfg = load_config(path=p)
    assert cfg["rename_confirmations"] == 1

    # 合法值 0/1/2/3
    p.write_text("rename_confirmations: 0\n", encoding="utf-8")
    assert load_config(path=p)["rename_confirmations"] == 0
    p.write_text("rename_confirmations: 1\n", encoding="utf-8")
    assert load_config(path=p)["rename_confirmations"] == 1
    p.write_text("rename_confirmations: 2\n", encoding="utf-8")
    assert load_config(path=p)["rename_confirmations"] == 2
    p.write_text("rename_confirmations: 3\n", encoding="utf-8")
    assert load_config(path=p)["rename_confirmations"] == 3

    # 下限钳到 0，非负整数原样保留（不设多余的人为上限）
    p.write_text("rename_confirmations: -2\n", encoding="utf-8")
    assert load_config(path=p)["rename_confirmations"] == 0
    p.write_text("rename_confirmations: 99\n", encoding="utf-8")
    assert load_config(path=p)["rename_confirmations"] == 99

    # 非法浮点回退默认
    p.write_text("rename_confirmations: 1.5\n", encoding="utf-8")
    cfg = load_config(path=p)
    assert cfg["rename_confirmations"] == DEFAULTS["rename_confirmations"]


def test_max_renames_per_session_defaults_off_and_clamps(tmp_path):
    p = tmp_path / "config.yaml"

    cfg = load_config(path=tmp_path / "missing.yaml")
    assert cfg["max_renames_per_session"] == 0

    p.write_text("max_renames_per_session: 3\n", encoding="utf-8")
    assert load_config(path=p)["max_renames_per_session"] == 3

    p.write_text("max_renames_per_session: -2\n", encoding="utf-8")
    assert load_config(path=p)["max_renames_per_session"] == 0

    p.write_text("max_renames_per_session: 1.5\n", encoding="utf-8")
    assert load_config(path=p)["max_renames_per_session"] == DEFAULTS["max_renames_per_session"]


# -- 配置加固：数值布尔 / NaN / inf / 非字符串 model / 负长度 -------------------

def test_config_rejects_numeric_bool_outside_01(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("enabled: 2\non_close: -1\nignore_model_messages: 0.5\n", encoding="utf-8")
    cfg = load_config(path=p)
    assert cfg["enabled"] is True  # 回退默认，不静默按 truthy/falsy 解释
    assert cfg["on_close"] is True
    assert cfg["ignore_model_messages"] is False


def test_config_accepts_numeric_bool_01(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("enabled: 0\non_close: 1\nignore_model_messages: 0\n", encoding="utf-8")
    cfg = load_config(path=p)
    assert cfg["enabled"] is False
    assert cfg["on_close"] is True
    assert cfg["ignore_model_messages"] is False


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
        assert "Invalid value" in h(f"config min_interval_minutes {bad}")
        assert t.cfg["min_interval_minutes"] == 5.0
    # 数值布尔只接受 0/1
    for bad in ("2", "-1", "0.5"):
        assert "Invalid value" in h(f"config enabled {bad}")
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


def test_extract_compaction_summary_merged_carrier_and_clean_text():
    from hermes_auto_titler.messages import _extract_compaction_summary, clean_captured_text

    merged_carrier = (
        "[PRIOR CONTEXT — for reference only; not a new message]\n"
        "Earlier turns were compacted into the summary below.\n"
        "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]\n"
        "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted:\n"
        "avoid repeating it:\n"
        "## Historical Task Snapshot\n"
        "User asked: 'v0.2.0 发版与文档整理'\n"
        "Historical only; newer protected-tail messages after this summary win.\n"
        "--- END OF CONTEXT SUMMARY ---\n"
        "[STILL IN PROGRESS — continue current task]\n"
        "真实的最后一条用户请求"
    )

    # 1. 摘要提取：即使带有 [PRIOR CONTEXT]，依然能定位并提取正文，且清洗掉行为指令
    summary = _extract_compaction_summary(merged_carrier)
    assert summary is not None
    assert "## Historical Task Snapshot" in summary
    assert "v0.2.0 发版与文档整理" in summary
    assert "Historical only" not in summary

    # 2. 消息清洗：正确截取 --- END OF CONTEXT SUMMARY --- 之后的真实用户指令，剥除 [STILL IN PROGRESS]
    user_turn = clean_captured_text(merged_carrier)
    assert user_turn == "真实的最后一条用户请求"


def test_clean_assistant_dialog_strips_xml_control_tags_and_code():
    from hermes_auto_titler.messages import clean_assistant_dialog

    raw_assistant = (
        "<command-message>git status</command-message>\n"
        "<local-command-stdout>On branch main</local-command-stdout>\n"
        "<system-reminder>Please run tests</system-reminder>\n"
        "```python\nprint('hello')\n```\n"
        "[tool: terminal] Ran pytest.\n"
        "已成功完成插件发布与验证。"
    )
    cleaned = clean_assistant_dialog(raw_assistant)
    assert "<command-message>" not in cleaned
    assert "git status" not in cleaned
    assert "<local-command-stdout>" not in cleaned
    assert "<system-reminder>" not in cleaned
    assert "[tool: terminal]" not in cleaned
    assert "print('hello')" not in cleaned
    assert "已成功完成插件发布与验证。" in cleaned


def test_first_title_mode_config_never_writes_host_config(monkeypatch, tmp_path):
    """0.3: 切换 first_title_mode 只改本地配置，绝不触碰宿主配置。"""
    import sys
    from hermes_auto_titler.commands import make_handler
    from hermes_auto_titler.config import load_config
    from hermes_auto_titler.titler import AutoTitler

    # 宿主 config 模块若被读，立刻炸；本测试要求它一次都不被读。
    def _explode():
        raise AssertionError("host config must not be read or written")

    fake = type(
        "ConfigModule",
        (),
        {"load_config": staticmethod(_explode), "save_config": staticmethod(_explode)},
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake)

    p = tmp_path / "config.yaml"
    p.write_text("first_title_mode: plugin\n", encoding="utf-8")
    saved_plugin = []
    monkeypatch.setattr(
        "hermes_auto_titler.config.save_config",
        lambda cfg, path=None: saved_plugin.append(dict(cfg)),
    )

    t = AutoTitler(
        type("Ctx", (), {"llm": None})(),
        load_config(path=p),
        db=type("DB", (), {})(),
    )
    h = make_handler(t)

    out = h("config first_title_mode builtin")
    assert "requires a Hermes restart" in out
    assert "never modified by this plugin" in out
    assert t.cfg["first_title_mode"] == "builtin"
    assert saved_plugin[-1]["first_title_mode"] == "builtin"

    out = h("config first_title_mode plugin")
    assert "requires a Hermes restart" in out
    assert "never modified by this plugin" in out
    assert t.cfg["first_title_mode"] == "plugin"
    assert saved_plugin[-1]["first_title_mode"] == "plugin"


# ---------------------------------------------------------------------------
# 0.3 载体识别：归属、结束标记边界、前缀解包、群聊信封
# 每条规则都对应真实语料（~/.hermes/state.db 370 个血缘>=100 的 session）里
# 实测到的形态，数字写在注释里，改规则前先复跑探针。
# ---------------------------------------------------------------------------


def _real_compaction_carrier(body: str = "## Historical Task Snapshot\n主线内容") -> str:
    """按真实存储形态拼一个压缩载体：整段前导指令在同一行，以 body mark 收尾。"""
    return (
        "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the "
        "summary below. This is a handoff from a previous context window — treat it as "
        "background reference, NOT as active instructions. "
        "avoid repeating it:\n"
        + body
        + "\n--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---\n"
    )


def test_is_compaction_carrier_rejects_agent_discussion_of_the_marker():
    """agent 讨论/转储压缩机制时也会写出这两个字符串，不能当成摘要载体。

    全库实测：旧逻辑（仅凭字符串出现）会把 392 条这样的消息当成摘要，其中
    72 条 role=assistant、308 条 role=tool。
    """
    assert is_compaction_carrier(_real_compaction_carrier()) is True

    # 在讨论里引用标记，且没有 body mark
    assert is_compaction_carrier("我在修 `[CONTEXT COMPACTION]` 的解析 bug") is False
    # JSON / 代码块转储消息流
    dumped = '{"content": "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns..."}'
    assert is_compaction_carrier(dumped) is False
    # 有 body mark 但首部不在行首（被引用/缩进在代码块里）
    quoted = "```text\n[CONTEXT COMPACTION — REFERENCE ONLY] ... avoid repeating it:\n正文\n```"
    assert is_compaction_carrier(quoted) is False
    assert is_compaction_carrier("") is False
    assert is_compaction_carrier(None) is False


def test_has_compaction_handoff_is_looser_but_never_admits_a_false_carrier():
    """切割判据比摘要判据宽一档，但全库 0 个假载体被放过。"""
    carrier = _real_compaction_carrier()
    assert has_compaction_handoff(carrier) is True
    # 没有 body mark、但有独占整行的结束标记 —— 仍应按 handoff 切一刀
    assert has_compaction_handoff("[CONTEXT COMPACTION — x]\n正文\n--- END OF CONTEXT SUMMARY ---\n") is True
    # agent 讨论：既无 body mark 也无独占行结束标记
    assert has_compaction_handoff("讨论 `[CONTEXT COMPACTION]` 的实现") is False


def test_extract_compaction_summary_ignores_agent_discussion():
    body = _real_compaction_carrier()
    assert _extract_compaction_summary(body) is not None
    assert "主线内容" in _extract_compaction_summary(body)

    # agent 长篇讨论压缩机制：不提摘要
    discussion = (
        "## 调查结果\n\n真正的根因在 `clean_captured_text`：\n"
        "它遇到 `[CONTEXT COMPACTION]` 就把整条丢掉。\n"
        "Hermes 的压缩会生成一个包含 `--- END OF CONTEXT SUMMARY ---` 的块。\n"
    )
    assert _extract_compaction_summary(discussion) is None


def test_end_marker_boundary_prefers_col0_standalone_line():
    """正文小节里反引号引用的示例文本会提前命中 end marker，必须按行判别。

    真实案例 20260920_194554_afa67c：13323 字符载体，`## Key Decisions` 与
    `## Errors & Fixes` 小节内各有一次反引号引用命中。取首次命中只得 4519
    字符 / 7 小节；取独占整行的真边界得 11285 字符 / 13 小节，且被砍掉的
    后半段不会再被当成「真实用户消息」回灌。
    """
    carrier = (
        "[CONTEXT COMPACTION — REFERENCE ONLY] ... avoid repeating it:\n"
        "## Anchor Index\n锚点\n\n"
        "## Key Decisions\n判别式是「标记独占整行」：`--- end of context summary ---` 这种行内引用不算。\n\n"
        "## Errors & Fixes\n回归见 `--- end of context summary ---` 的误命中。\n\n"
        "## User Messages (verbatim, newest first)\n真实用户原话\n\n"
        "--- END OF CONTEXT SUMMARY — respond to the message below ---\n"
    )
    summary = _extract_compaction_summary(carrier)
    assert summary is not None
    # 反引号引用里的小节正文必须完整保留
    assert "## Key Decisions" in summary
    assert "## Errors & Fixups" not in summary
    assert "## User Messages (verbatim, newest first)" in summary
    assert "真实用户原话" in summary
    # 结束标记本身不进入摘要
    assert "END OF CONTEXT SUMMARY" not in summary


def test_end_marker_boundary_falls_back_when_no_col0_line():
    """找不到独占整行的标记时必须回退到首次命中，不能把摘要整个丢掉。

    真实案例 20260922_142944_ff6e95：唯一命中就是真边界，但 `---\\s*` 吞掉了
    紧随的 `\n\n[STILL IN PROGRESS …]`，按「同行不能有后续内容」一刀切会误伤。
    """
    carrier = (
        "[CONTEXT COMPACTION — REFERENCE ONLY] ... avoid repeating it:\n"
        "## Historical Task Snapshot\n主线\n"
        "--- END OF CONTEXT SUMMARY --- [STILL IN PROGRESS — continue the task]\n"
        "当前真实请求\n"
    )
    summary = _extract_compaction_summary(carrier)
    assert summary is not None
    assert "主线" in summary
    # 回退路径同样要把结束标记之后的真实用户消息交回 clean_captured_text
    assert clean_captured_text(carrier) == "当前真实请求"


def test_clean_captured_text_drops_agent_discussion_entirely():
    """agent 讨论压缩机制的长篇正文不是用户消息，必须整体丢弃而不是切一刀。"""
    discussion = (
        "## 调查结果\n\n真正的根因在 `clean_captured_text`：\n"
        "它遇到 `[CONTEXT COMPACTION]` 就把整条丢掉。\n"
        "Hermes 的压缩会生成一个包含 `--- END OF CONTEXT SUMMARY ---` 的块。\n"
    )
    assert clean_captured_text(discussion) is None


def test_unpack_system_wrapper_keeps_the_real_request():
    """包装之后紧跟的正文就是用户原话，旧逻辑把整条连同请求一起丢掉。

    全库实测：`model has changed` 121 条中 20 条带正文且全是人类原话，
    `interrupted mid-run` 34 条全部带正文（其中 4 条 >40 字符是人类原话）。
    """
    assert unpack_system_wrapper(
        "[System: The active model for this chat has changed to xai/grok-4.7 via "
        "provider opencodex. From this point forward, use this runtime metadata.]\n\n"
        "我想让你每天自己运营，然后账号知名度做大后接单之类"
    ) == "我想让你每天自己运营，然后账号知名度做大后接单之类"

    assert unpack_system_wrapper(
        "[System note: Your previous turn was interrupted mid-run — the app or its "
        "backend process stopped before the turn could finish.]\n\n"
        "给aside装上浏览器拓展，现在没有，然后给它换个dia的图标"
    ) == "给aside装上浏览器拓展，现在没有，然后给它换个dia的图标"

    assert unpack_system_wrapper(
        "[System: The previous response was cut off by a network error mid-stream. "
        "Continue exactly where you left off.]"
    ) is None

    assert unpack_system_wrapper("真实用户消息") is None
    assert unpack_system_wrapper("") is None


def test_load_context_unwraps_system_wrappers_into_user_trajectory():
    conv = [
        {"role": "user", "content": "[System: The active model for this chat has changed to gpt-5.]"},
        {"role": "assistant", "content": "好"},
        {"role": "user", "content": "[System: The active model for this chat has changed to gpt-5.]\n\n我想让你每天自己运营"},
        {"role": "assistant", "content": "了解"},
    ]
    _, all_user, opening, summary = load_context_with_summary(
        FakeDB(conv), "s1", recent_turns=4, include_all_user=True, opening_turns=2
    )
    # 空正文的包装退化成丢弃；带正文的包装解包后进入用户意图轨迹
    assert all_user == [("user", "我想让你每天自己运营")]
    assert all("[System" not in t for _, t in opening)
    assert summary is None


def test_strip_group_chat_envelope_keeps_only_the_messages_block():
    """群聊信封的规则块对标题没有主题信息，却以「用户意图」形态进 prompt。

    全库实测 116 封 / 315,175 字符：规则块 32.4% + 首部 6.9% = 39.5% 是样板。
    """
    envelope = (
        '[Group chat: "Lattice"] You are @eclipse, one participant in a group chat '
        "with @lynn, @aperture and the user.\n\n"
        "New messages in the room since your last turn (oldest first):\n"
        "  You (user): @lynn 我们需要推动AutoTitler到0.3\n"
        "  Lynn: 我刚核查了上游源码\n\n"
        "Rules for this room:\n"
        "- Reply with ONE conversational message ONLY if you have something new.\n"
        "- If you have nothing new to add, reply with exactly \"(pass)\".\n"
    )
    stripped = strip_group_chat_envelope(envelope)
    assert stripped is not None
    assert "我们需要推动AutoTitler到0.3" in stripped
    assert "我刚核查了上游源码" in stripped
    assert "Rules for this room" not in stripped
    assert 'Group chat: "Lattice"' not in stripped
    assert "New messages in the room" not in stripped

    assert strip_group_chat_envelope("普通消息") is None
    assert strip_group_chat_envelope("") is None


def test_load_context_strips_group_chat_envelope_from_user_turns():
    conv = [
        {
            "role": "user",
            "content": (
                '[Group chat: "Lattice"] You are @eclipse.\n\n'
                "New messages in the room since your last turn (oldest first):\n"
                "  You (user): 帮我把标题插件推进到 0.3\n\n"
                "Rules for this room:\n"
                "- Reply with ONE conversational message ONLY.\n"
            ),
        },
        {"role": "assistant", "content": "好的"},
    ]
    _, all_user, opening, _ = load_context_with_summary(
        FakeDB(conv), "s1", recent_turns=2, include_all_user=True, opening_turns=2
    )
    assert all_user == [("user", "You (user): 帮我把标题插件推进到 0.3")]
    assert all("Rules for this room" not in t for _, t in opening)


def test_load_context_merges_multi_compaction_summaries():
    """多轮压缩会产生多段载体，按时间顺序拼接而不是只取 summaries[0]。"""
    conv = [
        {"role": "user", "content": _real_compaction_carrier("## Historical Task Snapshot\n第一段主线目标")},
        {"role": "user", "content": "继续"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": _real_compaction_carrier("## Historical Task Snapshot\n第二段最新状态")},
        {"role": "user", "content": "再继续"},
    ]
    _, _, _, summary = load_context_with_summary(
        FakeDB(conv), "s1", recent_turns=2, include_all_user=True, opening_turns=2
    )
    assert summary is not None
    assert "第一段主线目标" in summary
    assert "第二段最新状态" in summary


def test_summary_survives_a_visible_opening_instead_of_being_gated_away():
    """一句「继续」不该把历史摘要整体清空。

    旧的放行条件是 `not saw_visible_opening`，实测 225 个含载体会话里 141 个
    因此完全看不到摘要。
    """
    conv = [
        {"role": "user", "content": _real_compaction_carrier("## Historical Task Snapshot\n原始目标")},
        {"role": "user", "content": "继续"},
        {"role": "assistant", "content": "a1"},
    ]
    _, _, _, summary = load_context_with_summary(
        FakeDB(conv), "s1", recent_turns=2, include_all_user=True, opening_turns=2
    )
    assert summary is not None
    assert "原始目标" in summary


def test_status_diagnoses_first_title_host_switch(monkeypatch):
    import sys
    from hermes_auto_titler.commands import make_handler
    from hermes_auto_titler.titler import AutoTitler

    # 1. first_title_mode=builtin 且宿主 auxiliary.title_generation.enabled=false
    fake_disabled = type("ConfigModule", (), {
        "load_config_readonly": staticmethod(lambda: {"auxiliary": {"title_generation": {"enabled": False}}})
    })
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_disabled)

    t = AutoTitler(type("Ctx", (), {"llm": None})(), {**DEFAULTS, "first_title_mode": "builtin"}, db=type("DB", (), {})())
    h = make_handler(t)
    out = h("status")
    assert "first-title mismatch" in out
    assert "hermes config set auxiliary.title_generation.enabled true" in out
    assert "first_title_mode plugin" in out

    # 2. first_title_mode=builtin 且宿主配置未知 (load_config_readonly 返回 None 或抛异常)
    fake_unknown = type("ConfigModule", (), {
        "load_config_readonly": staticmethod(lambda: None)
    })
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_unknown)
    out_unknown = h("status")
    assert "first-title mismatch" not in out_unknown
    assert "unknown" in out_unknown

    # 3. first_title_mode=plugin 时提示首轮由插件负责，但不改宿主开关
    t_plugin = AutoTitler(type("Ctx", (), {"llm": None})(), {**DEFAULTS, "first_title_mode": "plugin"}, db=type("DB", (), {})())
    h_plugin = make_handler(t_plugin)
    out_plugin = h_plugin("status")
    assert "plugin" in out_plugin
    assert "first-title mismatch" not in out_plugin


class PhysicalSqliteSessionDB:
    """真实物理 SQLite DB 形态，精确复现 hermes_state_messages.py 的表结构与查询语义。"""

    def __init__(self):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                parent_session_id TEXT,
                title TEXT,
                title_source TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                compacted INTEGER NOT NULL DEFAULT 0,
                timestamp REAL DEFAULT 0.0,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT,
                display_metadata TEXT,
                _compressed_summary INTEGER DEFAULT 0
            );
        """)

    def insert_session(self, sid: str, parent_id: str = None):
        self.conn.execute("INSERT INTO sessions (id, parent_session_id) VALUES (?, ?)", (sid, parent_id))

    def insert_message(self, session_id: str, role: str, content: str, active: int = 1, compacted: int = 0, timestamp: float = 0.0):
        self.conn.execute(
            "INSERT INTO messages (session_id, role, content, active, compacted, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, role, content, active, compacted, timestamp)
        )

    def _resolve_lineage(self, session_id: str) -> List[str]:
        lineage = []
        cur = session_id
        while cur:
            lineage.append(cur)
            row = self.conn.execute("SELECT parent_session_id FROM sessions WHERE id = ?", (cur,)).fetchone()
            cur = row["parent_session_id"] if row else None
        return list(reversed(lineage))

    def get_messages_as_conversation(
        self,
        session_id: str,
        include_ancestors: bool = False,
        include_inactive: bool = False,
        repair_alternation: bool = False,
        include_row_ids: bool = False,
        include_compacted: bool = False,
    ) -> List[dict]:
        """与宿主 hermes_state_messages.py:1157-1160 _active_clause 严格一致。"""
        if include_inactive:
            active_clause = ""
        elif include_compacted:
            active_clause = " AND (active = 1 OR compacted = 1)"
        else:
            active_clause = " AND active = 1"

        sids = self._resolve_lineage(session_id) if include_ancestors else [session_id]
        placeholders = ",".join("?" for _ in sids)
        rows = self.conn.execute(
            f"SELECT id, role, content, timestamp FROM messages WHERE session_id IN ({placeholders}){active_clause} ORDER BY id ASC",
            sids
        ).fetchall()
        return [{"role": r["role"], "content": r["content"], "timestamp": r["timestamp"]} for r in rows]


def test_compacted_session_recovers_original_human_opening_from_physical_db():
    """测试物理 DB 形态下，原地压缩会话能正确恢复压缩前首轮人类意图，且不引入撤回消息。"""
    db = PhysicalSqliteSessionDB()
    db.insert_session("sess_1")
    # 压缩前的历史轮次 (compacted=1, active=0)
    db.insert_message("sess_1", "user", "请帮我实现一个基于 Rust 的高并发任务调度器", active=0, compacted=1)
    db.insert_message("sess_1", "assistant", "好的，我们用 tokio 来实现...", active=0, compacted=1)
    # 撤回消息 (compacted=0, active=0) - 宿主 rewind 产生，严禁作为人类意图恢复
    db.insert_message("sess_1", "user", "撤回的错误输入：先别写调度器了", active=0, compacted=0)
    # 压缩载体 (active=1, compacted=0)
    carrier = _real_compaction_carrier("## Historical Task Snapshot\n主线是构建 Rust 任务调度器")
    db.insert_message("sess_1", "user", carrier, active=1, compacted=0)
    # 压缩后的后续活跃轮次 (active=1, compacted=0)
    db.insert_message("sess_1", "user", "增加支持优先级的 worker 队列", active=1, compacted=0)
    db.insert_message("sess_1", "assistant", "已添加基于 BinaryHeap 的优先级队列实现", active=1, compacted=0)
    db.insert_message("sess_1", "user", "运行单元测试", active=1, compacted=0)
    db.insert_message("sess_1", "assistant", "所有 15 个测试通过", active=1, compacted=0)

    recent, all_user, opening, summary = load_context_with_summary(
        db, "sess_1", recent_turns=2, include_all_user=True, opening_turns=2
    )

    # 1. 原始人类首轮意图成功恢复
    assert opening[0] == ("user", "请帮我实现一个基于 Rust 的高并发任务调度器")
    # 2. 撤回消息（active=0, compacted=0）严禁恢复为人类意图
    assert not any("撤回" in text for _, text in opening)
    assert not any("撤回" in text for _, text in all_user)
    assert not any("撤回" in text for _, text in recent)
    # 3. 压缩摘要作为独立锚点保留
    assert summary is not None and "主线是构建 Rust 任务调度器" in summary
    # 4. 最近轮次正确对应活跃尾部
    assert recent[-1] == ("assistant", "所有 15 个测试通过")


def test_compacted_session_lineage_recovers_original_human_opening_from_physical_db():
    """测试物理 DB 形态下，祖先血缘（parent-child 分叉）压缩会话正确恢复祖先根部的首轮人类意图。"""
    db = PhysicalSqliteSessionDB()
    db.insert_session("parent_sess")
    db.insert_session("child_sess", parent_id="parent_sess")
    # 父会话轮次（父会话被压缩后整体转为 compacted=1, active=0）
    db.insert_message("parent_sess", "user", "设计 AutoTitler 评估指标与回放流水线", active=0, compacted=1)
    db.insert_message("parent_sess", "assistant", "好的，设计如下流水线...", active=0, compacted=1)
    # 父会话中的撤回草稿 (active=0, compacted=0)
    db.insert_message("parent_sess", "user", "草稿：放弃 AutoTitler", active=0, compacted=0)
    # 子会话轮次 (active=1, compacted=0)
    carrier = _real_compaction_carrier("## Historical Task Snapshot\n设计 AutoTitler 评估指标")
    db.insert_message("child_sess", "user", carrier, active=1, compacted=0)
    db.insert_message("child_sess", "user", "运行回放指标测试", active=1, compacted=0)
    db.insert_message("child_sess", "assistant", "指标均已达标", active=1, compacted=0)

    recent, all_user, opening, summary = load_context_with_summary(
        db, "child_sess", recent_turns=1, include_all_user=True, opening_turns=1
    )

    # 1. 祖先原始人类首轮意图恢复
    assert opening[0] == ("user", "设计 AutoTitler 评估指标与回放流水线")
    # 2. 撤回草稿不出现
    assert not any("草稿" in text for _, text in all_user)
    # 3. 摘要保留
    assert summary is not None and "设计 AutoTitler 评估指标" in summary


def test_load_context_fallback_when_db_lacks_include_compacted():
    """测试当宿主/Mock DB 签名不支持 include_compacted 时，优雅降级而不是抛出 TypeError。"""
    class LegacyDuckDB:
        def __init__(self, conv):
            self.conv = conv

        def get_messages_as_conversation(self, session_id, include_ancestors=True):
            return self.conv

    legacy_db = LegacyDuckDB([
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！有什么我可以帮你的？"},
    ])
    recent, all_user, opening, summary = load_context_with_summary(
        legacy_db, "s_legacy", recent_turns=1, include_all_user=True
    )
    assert opening[0] == ("user", "你好")


def test_provenance_contrast_compacted_vs_inactive_vs_default():
    """对比证明：
    1. 默认 include_compacted=False 会使压缩前 active=0 消息丢失，首轮被后续轮次篡改。
    2. 旧审查误建议的 include_inactive=True 会把 active=0, compacted=0 的撤回消息复活为人意图。
    3. 唯有 include_compacted=True 既能找回压缩前首轮意图，又严格排除撤回消息。
    """
    db = PhysicalSqliteSessionDB()
    db.insert_session("sess_contrast")
    # 真实首轮（压缩后 active=0, compacted=1）
    db.insert_message("sess_contrast", "user", "真实意图：搭建流式处理服务", active=0, compacted=1)
    db.insert_message("sess_contrast", "assistant", "已确认目标", active=0, compacted=1)
    # 撤回消息（active=0, compacted=0）
    db.insert_message("sess_contrast", "user", "已撤回的错误指令", active=0, compacted=0)
    # 压缩载体与后续轮次（active=1, compacted=0）
    carrier = _real_compaction_carrier("## Historical Task Snapshot\n搭建流式处理服务")
    db.insert_message("sess_contrast", "user", carrier, active=1, compacted=0)
    db.insert_message("sess_contrast", "user", "后续调试", active=1, compacted=0)
    db.insert_message("sess_contrast", "assistant", "调试完毕", active=1, compacted=0)

    # 1. 默认模式（include_compacted=False, include_inactive=False）：首轮丢失
    raw_default = db.get_messages_as_conversation("sess_contrast", include_ancestors=True, include_compacted=False)
    default_user_msgs = [m["content"] for m in raw_default if m["role"] == "user"]
    assert "真实意图：搭建流式处理服务" not in default_user_msgs
    assert "已撤回的错误指令" not in default_user_msgs

    # 2. 错误建议模式（include_inactive=True）：撤回消息被错误复活
    raw_inactive = db.get_messages_as_conversation("sess_contrast", include_ancestors=True, include_inactive=True)
    inactive_user_msgs = [m["content"] for m in raw_inactive if m["role"] == "user"]
    assert "真实意图：搭建流式处理服务" in inactive_user_msgs
    assert "已撤回的错误指令" in inactive_user_msgs  # 缺陷：撤回消息被污染进意图！

    # 3. 正确模式（include_compacted=True, include_inactive=False）：精准恢复首轮，严密排除撤回
    raw_correct = db.get_messages_as_conversation("sess_contrast", include_ancestors=True, include_compacted=True)
    correct_user_msgs = [m["content"] for m in raw_correct if m["role"] == "user"]
    assert "真实意图：搭建流式处理服务" in correct_user_msgs
    assert "已撤回的错误指令" not in correct_user_msgs

