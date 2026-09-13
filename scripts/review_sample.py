"""真实会话抽样审查：按当前生产配置 dry-run 标题判断，不写 SessionDB。

默认取最近 10 个非 user-title 会话。上下文提取参数全部来自插件 config，避免
测试脚本与真实运行使用不同的 preview / trajectory / summary 预算。

用法:
  <your-hermes-venv>/bin/python scripts/review_sample.py --n 20
  <your-hermes-venv>/bin/python scripts/review_sample.py --n 20 --strategy aggressive
  <your-hermes-venv>/bin/python scripts/review_sample.py --n 20 --blind
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_state import SessionDB

from hermes_auto_titler.config import VALID_STRATEGIES, load_config
from hermes_auto_titler.messages import load_context_with_summary
from hermes_auto_titler.titler import AutoTitler

SHOW = 10
TRUNC = 120


class Ctx:
    def __init__(self):
        from agent.plugin_llm import PluginLlm

        self.llm = PluginLlm(plugin_id="hermes-auto-titler")


def clip(t: str, n: int = TRUNC) -> str:
    t = t.replace("\n", " ").strip()
    return t if len(t) <= n else t[:n] + "…"


def parse_args():
    parser = argparse.ArgumentParser(description="Dry-run real-session title review")
    parser.add_argument("--n", type=int, default=SHOW, help="number of sessions to sample")
    parser.add_argument("--blind", action="store_true", help="regenerate without showing current title")
    parser.add_argument(
        "--strategy",
        choices=sorted(VALID_STRATEGIES),
        help="override configured strategy for this review only",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    db = SessionDB()
    cfg = load_config()
    if args.strategy:
        cfg["strategy"] = args.strategy
    titler = AutoTitler(Ctx(), cfg, db=db)

    rows = list(db.list_sessions_rich(limit=1000, include_children=False))
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
        if len(picked) >= max(0, args.n):
            break

    summary_chars = int(
        cfg.get("retitle_summary_chars", 12000)
        if args.blind
        else cfg.get("summary_preview_chars", 1200)
    )
    route = f"{cfg.get('provider') or '(host default)'}/{cfg.get('model') or '(host default)'}"
    print(
        "# production-config review "
        f"blind={'on' if args.blind else 'off'} strategy={cfg.get('strategy')} route={route} "
        f"preview={cfg.get('preview_chars')} trajectory={cfg.get('user_message_threshold')} "
        f"summary={summary_chars}; skipped user titles={skipped_user}"
    )

    for i, (row, tsrc) in enumerate(picked, 1):
        sid = row["id"]
        current = row.get("title") or ""
        recent, all_user, opening, earlier_summary = load_context_with_summary(
            db,
            sid,
            recent_turns=int(cfg.get("recent_turns", 2)),
            include_all_user=bool(cfg.get("include_all_user_messages", True)),
            opening_turns=int(cfg.get("opening_turns", 2)),
            ignore_model_messages=bool(cfg.get("ignore_model_messages", False)),
            preview_chars=int(cfg.get("preview_chars", 400)),
            user_message_threshold=int(cfg.get("user_message_threshold", 40)),
            user_message_preview_chars=int(cfg.get("user_message_preview_chars", 300)),
            summary_chars=summary_chars,
        )
        action, title = titler._generate(
            current,
            recent,
            all_user,
            opening,
            blind=args.blind,
            earlier_summary=earlier_summary,
            session_id=sid,
        )

        n_msgs = row.get("message_count") or 0
        n_user = len(all_user)
        traj = " | ".join(clip(t, 80) for _, t in all_user[:8])
        if len(all_user) > 8:
            traj += f" …（采样后共 {n_user} 条）"

        guard = " [legacy NULL protected]" if tsrc is None and current else ""
        print(f"\n===== [{i}] {sid[:16]} messages={n_msgs} sampled_users={n_user} source={tsrc}{guard}")
        print(f"current : {current or '(empty)'}")
        print(f"decision: {action}: {title}")
        print(f"intent  : {traj}")
        if earlier_summary:
            print(f"summary : {clip(earlier_summary)}")

    db.close()


if __name__ == "__main__":
    main()
