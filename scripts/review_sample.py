"""抽样审查：10 个会话，dry-run 出建议标题，附会话内容摘要供人工审查。

不写库。输出每个会话：
- id / 消息数 / 用户消息数 / 标题来源（平台来源不算标题来源！）
- 原标题
- 模型建议标题（当前配置 preview_chars=200；--blind 走 retitle-all 盲改模式）
- 用户消息意图轨迹（截断）

用法:
  ~/.hermes/hermes-agent/venv/bin/python scripts/review_sample.py [--blind] [--n 10]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_state import SessionDB

from hermes_auto_titler.config import load_config
from hermes_auto_titler.messages import load_context
from hermes_auto_titler.titler import AutoTitler

SHOW = 10
TRUNC = 120  # 单条消息展示截断


class Ctx:
    def __init__(self):
        from agent.plugin_llm import PluginLlm

        self.llm = PluginLlm(plugin_id="hermes-auto-titler")


def clip(t: str, n: int = TRUNC) -> str:
    t = t.replace("\n", " ").strip()
    return t if len(t) <= n else t[:n] + "…"


def main():
    blind = "--blind" in sys.argv
    n_arg = len(sys.argv) - 1 - (1 if blind else 0)
    show = SHOW
    if n_arg > 0 and sys.argv[-1].isdigit():
        show = int(sys.argv[-1])

    db = SessionDB()
    cfg = load_config()
    titler = AutoTitler(Ctx(), cfg, db=db)

    rows = [r for r in db.list_sessions_rich(limit=1000, include_children=False)]
    rows.sort(key=lambda r: r.get("last_active") or "", reverse=True)

    picked = []
    skipped_user = 0
    for row in rows:
        sid = row["id"]
        try:
            tsrc = db.get_session_title_source(sid)
        except Exception:
            tsrc = None
        if tsrc == SessionDB.TITLE_SOURCE_USER:
            skipped_user += 1
            continue
        picked.append((row, tsrc))
        if len(picked) >= show:
            break

    print(f"# blind={'on' if blind else 'off'}  跳过 user 手改标题 {skipped_user} 个")
    for i, (row, tsrc) in enumerate(picked, 1):
        sid = row["id"]
        current = row.get("title") or ""
        recent, all_user, opening = load_context(
            db, sid,
            recent_turns=2, include_all_user=True,
            opening_turns=2, ignore_model_messages=False,
            preview_chars=200,
        )
        action, title = titler._generate(current, recent, all_user, opening, blind=blind)
        n_msgs = row.get("message_count") or 0
        n_user = sum(1 for r, _ in all_user)
        traj = " | ".join(clip(t, 80) for _, t in all_user[:8])
        if len(all_user) > 8:
            traj += f" …（共 {n_user} 条）"

        guard = " [legacy NULL 保护，改不了]" if tsrc is None and current else ""
        print(f"\n===== [{i}] {sid[:16]} 消息{n_msgs}(用户{n_user}) 标题来源:{tsrc}{guard}")
        print(f"原标题: {current or '(空)'}")
        print(f"建议  : {action}: {title}")
        print(f"轨迹  : {traj}")
    db.close()


if __name__ == "__main__":
    main()
