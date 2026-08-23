"""批量重命名历史会话：dry-run 预览 → 实际执行。

用法:
  <your-hermes-venv>/bin/python scripts/retitle_all.py --dry-run
  <your-hermes-venv>/bin/python scripts/retitle_all.py [--limit N] [--min-messages M]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_state import SessionDB

from hermes_auto_titler.config import load_config
from hermes_auto_titler.titler import AutoTitler


class Ctx:
    """最小 ctx：真实 PluginLlm（宿主模型通道，含门控检查）。"""

    def __init__(self):
        from agent.plugin_llm import PluginLlm

        self.llm = PluginLlm(plugin_id="hermes-auto-titler")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--min-messages", type=int, default=0)
    args = ap.parse_args()

    db = SessionDB()
    cfg = load_config()
    titler = AutoTitler(Ctx(), cfg, db=db)

    # 先统计范围（dry-run 与执行前都用同一查询）
    rows = db.list_sessions_rich(
        limit=args.limit or 1000,
        min_message_count=args.min_messages,
        include_children=False,
    )
    user_cnt = 0
    legacy_cnt = 0
    empty_cnt = 0
    pending = []
    for row in rows:
        sid = row.get("id")
        if not sid:
            continue
        try:
            src = db.get_session_title_source(sid)
        except Exception:
            src = None
        title = row.get("title") or ""
        if src == SessionDB.TITLE_SOURCE_USER:
            user_cnt += 1
            continue
        if src is None and title:
            legacy_cnt += 1
            continue
        if int(row.get("message_count") or 0) <= 0:
            empty_cnt += 1
            continue
        pending.append((sid, src, title[:50]))

    print(
        f"总会话: {len(rows)} | user 跳过: {user_cnt} | legacy 跳过: {legacy_cnt} "
        f"| 空会话跳过: {empty_cnt} | 模型候选: {len(pending)}"
    )
    if args.dry_run:
        for sid, src, title in pending:
            print(f"  {sid[:16]} [{src}] {title}")
        db.close()
        return

    results = titler.retitle_all(limit=args.limit, min_messages=args.min_messages)
    renamed = [r for r in results if r.get("action") == "renamed"]
    kept = [r for r in results if r.get("action") == "keep"]
    skipped = [r for r in results if r.get("action") == "skipped"]
    failed = [r for r in results if r.get("action") == "failed"]
    errors = [r for r in results if r.get("action") == "error"]

    print(f"\n完成: renamed={len(renamed)} keep={len(kept)} skipped={len(skipped)} "
          f"failed={len(failed)} error={len(errors)}")
    for r in renamed:
        print(f"  ✅ {r['session_id'][:16]} -> {r.get('title')}")
    for r in errors:
        print(f"  ❌ {r['session_id'][:16]} {r.get('reason')}")
    db.close()


if __name__ == "__main__":
    main()
