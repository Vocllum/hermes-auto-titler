"""逐步演进回放（Step-by-step Evolution Simulation）：
模拟会话逐轮推进过程（Turn 1 -> Turn 2 -> Turn 3 ...），每一步以各自上一步确立的标题作为
Current Title，分别在 Conservative 与 Aggressive 策略下评估，观察策略在时间轴上的演化分歧、
转折时延与防抖抗噪表现。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_state import SessionDB
from hermes_auto_titler.config import load_config
from hermes_auto_titler.messages import (
    clean_captured_text,
    message_text,
    sample_user_messages,
    smart_preview,
    _sample_turns,
)
from hermes_auto_titler.titler import AutoTitler


class Ctx:
    def __init__(self):
        from agent.plugin_llm import PluginLlm
        self.llm = PluginLlm(plugin_id="hermes-auto-titler")


def build_step_context(
    turns_so_far: List[List[Tuple[str, str]]],
    opening_turns_k: int = 2,
    recent_turns_k: int = 2,
    preview_limit: int = 400,
    user_traj_threshold: int = 40,
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]], List[Tuple[str, str]]]:
    """根据截止当前轮次的历史，构建 opening, recent, all_user。"""
    flat: List[Tuple[str, str]] = []
    for turn in turns_so_far:
        for role, text in turn:
            flat.append((role, text))

    sampled = _sample_turns(flat, lambda t: smart_preview(t, preview_limit))
    opening_turns = sampled[:opening_turns_k]
    recent_turns = sampled[-recent_turns_k:] if len(sampled) > opening_turns_k else []

    opening: List[Tuple[str, str]] = []
    for turn in opening_turns:
        opening.extend(turn)

    recent: List[Tuple[str, str]] = []
    for turn in recent_turns:
        recent.extend(turn)

    # 提取所有 user 消息轨迹
    all_users: List[Tuple[str, str]] = []
    for role, text in flat:
        if role == "user":
            all_users.append((role, text))
    user_trajectory = sample_user_messages(all_users, user_traj_threshold)

    return recent, user_trajectory, opening


def run_sequence(
    name: str,
    turns: List[List[Tuple[str, str]]],
    titler_cons: AutoTitler,
    titler_agg: AutoTitler,
):
    print(f"\n{'='*70}")
    print(f"🎬 演进序列: {name} (共 {len(turns)} 轮)")
    print(f"{'='*70}")

    curr_cons: Optional[str] = None
    curr_agg: Optional[str] = None

    for step in range(1, len(turns) + 1):
        turns_now = turns[:step]
        user_turn_text = next((text for role, text in turns_now[-1] if role == "user"), "")
        summary_user = user_turn_text.replace("\n", " ").strip()
        if len(summary_user) > 60:
            summary_user = summary_user[:60] + "…"

        recent, all_user, opening = build_step_context(turns_now)

        # Conservative 判定
        is_first_step = (step == 1)
        action_cons, gen_cons = titler_cons._generate(
            curr_cons,
            recent,
            all_user,
            opening,
            blind=is_first_step,
            force_rename=is_first_step,
        )
        if action_cons == "rename" and gen_cons:
            next_cons = gen_cons
        else:
            next_cons = curr_cons or gen_cons or "(none)"

        # Aggressive 判定
        action_agg, gen_agg = titler_agg._generate(
            curr_agg,
            recent,
            all_user,
            opening,
            blind=is_first_step,
            force_rename=is_first_step,
        )
        if action_agg == "rename" and gen_agg:
            next_agg = gen_agg
        else:
            next_agg = curr_agg or gen_agg or "(none)"

        # 检查两者是否有分歧
        diverged = (action_cons != action_agg) or (next_cons != next_agg)
        div_flag = " ⚡ [策略分歧]" if diverged else ""

        print(f"\n▶ Step {step}: 用户说: \"{summary_user}\"{div_flag}")
        print(f"   [Conservative] action={action_cons:<6} -> {next_cons}")
        print(f"   [Aggressive  ] action={action_agg:<6} -> {next_agg}")

        curr_cons = next_cons
        curr_agg = next_agg


def get_real_session_turns(db: SessionDB, sid_prefix: str) -> Optional[Tuple[str, List[List[Tuple[str, str]]]]]:
    rows = list(db.list_sessions_rich(limit=500, include_children=False))
    match = [r for r in rows if r["id"].startswith(sid_prefix)]
    if not match:
        return None
    row = match[0]
    conv = db.get_messages_as_conversation(row["id"], include_ancestors=True) or []
    pairs = []
    for m in conv:
        role = m.get("role")
        raw = m.get("content") or ""
        text = clean_captured_text(message_text(raw))
        if text and role in ("user", "assistant"):
            pairs.append((role, text))
    turns = _sample_turns(pairs, lambda t: t)
    return row.get("title") or row["id"], turns


def main():
    cfg = load_config()
    cfg_cons = dict(cfg)
    cfg_cons["strategy"] = "conservative"
    titler_cons = AutoTitler(Ctx(), cfg_cons)

    cfg_agg = dict(cfg)
    cfg_agg["strategy"] = "aggressive"
    titler_agg = AutoTitler(Ctx(), cfg_agg)

    # 1. 典型合成场景 1: 项目生命周期中的子任务与偶发 Bug 冲击
    seq_bug = [
        [
            ("user", "我们启动 FlowMouse 项目开发，这是一个跨平台的鼠标手势增强工具。"),
            ("assistant", "好，先设计手势录制、八方向链码识别和托盘常驻架构。"),
        ],
        [
            ("user", "已完成 Windows 平台的鼠标钩子实现，开始写手势识别逻辑。"),
            ("assistant", "实现链码匹配算法与动作映射表。"),
        ],
        [
            ("user", "发现 Windows 下 SetWindowsHookEx 偶发 5ms 消息丢包，写个测试用例排查下。"),
            ("assistant", "排查消息泵循环与异步回调延迟。"),
        ],
        [
            ("user", "测试用例跑通了，偶发延迟是系统电源管理引起的，bug 已修复。继续回来做手势编辑 UI。"),
            ("assistant", "开始构建手势设置与快捷键绑定面板。"),
        ],
        [
            ("user", "UI 做完了，准备为 FlowMouse 编写 1.0.0 发布说明和打包脚本。"),
            ("assistant", "整理安装包脚本与 Changelog。"),
        ],
    ]
    run_sequence("场景一：主线项目经历中间 Bug 与测试冲击，重回主线至发布", seq_bug, titler_cons, titler_agg)

    # 2. 典型合成场景 2: 渐进式转向（从原主题逐步漂移至新主题）
    seq_pivot = [
        [
            ("user", "继续维护 hermes-auto-titler，优化长会话标题提取规则。"),
            ("assistant", "先审查 policy.py 中的决策规则。"),
        ],
        [
            ("user", "现在改一下 smart_preview 的头尾截断策略。"),
            ("assistant", "调整首句与尾部窗口比例。"),
        ],
        [
            ("user", "auto-titler 搞得差不多了。我们看下 Zen Browser 的标签页同步怎么搞？"),
            ("assistant", "Zen Browser 基于 Firefox，可以使用 Firefox Sync 或编写专用扩展。"),
        ],
        [
            ("user", "就用扩展方式，帮我写个 Zen Browser 扩展的 manifest.json 和 background 脚本。"),
            ("assistant", "生成 manifest v3 配置与标签监听逻辑。"),
        ],
        [
            ("user", "扩展写好了，今天接下来全力做这个 Zen Browser 标签同步扩展的本地测试。"),
            ("assistant", "测试扩展在 Zen 环境下的加载与消息通信。"),
        ],
    ]
    run_sequence("场景二：从旧项目平滑渐进转向新项目（多轮渐进式转题）", seq_pivot, titler_cons, titler_agg)

    # 3. 真实会话回放: 20260912_164543_ (Hermes 插件 Hub 提交 -> 插件清理与浏览器注入)
    db = SessionDB()
    real_data = get_real_session_turns(db, "20260912_164543")
    if real_data:
        title, turns = real_data
        # 仅取前 6 轮
        run_sequence(f"真实会话演进: {title} (20260912_164543)", turns[:6], titler_cons, titler_agg)


if __name__ == "__main__":
    main()
