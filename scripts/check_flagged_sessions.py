"""验证：岚点名的 5 个'太长'会话，用当前代码（v4b 配置）重新生成标题。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_state import SessionDB
from hermes_auto_titler.config import load_config
from hermes_auto_titler.messages import load_context_with_summary
from hermes_auto_titler.titler import AutoTitler

class Ctx:
    def __init__(self):
        from agent.plugin_llm import PluginLlm
        self.llm = PluginLlm(plugin_id="hermes-auto-titler")

TARGETS = [
    "hermes-auto-titler", "自动标题插件开发",
    "技能自动更新脚本", "issue 审查",
    "TencentDB 替代 OpenViking", "数据迁移",
    "搜索方案对比", "Firecrawl",
    "Polymate QQ 机器人", "权限与限流",
]

db = SessionDB()
cfg = load_config()
titler = AutoTitler(Ctx(), cfg, db=db)

rows = db.list_sessions_rich(limit=1000, min_message_count=3, include_children=False)
hits = []
for r in rows:
    t = (r.get("title") or "").strip()
    if any(k in t for k in TARGETS):
        hits.append(r)

print(f"匹配会话 {len(hits)} 个:")
for r in sorted(hits, key=lambda r: -(r.get("message_count") or 0)):
    sid = r["id"]
    n = r.get("message_count") or 0
    old = (r.get("title") or "").strip()
    recent, all_user, opening, earlier_summary = load_context_with_summary(
        db, sid, recent_turns=2, include_all_user=True, opening_turns=2,
        preview_chars=200,
    )
    try:
        action, title = titler._generate(
            None, recent, all_user, opening,
            blind=True, earlier_summary=earlier_summary,
        )
    except Exception as e:
        action, title = "keep", f"(生成失败: {e})"
    print(f"\n{sid}  msgs={n}")
    print(f"  现标题: {old}")
    print(f"  新生成: {title}  ({len(title)}字符)")
db.close()
