"""A/B 对比：全文梗概模式（preview_chars=200）vs 完整消息（preview_chars=0）。

不写库——直接调 _generate，只打印判定结果 + 输入规模。
用法:
  ~/.hermes/hermes-agent/venv/bin/python scripts/ab_compare.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_state import SessionDB

from hermes_auto_titler.config import load_config
from hermes_auto_titler.messages import load_context
from hermes_auto_titler.titler import AutoTitler

# 抽样：长会话（多用户消息/多轮）优先，覆盖各种形态
SAMPLES = [
    "20260810_021618",  # 当前会话：开发插件 + 批量重命名（长，492 条）
    "20260807_233117",  # 上下文优化与记忆区别（长，245 条，28 条用户消息）
    "20260810_021520",  # Hermes 更新悬浮窗（短，2 条用户消息）
    "20260809_225806",  # 更新与记忆预算整理（中，2 条用户消息）
    "20260810_015334",  # skill 报错清理（短，1 条用户消息）
]


class Ctx:
    def __init__(self):
        from agent.plugin_llm import PluginLlm

        self.llm = PluginLlm(plugin_id="hermes-auto-titler")


def main():
    db = SessionDB()
    cfg = load_config()
    titler = AutoTitler(Ctx(), cfg, db=db)

    for prefix in SAMPLES:
        sid = None
        for row in db.list_sessions_rich(limit=1000, include_children=False):
            if row["id"].startswith(prefix):
                sid = row["id"]
                break
        if not sid:
            print(f"\n=== {prefix}: 未找到")
            continue
        current = db.get_session_title(sid)
        src = db.get_session_title_source(sid)
        print(f"\n=== {sid[:16]} [{src}] 当前标题: {current}")

        for label, pv in (("梗概200", 200), ("完整0", 0)):
            recent, all_user, opening = load_context(
                db, sid,
                recent_turns=2,
                include_all_user=True,
                opening_turns=2,
                ignore_model_messages=False,
                preview_chars=pv,
            )
            n_in = sum(len(t) for _, t in opening + recent + all_user)
            action, title = titler._generate(current, recent, all_user, opening)
            print(f"  [{label}] 输入{len(opening + recent + all_user)}条/{n_in}字符 -> {action}: {title}")
    db.close()


if __name__ == "__main__":
    main()
