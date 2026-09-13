"""Semantic acceptance matrix for the title prompt.

Runs curated synthetic conversations through the real configured title model.
It does not read or write SessionDB. The goal is to catch semantic regressions that
unit tests cannot: overfitting to the latest action, over-generalizing the topic,
missing real pivots, or treating an incidental tool as the subject.

Examples:
  <your-hermes-venv>/bin/python scripts/prompt_acceptance.py
  <your-hermes-venv>/bin/python scripts/prompt_acceptance.py --strategy aggressive
  <your-hermes-venv>/bin/python scripts/prompt_acceptance.py --both
  <your-hermes-venv>/bin/python scripts/prompt_acceptance.py --both --strict

PASS/WARN is intentionally lightweight. Read the emitted title and rationale for
borderline cases; by default WARN does not fail the process. Use --strict only when
you intentionally want the lexical checks to act as a gate.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_auto_titler.config import VALID_STRATEGIES, load_config
from hermes_auto_titler.titler import AutoTitler


class Ctx:
    def __init__(self):
        from agent.plugin_llm import PluginLlm

        self.llm = PluginLlm(plugin_id="hermes-auto-titler")


@dataclass(frozen=True)
class Case:
    name: str
    purpose: str
    current: str | None
    opening: list[tuple[str, str]]
    recent: list[tuple[str, str]]
    users: list[tuple[str, str]]
    blind: bool = False
    summary: str | None = None
    expected_actions: set[str] = field(default_factory=lambda: {"keep", "rename"})
    require_any: tuple[str, ...] = ()
    forbid: tuple[str, ...] = ()


PROJECT_OPENING = [
    ("user", "继续维护 hermes-auto-titler，重点是让长会话标题跟随真实主题。"),
    ("assistant", "先审查长期意图提取和 review state。"),
    ("user", "整个项目继续做，不要让具体工具或某次操作抢走标题。"),
]
PROJECT_RECENT = [
    ("user", "CI 还剩 Opening context 这个断言失败，修一下测试。"),
    ("assistant", "这是兼容标签断言，不影响核心主题。"),
]
PROJECT_USERS = [
    ("user", "继续维护 hermes-auto-titler，重点是让长会话标题跟随真实主题。"),
    ("user", "整个项目继续做，不要让具体工具或某次操作抢走标题。"),
    ("user", "CI 还剩 Opening context 这个断言失败，修一下测试。"),
]


CASES = [
    Case(
        name="project-over-latest-test",
        purpose="A failing test is one implementation step inside an ongoing project topic.",
        current="Hermes 自动标题维护",
        opening=PROJECT_OPENING,
        recent=PROJECT_RECENT,
        users=PROJECT_USERS,
        expected_actions={"keep"},
        forbid=("Opening context", "pytest", "断言"),
    ),
    Case(
        name="recover-from-too-narrow-title",
        purpose="An automatic title captured from one implementation step should be pulled back to the durable project topic.",
        current="Opening context 断言修复",
        opening=PROJECT_OPENING,
        recent=PROJECT_RECENT,
        users=PROJECT_USERS,
        expected_actions={"rename"},
        require_any=("Hermes", "hermes-auto-titler", "自动标题"),
        forbid=("Opening context", "pytest", "断言"),
    ),
    Case(
        name="durable-concrete-issue",
        purpose="A concrete issue should remain specific when that issue itself is the sustained goal.",
        current=None,
        blind=True,
        opening=[
            ("user", "Hindsight 的 PostgreSQL 开了 fsync=off，我要搞清楚这个设置的风险和性能影响。"),
            ("assistant", "可以从 WAL、断电风险和写入延迟解释。"),
        ],
        recent=[
            ("user", "继续围绕 fsync=off，看看容器重启和主机断电分别会怎样。"),
            ("assistant", "这仍然是同一个 fsync 配置问题。"),
            ("user", "最后给我判断 Hindsight 本地部署该不该开 fsync=off。"),
        ],
        users=[
            ("user", "Hindsight 的 PostgreSQL 开了 fsync=off，我要搞清楚这个设置的风险和性能影响。"),
            ("user", "继续围绕 fsync=off，看看容器重启和主机断电分别会怎样。"),
            ("user", "最后给我判断 Hindsight 本地部署该不该开 fsync=off。"),
        ],
        expected_actions={"rename"},
        require_any=("fsync",),
        forbid=("数据库问题", "软件配置"),
    ),
    Case(
        name="tool-is-the-subject",
        purpose="Do not strip a tool name when the tool itself is what the user is studying.",
        current=None,
        blind=True,
        opening=[
            ("user", "研究 Zen Browser 固定标签页的关闭语义，想让 FlowMouse 模拟它。"),
            ("assistant", "需要区分普通标签和 pinned tab。"),
        ],
        recent=[
            ("user", "重点还是 Zen Browser pinned tab：关闭后保留 pinned 状态但卸载页面。"),
            ("assistant", "这是浏览器行为本身，不只是实现工具。"),
        ],
        users=[
            ("user", "研究 Zen Browser 固定标签页的关闭语义，想让 FlowMouse 模拟它。"),
            ("user", "重点还是 Zen Browser pinned tab：关闭后保留 pinned 状态但卸载页面。"),
        ],
        expected_actions={"rename"},
        require_any=("Zen", "固定标签", "pinned"),
        forbid=("浏览器问题",),
    ),
    Case(
        name="one-off-interruption",
        purpose="A one-off explanatory question should not replace the active project topic.",
        current="Hindsight 记忆导入",
        opening=[
            ("user", "继续把 Hermes 历史 session 导入 Hindsight，先处理批量 retain。"),
            ("assistant", "可以按 source、raw fact、observation 分层。"),
            ("user", "这几天主线都还是把历史文档导完。"),
        ],
        recent=[
            ("user", "顺便问一句 PostgreSQL fsync=off 是什么意思？"),
            ("assistant", "它会减少同步落盘但增加崩溃时的数据风险。"),
            ("user", "明白，继续刚才 Hindsight 的批量导入。"),
        ],
        users=[
            ("user", "继续把 Hermes 历史 session 导入 Hindsight，先处理批量 retain。"),
            ("user", "这几天主线都还是把历史文档导完。"),
            ("user", "顺便问一句 PostgreSQL fsync=off 是什么意思？"),
            ("user", "明白，继续刚才 Hindsight 的批量导入。"),
        ],
        expected_actions={"keep"},
        forbid=("fsync",),
    ),
    Case(
        name="genuine-topic-pivot",
        purpose="An explicit abandonment plus sustained new work should replace the old topic.",
        current="Hermes 自动标题维护",
        opening=[
            ("user", "先继续 hermes-auto-titler 的 review 逻辑。"),
            ("assistant", "可以。"),
        ],
        recent=[
            ("user", "auto-titler 先到这里，不继续了。现在转去 FlowMouse 本地化。"),
            ("assistant", "开始检查 i18n 条目。"),
            ("user", "FlowMouse 里 Firefox/Zen 那批缺失翻译也一起补。"),
            ("assistant", "继续审查本地化范围。"),
            ("user", "这轮就围绕 FlowMouse 翻译 PR 做完。"),
        ],
        users=[
            ("user", "先继续 hermes-auto-titler 的 review 逻辑。"),
            ("user", "auto-titler 先到这里，不继续了。现在转去 FlowMouse 本地化。"),
            ("user", "FlowMouse 里 Firefox/Zen 那批缺失翻译也一起补。"),
            ("user", "这轮就围绕 FlowMouse 翻译 PR 做完。"),
        ],
        expected_actions={"rename"},
        require_any=("FlowMouse",),
        forbid=("Hermes", "auto-titler"),
    ),
    Case(
        name="literal-repo-identifier",
        purpose="Preserve an exact repository identifier while abstracting away the current edit.",
        current=None,
        blind=True,
        opening=[
            ("user", "继续开发 hermes-auto-titler，这次重新审查提示词判断层级。"),
            ("assistant", "可以把 topic abstraction 做成明确规则。"),
        ],
        recent=[
            ("user", "现在只是改 policy.py 的一句话，项目主线还是 hermes-auto-titler。"),
        ],
        users=[
            ("user", "继续开发 hermes-auto-titler，这次重新审查提示词判断层级。"),
            ("user", "现在只是改 policy.py 的一句话，项目主线还是 hermes-auto-titler。"),
        ],
        expected_actions={"rename"},
        require_any=("hermes-auto-titler",),
        forbid=("policy.py",),
    ),
    Case(
        name="compaction-anchor-over-tail",
        purpose="A compacted historical anchor should outrank a narrow recent implementation failure.",
        current=None,
        blind=True,
        summary="长期主线：设计 Prism LLM gateway，把 CLI、原生 API 和多模型路由统一到一个小型网关。已经讨论 combo routing、缓存、Hermes 登录复用和多端接入。",
        opening=[],
        recent=[
            ("user", "刚才 timeout case 失败了，先修 request retry。"),
            ("assistant", "可以调整 retry path。"),
        ],
        users=[
            ("user", "继续 Prism gateway，今天先修 timeout case。"),
            ("user", "retry 修完后继续 combo routing。"),
        ],
        expected_actions={"rename"},
        require_any=("Prism",),
        forbid=("timeout", "retry"),
    ),
    Case(
        name="specific-not-vague",
        purpose="Abstract above implementation details without collapsing into a generic category.",
        current=None,
        blind=True,
        opening=[
            ("user", "我们在设计一款基于 DAWproject 的协同 DAW，重点是多人协作和 AI 可操作参数。"),
            ("assistant", "需要从工程模型、插件参数暴露和协作状态开始。"),
        ],
        recent=[
            ("user", "今天先讨论 Bitwig 的工程结构作为参考，但产品仍然是自己的协同 DAW。"),
            ("assistant", "Bitwig 只是架构参考。"),
        ],
        users=[
            ("user", "我们在设计一款基于 DAWproject 的协同 DAW，重点是多人协作和 AI 可操作参数。"),
            ("user", "今天先讨论 Bitwig 的工程结构作为参考，但产品仍然是自己的协同 DAW。"),
        ],
        expected_actions={"rename"},
        require_any=("DAW", "DAWproject"),
        forbid=("软件开发", "AI 工具", "Bitwig 架构"),
    ),
]


def parse_args():
    parser = argparse.ArgumentParser(description="Run semantic title-prompt acceptance cases")
    parser.add_argument(
        "--strategy",
        choices=sorted(VALID_STRATEGIES),
        default="conservative",
        help="topic-shift strategy to test",
    )
    parser.add_argument("--both", action="store_true", help="run every case under both strategies")
    parser.add_argument("--case", action="append", dest="cases", help="run only named case(s)")
    parser.add_argument("--strict", action="store_true", help="exit non-zero when any lexical check warns")
    return parser.parse_args()


def contains_any(text: str, needles: Iterable[str]) -> bool:
    folded = text.casefold()
    return any(n.casefold() in folded for n in needles)


def run_case(titler: AutoTitler, case: Case):
    action, generated = titler._generate(
        case.current,
        case.recent,
        case.users,
        case.opening,
        blind=case.blind,
        earlier_summary=case.summary,
        session_id=None,
    )
    effective = generated if action == "rename" and generated else (case.current or "")

    problems = []
    if action not in case.expected_actions:
        problems.append(f"action={action!r}, expected {sorted(case.expected_actions)!r}")
    if case.require_any and not contains_any(effective, case.require_any):
        problems.append(f"missing one of {case.require_any!r}")
    bad = [word for word in case.forbid if word.casefold() in effective.casefold()]
    if bad:
        problems.append(f"contains transient/vague term(s) {bad!r}")

    verdict = "PASS" if not problems else "WARN"
    print(f"\n[{verdict}] {case.name}")
    print(f"  purpose : {case.purpose}")
    print(f"  current : {case.current or '(none)'}")
    print(f"  result  : {action}: {generated or '(none)'}")
    print(f"  effective: {effective or '(empty)'}")
    if problems:
        for problem in problems:
            print(f"  check   : {problem}")
    return not problems


def main():
    args = parse_args()
    selected = CASES
    if args.cases:
        wanted = set(args.cases)
        selected = [case for case in CASES if case.name in wanted]
        missing = wanted - {case.name for case in selected}
        if missing:
            raise SystemExit(f"unknown case(s): {', '.join(sorted(missing))}")

    strategies = ["conservative", "aggressive"] if args.both else [args.strategy]
    overall = True

    for strategy in strategies:
        cfg = load_config()
        cfg["strategy"] = strategy
        titler = AutoTitler(Ctx(), cfg, db=None)
        route = f"{cfg.get('provider') or '(host default)'}/{cfg.get('model') or '(host default)'}"
        print(f"\n=== strategy={strategy} route={route} cases={len(selected)} ===")
        for case in selected:
            overall = run_case(titler, case) and overall

    print("\nSemantic matrix complete. WARN means inspect the title; human review remains authoritative.")
    raise SystemExit(2 if args.strict and not overall else 0)


if __name__ == "__main__":
    main()
