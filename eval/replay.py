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

    MAX_TITLE_LENGTH = 100
    TITLE_SOURCE_USER = "user"
    TITLE_SOURCE_LLM = "llm"
    TITLE_SOURCE_DERIVED = "derived"

    def __init__(self, db_path: Union[str, Path]) -> None:
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False, isolation_level=None)
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

    def set_auto_title(self, session_id: str, title: str, source: str = "llm") -> bool:
        cur = self.conn.execute(
            "UPDATE sessions SET title = ?, title_source = ? WHERE id = ?",
            (title, source, session_id),
        )
        return cur.rowcount > 0

    def record_auxiliary_usage(self, session_id: str, task: str, **kwargs: Any) -> None:
        pass

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


class ReplayClock:
    """可由测试/回放框架受控推进的虚拟单调时钟与挂钟时间。"""

    def __init__(self, initial_monotonic: float = 1000.0, initial_time: float = 1727170000.0) -> None:
        self._mono = float(initial_monotonic)
        self._wall = float(initial_time)

    def monotonic(self) -> float:
        return self._mono

    def time(self) -> float:
        return self._wall

    def advance(self, seconds: float) -> None:
        self._mono += float(seconds)
        self._wall += float(seconds)


class ReplayHarness:
    """生产 evaluate() 状态机沙箱回放容器。

    保证：
    1. 每个 harness 实例绑定独立沙箱 SessionDB 与独立临时 state.json，严禁写穿生产；
    2. 注入受控 ReplayClock，支持精确模拟 5 分钟冷却退避；
    3. 支持通过 evaluate() 或 on_session_end / on_session_finalize 触发，并提供 drain_inflight 确定性同步执行；
    4. 记录每轮动作、候选、待审 pending、最终标题与 title_source。
    """

    def __init__(
        self,
        workdir: Union[str, Path],
        cfg: Optional[Dict[str, Any]] = None,
        clock: Optional[ReplayClock] = None,
        mock_llm: Optional[Any] = None,
    ) -> None:
        from unittest.mock import MagicMock
        from hermes_auto_titler.policy import AutoTitler

        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.clock = clock or ReplayClock()

        base_cfg: Dict[str, Any] = {
            "enabled": True,
            "min_interval_minutes": 5,
            "rename_confirmations": 1,
            "every_n_turns": 2,
            "strategy": "conservative",
            "title_style": "concise",
            "summary_preview_chars": 1200,
            "preview_chars": 100,
            "user_message_threshold": 40,
            "user_message_preview_chars": 300,
            "include_all_user_messages": True,
            "ignore_model_messages": False,
        }
        if cfg:
            base_cfg.update(cfg)
        self.cfg = base_cfg

        # 独立沙箱 SessionDB
        db_path = self.workdir / "state.db"
        if is_real_session_db_available():
            import hermes_state
            self.db = hermes_state.SessionDB(db_path=db_path)
        else:
            self.db = SandboxSqliteSessionDB(db_path=db_path)

        # 独立上下文与 LLM
        self.mock_llm = mock_llm or MagicMock()
        self.ctx = MagicMock()
        self.ctx.llm = self.mock_llm

        # 实例化 policy.AutoTitler
        self.titler = AutoTitler(self.ctx, self.cfg, db=self.db)

        # 锁死沙箱 state.json，杜绝写穿现网
        self.titler._state_path = self.workdir / "state.json"

        # 启动时钟 patch
        self._mono_patch = None
        self._time_patch = None
        self.start_clock_patch()

    def start_clock_patch(self) -> None:
        from unittest.mock import patch
        if self._mono_patch is None:
            self._mono_patch = patch("time.monotonic", side_effect=self.clock.monotonic)
            self._mono_patch.start()
        if self._time_patch is None:
            self._time_patch = patch("time.time", side_effect=self.clock.time)
            self._time_patch.start()

    def stop_clock_patch(self) -> None:
        if self._mono_patch is not None:
            try:
                self._mono_patch.stop()
            except Exception:
                pass
            self._mono_patch = None
        if self._time_patch is not None:
            try:
                self._time_patch.stop()
            except Exception:
                pass
            self._time_patch = None

    def evaluate(
        self,
        session_id: str,
        force: bool = False,
        blind: bool = False,
    ) -> Dict[str, Any]:
        """同步运行一次状态机 evaluate() 并捕获完整决策结果。"""
        return self.titler.evaluate(session_id, force=force, blind=blind)

    def on_session_end(self, **payload: Any) -> None:
        """通过真实宿主 hook 路径触发评估调度。"""
        self.titler.on_session_end(**payload)

    def on_session_finalize(self, session_id: str, **payload: Any) -> None:
        """通过真实宿主 hook 终局路径触发收尾评估。"""
        payload["session_id"] = session_id
        self.titler.on_session_finalize(**payload)
        self.titler._retry_failed_sessions()

    def drain_inflight(self, session_id: Optional[str] = None, timeout: float = 5.0) -> None:
        """确定性等待后台 worker 完成，杜绝 sleep 掩盖竞态。"""
        if session_id:
            event = None
            with self.titler._inflight_lock:
                event = self.titler._inflight.get(session_id)
            if event:
                event.wait(timeout)
        else:
            with self.titler._inflight_lock:
                events = list(self.titler._inflight.values())
            for ev in events:
                ev.wait(timeout)

    def record_transition(
        self,
        session_id: str,
        result: Dict[str, Any],
        candidate: Optional[str] = None,
    ) -> Dict[str, Any]:
        """记录完整的状态转移审计快照。"""
        import copy
        return {
            "session_id": session_id,
            "action": result.get("action"),
            "candidate": candidate or result.get("candidate") or result.get("title"),
            "reason": result.get("reason"),
            "pending": copy.deepcopy(self.titler._pending.get(session_id)),
            "db_title": self.db.get_session_title(session_id),
            "db_source": self.db.get_session_title_source(session_id),
            "monotonic": self.clock.monotonic(),
        }

    def close(self) -> None:
        self.stop_clock_patch()
        if hasattr(self.db, "close"):
            try:
                self.db.close()
            except Exception:
                pass

    def __enter__(self) -> ReplayHarness:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

