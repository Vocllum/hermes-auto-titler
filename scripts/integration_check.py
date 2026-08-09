"""真实 SessionDB 集成验证：标题来源/更新/冲突行为与插件写回路径一致。"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/Users/Vocllum/Hermes/Home/Projects/hermes-auto-titler")

from hermes_state import SessionDB

tmp = Path(tempfile.mkdtemp(prefix="autotitler-int-"))
db = SessionDB(db_path=tmp / "state.db")

# 1. 创建两个会话 + 消息
s1 = "sess-0001"
s2 = "sess-0002"
db.create_session(s1, source="cli")
db.create_session(s2, source="cli")
db.append_message(s1, role="user", content="帮我排查 Raft 空转")
db.append_message(s1, role="assistant", content="查了日志，是 wake 重放")
db.append_message(s2, role="user", content="显示器 HDR 泛白怎么办")

checks = []

# 2. 无标题 → set_auto_title(llm) 生效
ok = db.set_auto_title(s1, "Raft 空转排查", source=db.TITLE_SOURCE_LLM)
checks.append(("untitled→llm write", ok, db.get_session_title(s1), db.get_session_title_source(s1)))

# 3. llm 标题再次用 set_auto_title 更新 → 应为 no-op（不会自我改名）
ok2 = db.set_auto_title(s1, "Raft 空转新标题", source=db.TITLE_SOURCE_LLM)
checks.append(("llm→llm no-op", ok2, db.get_session_title(s1)))

# 4. 插件路径：set_session_title + 恢复 source=llm → 更新成功且仍可升级
ok3 = db.set_session_title(s1, "Raft 空转修复方案")
db.set_session_title_source(s1, db.TITLE_SOURCE_LLM)
checks.append(("plugin path update", ok3, db.get_session_title(s1), db.get_session_title_source(s1)))

# 5. 用户标题权威：user 来源后，插件路径的 llm 更新应被拒绝
db.set_session_title(s2, "用户手改标题")  # user 来源
ok4 = db.set_session_title(s2, "插件想改")  # user 来源会被覆盖 —— 插件代码会先检查 source
src2 = db.get_session_title_source(s2)
checks.append(("s2 user source", src2))

# 6. 唯一性冲突：s2 占用「插件想改」；插件路径 = set_session_title（user 权威写）→ 应 raise ValueError
try:
    db.set_session_title(s1, "插件想改")
    checks.append(("conflict raises", "NO-RAISE"))
except ValueError as e:
    checks.append(("conflict raises", "ValueError OK"))

# 7. get_messages_as_conversation 返回结构
conv = db.get_messages_as_conversation(s1, include_ancestors=True)
checks.append(("conv roles", [(m.get("role"), type(m.get("content")).__name__) for m in conv]))

# 8. list_sessions_rich 可用
rows = db.list_sessions_rich(limit=10)
checks.append(("rich rows", [(r.get("id"), r.get("title"), r.get("message_count")) for r in rows]))

db.close()
for c in checks:
    print(" | ".join(str(x) for x in c))
