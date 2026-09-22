"""消息提取与配置加载测试。"""

import sys
from pathlib import Path
from typing import List

import yaml

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
    summary_preview,
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
    # 摘要永远不进入 opening；最早摘要单独作为弱提示
    # 可见轮次只有 2 轮、recent_turns=2 时 opening 落在 recent 内：
    # 降级保留首批轮次的用户消息，仍满足 opening[0] 这个 subject 线索锚点
    assert opening[0] == ("user", "m1")
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
    assert cfg["early_turn_eval"] is False
    assert cfg["first_title_mode"] == "plugin"
    assert cfg["retitle_summary_chars"] == 12000


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
        "enabled: \"true\"\nevery_n_turns: \"2\"\nearly_turn_eval: \"false\"\non_close: \"on\"\n",
        encoding="utf-8",
    )
    cfg = load_config(path=p)
    assert cfg["enabled"] is True
    assert cfg["every_n_turns"] == 2
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


def test_first_title_mode_config_requires_restart_and_manages_host(monkeypatch, tmp_path):
    """P2: /autotitler config first_title_mode must report restart-required and manage host config."""
    import sys
    from hermes_auto_titler.commands import make_handler
    from hermes_auto_titler.config import load_config
    from hermes_auto_titler.titler import AutoTitler

    host = {"auxiliary": {"title_generation": {"enabled": False}}}
    saved_host = []
    fake = type("ConfigModule", (), {
        "load_config": staticmethod(lambda: host),
        "save_config": staticmethod(lambda cfg: saved_host.append(dict(cfg))),
    })
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

    # Switch to builtin
    out = h("config first_title_mode builtin")
    assert "requires a Hermes restart" in out
    assert "restored host auxiliary.title_generation.enabled=true" in out
    assert host["auxiliary"]["title_generation"]["enabled"] is True
    assert t.cfg["first_title_mode"] == "builtin"

    # Switch to plugin
    out = h("config first_title_mode plugin")
    assert "requires a Hermes restart" in out
    assert "disabled host auxiliary.title_generation.enabled" in out
    assert host["auxiliary"]["title_generation"]["enabled"] is False
    assert t.cfg["first_title_mode"] == "plugin"

