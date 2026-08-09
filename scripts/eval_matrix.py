"""参数矩阵实验：5 个长会话 × 6 组配置，conservative + blind（重新生成）。

不写库。输出：
1. 每个会话的全文审查素材（全量用户消息轨迹 + 开头/结尾完整消息）
2. 每组配置生成的标题 + 输入规模

用法:
  ~/.hermes/hermes-agent/venv/bin/python scripts/eval_matrix.py
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_state import SessionDB

from hermes_auto_titler.config import load_config
from hermes_auto_titler.messages import load_context
from hermes_auto_titler.titler import AutoTitler

# 7 组参数：preview / opening / recent / style（全部：带AI、用户全量、过滤噪声、conservative+blind）
CONFIGS = [
    ("P50   (50/1/1/concise)", 50, 1, 1, "concise"),
    ("P100  (100/1/1/concise)", 100, 1, 1, "concise"),
    ("P200  (200/1/1/concise)", 200, 1, 1, "concise"),
    ("P400  (400/1/1/concise)", 400, 1, 1, "concise"),
    ("P0    (0/1/1/concise 完整)", 0, 1, 1, "concise"),
    ("R22   (100/2/2/concise)", 100, 2, 2, "concise"),
    ("Scomp (100/1/1/complete)", 100, 1, 1, "complete"),
]

MIN_MSGS = 60   # 只抽长会话
N_SESSIONS = 10
SEED = 7


class Ctx:
    def __init__(self):
        from agent.plugin_llm import PluginLlm

        self.llm = PluginLlm(plugin_id="hermes-auto-titler")


def clip(t: str, n: int) -> str:
    t = t.replace("\n", " ").strip()
    return t if len(t) <= n else t[:n] + "…"


def main():
    db = SessionDB()
    cfg = load_config()
    titler = AutoTitler(Ctx(), cfg, db=db)

    rows = [r for r in db.list_sessions_rich(limit=1000, include_children=False)
            if (r.get("message_count") or 0) >= MIN_MSGS]
    # 排除 user 手改标题
    rows = [r for r in rows
            if db.get_session_title_source(r["id"]) != SessionDB.TITLE_SOURCE_USER]
    random.seed(SEED)
    random.shuffle(rows)
    picked = rows[:N_SESSIONS]

    for i, row in enumerate(picked, 1):
        sid = row["id"]
        n_msgs = row.get("message_count") or 0
        current = row.get("title") or ""
        src = db.get_session_title_source(sid)
        print(f"\n{'='*70}\n[{i}] {sid}  消息数:{n_msgs}  标题来源:{src}")
        print(f"原标题: {current}")

        # ---- 审查素材：全量用户消息轨迹 + 开头/结尾原始消息 ----
        conv = db.get_messages_as_conversation(sid, include_ancestors=True) or []
        users = [m for m in conv if m.get("role") == "user"]
        print(f"--- 用户消息轨迹（{len(users)} 条，全量审查）---")
        for j, m in enumerate(users, 1):
            print(f"  U{j}: {clip(str(m.get('content') or ''), 240)}")
        # 开头/结尾的 assistant 原文（未截断）
        for label, ms in (("开头 4 条", conv[:4]), ("结尾 4 条", conv[-4:])):
            print(f"--- {label}（原文）---")
            for m in ms:
                role = m.get("role")
                if role not in ("user", "assistant"):
                    continue
                print(f"  {role}: {clip(str(m.get('content') or ''), 400)}")

        # ---- 矩阵生成 ----
        print("--- 标题矩阵（全部 conservative + blind 重新生成 + 过滤噪声）---")
        for label, pv, op, rt, style in CONFIGS:
            recent, all_user, opening = load_context(
                db, sid, recent_turns=rt, include_all_user=True,
                opening_turns=op, ignore_model_messages=False, preview_chars=pv,
            )
            n_chars = sum(len(t) for _, t in opening + recent + all_user)
            saved = titler.cfg.get("title_style")
            if style != saved:
                titler.cfg["title_style"] = style
            try:
                action, title = titler._generate(
                    current, recent, all_user, opening, blind=True)
            finally:
                titler.cfg["title_style"] = saved
            print(f"  {label}: {title or '(空)'}  [输入{len(opening+recent+all_user)}条/{n_chars}字符]")
    db.close()


if __name__ == "__main__":
    main()
