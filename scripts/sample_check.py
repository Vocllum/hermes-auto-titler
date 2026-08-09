"""抽样核对：标题 vs 会话开头/收尾意图。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_state import SessionDB

SAMPLES = [
    "20260810_021618",  # 当前会话（开发插件 + 批量重命名）
    "20260810_021520",  # Hermes 更新不重建桌面版导致悬浮窗缺失
    "20260809_225806",  # Hermes 更新与上下文记忆预算整理
    "20260808_085739",  # 闪白屏根源排查与修复
    "20260807_233117",  # 上下文优化与记忆区别
    "20260810_015334",  # 排查 skill 报错并清理 ov 服务器 skills
]


def main():
    db = SessionDB()
    for prefix in SAMPLES:
        sid = None
        for row in db.list_sessions_rich(limit=1000, include_children=False):
            if row["id"].startswith(prefix):
                sid = row["id"]
                break
        if not sid:
            print(f"\n=== {prefix}: 未找到")
            continue
        title = db.get_session_title(sid)
        src = db.get_session_title_source(sid)
        msgs = db.get_messages_as_conversation(
            sid, include_ancestors=True, include_inactive=True
        )
        if not msgs:
            print(f"\n=== {prefix}: 无消息")
            continue
        user_msgs = [m for m in msgs if m.get("role") == "user"]
        n = len(msgs)
        print(f"\n=== {prefix}  [{src}] 标题: {title}")
        print(f"    消息 {n} 条, 用户消息 {len(user_msgs)} 条")
        def clip(m, k=90):
            c = (m.get("content") or "")
            if isinstance(c, list):
                c = " ".join(str(x.get("text", x.get("type", ""))) for x in c if isinstance(x, dict))
            c = str(c).replace("\n", " ")[:k]
            return c
        print("    开头用户消息:", clip(user_msgs[0]) if user_msgs else "-")
        if len(user_msgs) > 1:
            print("    第2条用户消息:", clip(user_msgs[1]))
        if len(user_msgs) > 2:
            print("    倒数第2条用户消息:", clip(user_msgs[-2]))
        print("    最后用户消息:", clip(user_msgs[-1]) if user_msgs else "-")
    db.close()


if __name__ == "__main__":
    main()
