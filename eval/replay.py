from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from hermes_auto_titler.messages import (
    clean_captured_text,
    is_summary,
    is_system_noise,
    message_text,
)


def is_human_turn_message(m: Dict[str, Any]) -> tuple[bool, Optional[str]]:
    """判断一条消息是否构成新的真实人类轮次边界。"""
    if m.get("role") != "user":
        return False, None
    raw_text = message_text(m.get("content"))
    cleaned = clean_captured_text(raw_text)
    if not cleaned:
        return False, None
    cleaned_stripped = cleaned.strip()
    if not cleaned_stripped or is_system_noise(cleaned_stripped) or is_summary(cleaned_stripped):
        return False, None
    return True, cleaned_stripped


def slice_prefix(events: List[Dict[str, Any]], turn: int) -> List[Dict[str, Any]]:
    """按人类轮次时间线切片事件列表。

    规则：
    1. turn <= 0 返回空列表；
    2. 消息保留原 role/content/timestamp/order 等所有原始字段；
    3. 完整助手回复、工具输出归入对应的人类轮次；
    4. 压缩摘要（如 [CONTEXT COMPACTION]）只在其真实时间点（即所在位置）可见，绝不向前泄漏；
    5. 子代理汇总包（如 [async delegation batch completed...]）与系统噪声不计新轮次，属于当前进行中的轮次；
    6. 切片在前缀截止点（第 turn+1 个真实人类轮次开始前）精准截断。
    """
    if turn <= 0:
        return []

    sliced: List[Dict[str, Any]] = []
    human_turns_seen = 0
    last_user_text: Optional[str] = None

    for m in events:
        is_human, cleaned_text = is_human_turn_message(m)
        if is_human:
            # 相邻重复用户输入去重
            if last_user_text is not None and cleaned_text == last_user_text:
                pass
            else:
                if human_turns_seen >= turn:
                    # 达到了第 turn 轮之后的下一个新人类轮次，切片截止
                    break
                human_turns_seen += 1
                last_user_text = cleaned_text

        sliced.append(dict(m))

    return sliced


def is_real_session_db_available() -> bool:
    """检查当前环境是否能使用真实的宿主 SessionDB。"""
    try:
        import hermes_state
        return hasattr(hermes_state, "SessionDB") and hasattr(hermes_state.SessionDB, "create_session")
    except Exception:
        return False


class SandboxSqliteSessionDB:
    """轻量 SQLite 沙箱存储。

    在未注入宿主 hermes-agent 源码的 unit test 环境下提供与 SessionDB 完全一致的
    会话创建、标题更新、消息追加与查询接口。
    """

    def __init__(self, db_path: Union[str, Path]) -> None:
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    source TEXT,
                    title TEXT,
                    title_source TEXT,
                    model TEXT,
                    parent_session_id TEXT,
                    started_at REAL
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    role TEXT,
                    content TEXT,
                    timestamp REAL,
                    active INTEGER DEFAULT 1,
                    tool_calls TEXT,
                    tool_name TEXT,
                    tool_call_id TEXT
                )
                """
            )

    def create_session(
        self,
        session_id: str,
        source: str = "cli",
        model: Optional[str] = None,
        parent_session_id: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO sessions (id, source, model, parent_session_id, started_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, source, model, parent_session_id, time.time()),
        )
        return session_id

    def set_session_title(self, session_id: str, title: str) -> bool:
        cur = self.conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
        return cur.rowcount > 0

    def set_session_title_source(self, session_id: str, source: str) -> bool:
        cur = self.conn.execute("UPDATE sessions SET title_source = ? WHERE id = ?", (source, session_id))
        return cur.rowcount > 0

    def get_session_title(self, session_id: str) -> Optional[str]:
        row = self.conn.execute("SELECT title FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return row["title"] if row else None

    def get_session_title_source(self, session_id: str) -> Optional[str]:
        row = self.conn.execute("SELECT title_source FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return row["title_source"] if row else None

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    def append_message(
        self,
        session_id: str,
        role: str,
        content: Optional[str] = None,
        timestamp: Optional[float] = None,
        **kwargs: Any,
    ) -> int:
        ts = timestamp if timestamp is not None else time.time()
        cur = self.conn.execute(
            """
            INSERT INTO messages (session_id, role, content, timestamp, active)
            VALUES (?, ?, ?, ?, 1)
            """,
            (session_id, role, content, ts),
        )
        return cur.lastrowid

    def append_messages_batch(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> int:
        count = 0
        for m in messages:
            self.append_message(
                session_id=session_id,
                role=m.get("role", "user"),
                content=m.get("content"),
                timestamp=m.get("timestamp"),
            )
            count += 1
        return count

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE session_id = ? AND active = 1 ORDER BY id ASC",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_messages_as_conversation(
        self,
        session_id: str,
        include_ancestors: bool = False,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        rows = self.get_messages(session_id)
        conv: List[Dict[str, Any]] = []
        for r in rows:
            conv.append({
                "role": r["role"],
                "content": r["content"],
                "timestamp": r["timestamp"],
            })
        return conv

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


def make_sandbox(
    source_db: Any,
    session_id: str,
    workdir: Union[str, Path],
    *,
    events: Optional[List[Dict[str, Any]]] = None,
) -> Any:
    """在指定 workdir 创建完全隔离的沙箱 SessionDB，并载入会话与前缀切片消息。

    保证：
    1. 沙箱数据库与状态文件严格落在 workdir 内部；
    2. 源数据库只读，运行前后不被写穿或篡改；
    3. 支持宿主真实 SessionDB 与轻量沙箱适配器。
    """
    workdir_path = Path(workdir)
    workdir_path.mkdir(parents=True, exist_ok=True)
    sandbox_db_path = workdir_path / "state.db"

    # 读取源会话元数据
    source_session: Dict[str, Any] = {}
    if hasattr(source_db, "get_session"):
        raw_sess = source_db.get_session(session_id)
        if raw_sess:
            source_session = dict(raw_sess)

    source = source_session.get("source") or "cli"
    model = source_session.get("model")
    parent_id = source_session.get("parent_session_id") or source_session.get("parent_id")

    title = None
    if hasattr(source_db, "get_session_title"):
        title = source_db.get_session_title(session_id)
    if title is None:
        title = source_session.get("title")

    title_source = None
    if hasattr(source_db, "get_session_title_source"):
        title_source = source_db.get_session_title_source(session_id)
    if title_source is None:
        title_source = source_session.get("title_source")

    # 获取消息切片
    if events is None:
        if hasattr(source_db, "get_messages"):
            events = source_db.get_messages(session_id)
        elif hasattr(source_db, "get_session_messages"):
            events = source_db.get_session_messages(session_id)
        elif hasattr(source_db, "get_messages_as_conversation"):
            events = source_db.get_messages_as_conversation(session_id)
        else:
            events = []

    # 构造独立沙箱 DB
    if is_real_session_db_available():
        import hermes_state
        sandbox_db = hermes_state.SessionDB(db_path=sandbox_db_path)
    else:
        sandbox_db = SandboxSqliteSessionDB(db_path=sandbox_db_path)

    # 写入会话元数据
    sandbox_db.create_session(
        session_id=session_id,
        source=source,
        model=model,
        parent_session_id=parent_id,
    )
    if title is not None and hasattr(sandbox_db, "set_session_title"):
        sandbox_db.set_session_title(session_id, title)
    if title_source is not None and hasattr(sandbox_db, "set_session_title_source"):
        sandbox_db.set_session_title_source(session_id, title_source)

    # 写入切片消息
    if events:
        if hasattr(sandbox_db, "append_messages_batch"):
            sandbox_db.append_messages_batch(session_id, events)
        elif hasattr(sandbox_db, "append_message"):
            for m in events:
                sandbox_db.append_message(
                    session_id,
                    role=m.get("role", "user"),
                    content=m.get("content"),
                    timestamp=m.get("timestamp"),
                )

    return sandbox_db
