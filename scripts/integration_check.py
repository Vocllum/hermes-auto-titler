"""真实 SessionDB 集成验证：标题来源、更新与唯一性行为。"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_state import SessionDB


def main() -> None:
    checks = []
    with tempfile.TemporaryDirectory(prefix="autotitler-int-") as tmp_dir:
        db = SessionDB(db_path=Path(tmp_dir) / "state.db")
        try:
            s1 = "sess-0001"
            s2 = "sess-0002"
            db.create_session(s1, source="cli")
            db.create_session(s2, source="cli")
            db.append_message(s1, role="user", content="帮我排查后台任务重复触发")
            db.append_message(s1, role="assistant", content="已定位到重复事件")
            db.append_message(s2, role="user", content="请保留我的手改标题")

            # 无标题 → 原生自动标题写入成功，并记录 llm 来源。
            ok = db.set_auto_title(s1, "后台任务重复触发", source=db.TITLE_SOURCE_LLM)
            assert ok is True
            assert db.get_session_title(s1) == "后台任务重复触发"
            assert db.get_session_title_source(s1) == db.TITLE_SOURCE_LLM
            checks.append(("untitled→llm write", "OK"))

            # Hermes 的 set_auto_title 不会直接覆盖已有 llm 标题。
            ok2 = db.set_auto_title(s1, "后台任务去重", source=db.TITLE_SOURCE_LLM)
            assert ok2 is False
            assert db.get_session_title(s1) == "后台任务重复触发"
            checks.append(("llm→llm auto-title no-op", "OK"))

            # 插件采用权威标题写入后恢复 llm provenance。
            ok3 = db.set_session_title(s1, "后台任务去重")
            db.set_session_title_source(s1, db.TITLE_SOURCE_LLM)
            assert ok3 is True
            assert db.get_session_title(s1) == "后台任务去重"
            assert db.get_session_title_source(s1) == db.TITLE_SOURCE_LLM
            checks.append(("plugin update path", "OK"))

            # 手改标题由 SessionDB 标记为 user 来源，供插件入口拒绝自动覆盖。
            assert db.set_session_title(s2, "用户手改标题") is True
            assert db.get_session_title_source(s2) == db.TITLE_SOURCE_USER
            checks.append(("user provenance", "OK"))

            # 标题唯一性冲突必须抛错，插件再负责生成安全后缀重试。
            try:
                db.set_session_title(s1, "用户手改标题")
            except ValueError:
                checks.append(("unique-title conflict", "OK"))
            else:
                raise AssertionError("duplicate title did not raise ValueError")

            conv = db.get_messages_as_conversation(s1, include_ancestors=True)
            assert [m.get("role") for m in conv] == ["user", "assistant"]
            checks.append(("conversation read", "OK"))

            rows = db.list_sessions_rich(limit=10)
            assert {row.get("id") for row in rows} == {s1, s2}
            checks.append(("rich session listing", "OK"))
        finally:
            db.close()

    for check in checks:
        print(" | ".join(check))
    print("temporary database cleanup | OK")


if __name__ == "__main__":
    main()
