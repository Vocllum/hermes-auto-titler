"""真实端到端验证：PluginLlm（dsv4）+ 真实 SessionDB + 最近活跃会话。

用法: ~/.hermes/hermes-agent/venv/bin/python scripts/e2e_check.py [session_id]
"""

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
    db = SessionDB()
    sid = sys.argv[1] if len(sys.argv) > 1 else None
    if not sid:
        row = db._conn.execute(
            "SELECT session_id FROM messages ORDER BY id DESC LIMIT 1"
        ).fetchone()
        sid = row["session_id"]
    print(f"目标会话: {sid}")
    print(f"原标题: {db.get_session_title(sid)!r} 来源: {db.get_session_title_source(sid)!r}")

    cfg = load_config()
    print(f"配置: model={cfg['model']!r} strategy={cfg['strategy']} recent_turns={cfg['recent_turns']}")

    titler = AutoTitler(Ctx(), cfg, db=db)
    result = titler.evaluate(sid, force=True)
    print(f"结果: {result}")
    print(f"新标题: {db.get_session_title(sid)!r} 来源: {db.get_session_title_source(sid)!r}")
    db.close()


if __name__ == "__main__":
    main()
