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
    Case(
        name="lifecycle-design-to-release",
        purpose="An ongoing project topic must span design, implementation, bug-fixing, and release without shrinking into release details.",
        current="微信打卡小程序开发",
        opening=[
            ("user", "启动微信打卡小程序的架构设计与开发，支持每日定位签到和打卡记录统计。"),
            ("assistant", "我们先设计打卡数据结构和地理围栏判定逻辑。"),
        ],
        recent=[
            ("user", "微信小程序审核已经过了，打卡功能上线，准备写 1.0.0 发布说明。"),
            ("assistant", "可以梳理核心功能点和发布记录。"),
        ],
        users=[
            ("user", "启动微信打卡小程序的架构设计与开发，支持每日定位签到和打卡记录统计。"),
            ("user", "打卡页面和定位组件写完了，接下来对接云函数。"),
            ("user", "测试发现跨时区打卡时间戳有偏移，正在修这个 bug。"),
            ("user", "微信小程序审核已经过了，打卡功能上线，准备写 1.0.0 发布说明。"),
        ],
        expected_actions={"keep"},
        forbid=("发布说明", "审核", "时区", "release"),
    ),
    Case(
        name="bug-pullback-pair",
        purpose="When current title was captured from a past transient bug, pull it back to the project topic.",
        current="修复打卡时区偏移 Bug",
        opening=[
            ("user", "启动微信打卡小程序的架构设计与开发，支持每日定位签到和打卡记录统计。"),
            ("assistant", "我们先设计打卡数据结构和地理围栏判定逻辑。"),
        ],
        recent=[
            ("user", "微信小程序审核已经过了，打卡功能上线，准备写 1.0.0 发布说明。"),
            ("assistant", "可以梳理核心功能点和发布记录。"),
        ],
        users=[
            ("user", "启动微信打卡小程序的架构设计与开发，支持每日定位签到和打卡记录统计。"),
            ("user", "打卡页面和定位组件写完了，接下来对接云函数。"),
            ("user", "测试发现跨时区打卡时间戳有偏移，正在修这个 bug。"),
            ("user", "微信小程序审核已经过了，打卡功能上线，准备写 1.0.0 发布说明。"),
        ],
        expected_actions={"rename"},
        require_any=("打卡", "小程序"),
        forbid=("时区", "偏移", "Bug", "发布说明"),
    ),
    Case(
        name="tool-as-means",
        purpose="A tool used merely as a diagnostic means should not displace the actual problem subject.",
        current="商城支付回调超时排查",
        opening=[
            ("user", "排查商城系统在高峰期的微信支付回调超时问题，订单状态无法及时更新。"),
            ("assistant", "先检查回调接口耗时、数据库事务锁和网络延时。"),
        ],
        recent=[
            ("user", "用 tcpdump 和 Wireshark 在网关抓了 8080 端口的回调包，分析是否有丢包或重传。"),
            ("assistant", "抓包显示服务端在收到回调时发生了 TCP 乱序重传。"),
        ],
        users=[
            ("user", "排查商城系统在高峰期的微信支付回调超时问题，订单状态无法及时更新。"),
            ("user", "用 tcpdump 和 Wireshark 在网关抓了 8080 端口的回调包，分析是否有丢包或重传。"),
        ],
        expected_actions={"keep"},
        forbid=("Wireshark", "tcpdump", "抓包"),
    ),
    Case(
        name="multi-interruption-return",
        purpose="Multiple consecutive quick side questions followed by returning to the main topic must keep the main title.",
        current="用户鉴权系统重构",
        opening=[
            ("user", "我们开始重构用户鉴权系统，将 Session 迁移至 JWT + Redis 黑名单架构。"),
            ("assistant", "好的，我们规划 Token 生成、刷新和双端校验逻辑。"),
        ],
        recent=[
            ("user", "顺便问下 Docker buildx 怎么传多架构参数？"),
            ("assistant", "可以使用 --platform linux/amd64,linux/arm64。"),
            ("user", "那 nerdctl 怎么看镜像架构？"),
            ("assistant", "可以使用 nerdctl image inspect。"),
            ("user", "收到，回到鉴权系统重构，继续写 Redis 踢人与黑名单机制。"),
        ],
        users=[
            ("user", "我们开始重构用户鉴权系统，将 Session 迁移至 JWT + Redis 黑名单架构。"),
            ("user", "顺便问下 Docker buildx 怎么传多架构参数？"),
            ("user", "那 nerdctl 怎么看镜像架构？"),
            ("user", "收到，回到鉴权系统重构，继续写 Redis 踢人与黑名单机制。"),
        ],
        expected_actions={"keep"},
        forbid=("Docker", "buildx", "nerdctl", "镜像"),
    ),
    Case(
        name="genuine-multi-turn-pivot",
        purpose="Explicit abandonment followed by sustained multi-turn work on a new topic must rename to the new topic.",
        current="用户鉴权系统重构",
        opening=[
            ("user", "我们开始重构用户鉴权系统，将 Session 迁移至 JWT + Redis 黑名单架构。"),
            ("assistant", "好的，规划鉴权迁移步骤。"),
        ],
        recent=[
            ("user", "鉴权系统先完全不做了，搁置。现在全力做 Docker 容器镜像的多架构自动构建。"),
            ("assistant", "转向容器镜像多架构构建配置。"),
            ("user", "把 buildx 缓存和 GitLab CI 多架构 push 全配好。"),
            ("assistant", "已配置 GitLab CI 多平台流水线。"),
            ("user", "测试一下 arm64 和 amd64 镜像构建，验证自动推送。"),
        ],
        users=[
            ("user", "我们开始重构用户鉴权系统，将 Session 迁移至 JWT + Redis 黑名单架构。"),
            ("user", "鉴权系统先完全不做了，搁置。现在全力做 Docker 容器镜像的多架构自动构建。"),
            ("user", "把 buildx 缓存和 GitLab CI 多架构 push 全配好。"),
            ("user", "测试一下 arm64 和 amd64 镜像构建，验证自动推送。"),
        ],
        expected_actions={"rename"},
        require_any=("Docker", "镜像", "多架构", "构建"),
        forbid=("鉴权", "JWT", "Session"),
    ),
    Case(
        name="concrete-issue-not-generalized",
        purpose="A concrete troubleshooting issue should remain specific and not collapse into generic system/config titles.",
        current=None,
        blind=True,
        opening=[
            ("user", "排查 M1 Mac 外接 4K 显示器开启 DDC/CI 后的持续闪屏黑屏问题。"),
            ("assistant", "这可能是 macOS 显示器驱动与显示器固件的 DDC 通信冲突。"),
        ],
        recent=[
            ("user", "测试了关闭 BetterDisplay 的 DDC 写入后完全不黑屏了，确诊是固件 DDC/CI 冲突。"),
            ("assistant", "可以保留软件调光，禁用硬件 DDC 写入来彻底规避。"),
        ],
        users=[
            ("user", "排查 M1 Mac 外接 4K 显示器开启 DDC/CI 后的持续闪屏黑屏问题。"),
            ("user", "测试了关闭 BetterDisplay 的 DDC 写入后完全不黑屏了，确诊是固件 DDC/CI 冲突。"),
        ],
        expected_actions={"rename"},
        require_any=("DDC", "闪屏", "黑屏", "显示器"),
        forbid=("硬件问题", "macOS 问题", "配置问题", "系统问题"),
    ),
    Case(
        name="mixed-language-repo-command",
        purpose="Preserve exact repository identifier in bilingual context without adopting raw command or filename.",
        current=None,
        blind=True,
        opening=[
            ("user", "给 faster-whisper 项目添加流式音频分块切片处理功能。"),
            ("assistant", "可以在 audio 模块增加实时分片 buffer。"),
        ],
        recent=[
            ("user", "运行 pytest tests/test_streaming.py -k test_slice 验证切片边界。"),
            ("assistant", "流式切片测试用例全部通过。"),
        ],
        users=[
            ("user", "给 faster-whisper 项目添加流式音频分块切片处理功能。"),
            ("user", "运行 pytest tests/test_streaming.py -k test_slice 验证切片边界。"),
        ],
        expected_actions={"rename"},
        require_any=("faster-whisper",),
        forbid=("test_streaming.py", "pytest", "音频处理", "软件开发"),
    ),
    Case(
        name="narrow-acceptable-title-divergence",
        purpose="Evaluate divergence between conservative and aggressive on a somewhat narrow but acceptable title.",
        current="个人博客 Markdown 渲染",
        opening=[
            ("user", "开发个人独立博客系统，支持 Markdown 渲染和标签分类。"),
            ("assistant", "设计文章模型与 Markdown 渲染流水线。"),
        ],
        recent=[
            ("user", "博客文章渲染做完了，现在给博客添加友情链接展示页面。"),
            ("assistant", "实现友链展示组件与数据配置。"),
        ],
        users=[
            ("user", "开发个人独立博客系统，支持 Markdown 渲染和标签分类。"),
            ("user", "博客文章渲染做完了，现在给博客添加友情链接展示页面。"),
        ],
        expected_actions={"keep", "rename"},
        forbid=("软件开发", "网页设计"),
    ),
    Case(
        name="aggressive-short-subtask-not-pivot",
        purpose="Ensure aggressive strategy does not mistake a one-off temporary subtask script as a new topic.",
        current="MySQL 迁移 PostgreSQL 方案",
        opening=[
            ("user", "推进生产数据库从 MySQL 全量迁移到 PostgreSQL 的架构方案。"),
            ("assistant", "规划表结构转换、数据同步工具与停机切换窗口。"),
        ],
        recent=[
            ("user", "先写个一次性 python 临时脚本把旧时间戳字段转成 ISO 字符串，导完就删。"),
            ("assistant", "已生成临时转换脚本并在测试集执行完成。"),
            ("user", "好的，继续回到 PostgreSQL 迁移主线，核对外键和索引。"),
        ],
        users=[
            ("user", "推进生产数据库从 MySQL 全量迁移到 PostgreSQL 的架构方案。"),
            ("user", "先写个一次性 python 临时脚本把旧时间戳字段转成 ISO 字符串，导完就删。"),
            ("user", "好的，继续回到 PostgreSQL 迁移主线，核对外键和索引。"),
        ],
        expected_actions={"keep"},
        forbid=("临时脚本", "时间戳", "Python 脚本"),
    ),
    Case(
        name="assistant-proposed-tool-no-hijack",
        purpose="A tool or solution suggested solely by the assistant must not hijack the session topic.",
        current="网页首屏加载速度优化",
        opening=[
            ("user", "优化商城前端首页加载速度，首屏 5MB 太慢了，必须优化。"),
            ("assistant", "我们先分析资源分布，排查大体积 JS 和未压缩图片。"),
        ],
        recent=[
            ("assistant", "我建议引入 Webpack Bundle Analyzer 并迁移到 Rsbuild 进行打包重构。"),
            ("user", "你看着用什么工具都行，只要把首屏资源体积压到 1MB 以内、加载时间进 1 秒。"),
            ("assistant", "明白，目标是 1MB 首屏体积。"),
        ],
        users=[
            ("user", "优化商城前端首页加载速度，首屏 5MB 太慢了，必须优化。"),
            ("user", "你看着用什么工具都行，只要把首屏资源体积压到 1MB 以内、加载时间进 1 秒。"),
        ],
        expected_actions={"keep"},
        forbid=("Webpack", "Rsbuild", "Bundle Analyzer"),
    ),
    Case(
        name="summary-anchor-conflicting-tail",
        purpose="When compaction summary establishes a sustained project, recent crash log should not overwrite the title.",
        current=None,
        blind=True,
        summary="长期主线：重构音视频编辑器的多轨混音引擎，已实现多轨波形对齐、音量包络线计算和 VST3 插件宿主支持。",
        opening=[],
        recent=[
            ("user", "单元测试抛出 `Segmentation fault in libasound.so`，排查 ALSA 音频 buffer 越界。"),
            ("assistant", "检查 ALSA ring buffer 指针与环形队列边界。"),
        ],
        users=[
            ("user", "多轨混音引擎持续开发，今天排查 libasound 的 buffer 越界。"),
            ("user", "越界修完后继续推进 VST3 插件自动化测试。"),
        ],
        expected_actions={"rename"},
        require_any=("混音", "音频", "多轨"),
        forbid=("libasound", "ALSA", "Segmentation fault", "段错误"),
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
