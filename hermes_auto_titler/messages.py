"""从 Hermes SessionDB 提取用于标题评估的对话上下文。

只取纯文本：附件（图片/文件）只保留占位符，不展开内容，控制成本。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, List, Optional, Tuple


def char_cols(ch: str) -> int:
    """单字符显示列宽：East Asian Wide/Fullwidth = 2 列，其余 = 1 列。

    中文/全角符号（：、（）等）= 2 列，拉丁字母/数字/半角符号 = 1 列，
    emoji 在 Python 3.11 大多归入 W 也按 2 列。零依赖（unicodedata）。
    """
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(text: str) -> int:
    """标题显示宽度（列），与侧边栏实际渲染宽度同量纲。"""
    return sum(char_cols(ch) for ch in text)


# 截断回退用的分隔符（空格 + 中英文标点）
_TRUNC_BREAKS = set(" 　,，。、；;:：()（）/-—…·`\"'《》【】")


def truncate_to_width(text: str, max_cols: int) -> str:
    """按显示列宽截断；截断点尽量回退到最近分隔符，避免中文词被拦腰切断。

    max_cols <= 0 或宽度不超限时原样返回。回退失败（纯长词）才硬切。
    """
    if max_cols <= 0 or display_width(text) <= max_cols:
        return text
    cols = 0
    for i, ch in enumerate(text):
        c = char_cols(ch)
        if cols + c > max_cols:
            for j in range(i - 1, -1, -1):
                if text[j] in _TRUNC_BREAKS:
                    return text[:j]  # 截到分隔符前，不留尾部空格/标点
            return text[:i]
        cols += c
    return text


def _part_text(part: dict) -> str:
    t = part.get("type", "")
    if t == "text":
        return str(part.get("text", ""))
    if t == "image_url":
        return "[图片]"
    if t in ("file", "input_file"):
        name = part.get("name") or part.get("file_name") or ""
        return f"[文件: {name}]" if name else "[文件]"
    return "[附件]"


def message_text(content: Any) -> str:
    """OpenAI 格式 content（str 或 parts list）→ 纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_part_text(p) for p in content if isinstance(p, dict))
    if isinstance(content, dict):
        return _part_text(content)
    return ""


# Hermes 系统注入噪声（以 user/assistant 角色混进消息流，不是用户真实意图）：
# 模型切换通知、上下文压缩标记、子代理批量完成、后台进程完成、系统打断提示等。
_SYSTEM_NOISE_PREFIXES = (
    "[system:",
    "[system note:",
    "[context compaction",
    "[async delegation",
    "[important:",
    "[your active task list was preserved",
    # cron-memory-maintenance 的最终标记（通常以 assistant 角色写回消息流）
    "memory_maintenance_summary",
)

# 压缩摘要（混在 user 角色）：不是用户意图，也不进轨迹；只有在原始 opening
# 已被压缩替换时，作为单独的「Earlier history summary」返回。这样它不再
# 冒充 Opening；调用方可在日常评估中视为弱提示，在 blind 重生成中视为
# 原始 opening 缺失后的主要历史证据。
_SUMMARY_PREFIXES = (
    "[recent summary",
    "[session arc summary",
    "[session summary",
    "[durable summary",
)


def is_system_noise(text: str) -> bool:
    t = (text or "").lstrip().lower()
    return any(t.startswith(p) for p in _SYSTEM_NOISE_PREFIXES)


def is_summary(text: str) -> bool:
    t = (text or "").lstrip().lower()
    return any(t.startswith(p) for p in _SUMMARY_PREFIXES)


_HANDOFF_END_RE = re.compile(
    r"---\s*end of context summary\s*[—-].*?---\s*",
    re.IGNORECASE | re.DOTALL,
)
_HANDOFF_REPLAY_RE = re.compile(
    r"^\s*\[still in progress[^\]]*\]\s*",
    re.IGNORECASE,
)


def clean_captured_text(text: str) -> Optional[str]:
    """Remove Hermes handoff wrappers while preserving the real user turn.

    Context compaction and replay markers are persisted as ordinary user
    messages.  A whole compaction handoff is not useful title evidence, but
    its final message after ``END OF CONTEXT SUMMARY`` is a real user turn and
    must be retained.  An unfinished handoff is discarded rather than fed to
    the title model as if it were user intent.
    """
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not t:
        return None

    if t.lower().startswith("[context compaction"):
        match = _HANDOFF_END_RE.search(t)
        if not match:
            return None
        t = t[match.end():].strip()

    t = _HANDOFF_REPLAY_RE.sub("", t, count=1).strip()
    if not t or is_system_noise(t):
        return None
    return t


# 句子边界：中文/通用标点 + 换行 + 英文句点后跟空白
_SENT_RE = re.compile(r"[。！？…!?]|(?<=\.)\s|\n")


def smart_preview(text: str, limit: int) -> str:
    """超长消息提取首句与尾部窗口（保留开头实体与尾部连续指令，无任何硬编码词表）。

    - 长度 ≤ limit：零损耗原样返回
    - 有句子/换行边界：
      - 头部提取：首句（预算 limit // 2）
      - 尾部提取：尾窗（从末尾向前尽可能多地容纳完整的连续句子，直到填满剩余预算）
    - 无句子边界（单行长串：日志/代码）：硬切前 2/3 + 后 1/3
    """
    if limit <= 0 or len(text) <= limit:
        return text
    parts = [p.strip() for p in _SENT_RE.split(text) if p.strip()]
    if len(parts) >= 2:
        budget = max(1, limit // 2)
        head = parts[0]
        if len(head) > budget:
            head = head[:budget].rstrip()

        # 尾部窗口：从末尾往前尽可能多地容纳完整的句子（确保倒数多句都在预算内完整保留）
        tail_budget = max(1, limit - len(head) - 3)
        tail_parts = []
        cur_len = 0
        for p in reversed(parts[1:]):
            needed = len(p) + (1 if tail_parts else 0)
            if cur_len + needed <= tail_budget or not tail_parts:
                tail_parts.append(p)
                cur_len += needed
            else:
                break
        tail_parts.reverse()
        tail = " ".join(tail_parts)
        if len(tail) > tail_budget:
            tail = tail[-tail_budget:].lstrip()

        return f"{head} … {tail}"
    cut = limit * 2 // 3
    return text[:cut].rstrip() + " … " + text[-(limit - cut):].lstrip()


def sample_user_messages(users: List[Tuple[str, str]], threshold: int) -> List[Tuple[str, str]]:
    """用户消息条数上限：超限时保留开头 1 条（起点锚点）+ 最近 N-1 条（当前意图）。

    head + tail 策略：第一条锚定会话从哪里开始，最近消息反映当前方向；
    中间的旧主题（含压缩续接会话的祖先内容）对标题价值最低，直接丢弃。
    threshold <= 0 表示不限。
    """
    if threshold <= 0 or len(users) <= threshold:
        return users
    head = 1
    tail = threshold - head
    if tail <= 0:  # threshold=1 时 users[-0:] 会返回全部，必须单独处理
        return users[:head]
    return users[:head] + users[-tail:]


def _sample_turns(
    pairs: List[Tuple[str, str]],
    preview,
) -> List[List[Tuple[str, str]]]:
    """按真实用户消息切轮，每轮只保留用户消息和最后一条模型回复。"""
    turns: List[List[Tuple[str, str]]] = []
    user_text: Optional[str] = None
    assistant_text: Optional[str] = None

    def flush() -> None:
        if user_text is None:
            return
        turn = [("user", preview(user_text))]
        if assistant_text is not None:
            turn.append(("assistant", preview(assistant_text)))
        turns.append(turn)

    for role, text in pairs:
        if role == "user":
            flush()
            user_text = text
            assistant_text = None
        elif user_text is not None:
            assistant_text = text
    flush()
    return turns


def load_context_with_summary(
    db,
    session_id: str,
    recent_turns: int,
    include_all_user: bool,
    opening_turns: int = 2,
    ignore_model_messages: bool = False,
    preview_chars: int = 200,
    user_message_threshold: int = 0,
    user_message_preview_chars: int = 0,
    summary_chars: int = 0,
) -> Tuple[
    List[Tuple[str, str]],
    List[Tuple[str, str]],
    List[Tuple[str, str]],
    Optional[str],
]:
    """返回 (recent, all_user, opening, earlier_summary)。

    earlier_summary 只在可见消息中没有真实 opening、且存在压缩摘要时提供；
    它永远不进入 opening、recent 或用户意图轨迹。summary_chars>0 时用它
    截断摘要（retitle 盲改场景：模型没有当前标题锚点，需要更长摘要来恢复
    Subject）；0 = 沿用 preview_chars。提示强度由调用方决定。
    """
    conv = db.get_messages_as_conversation(session_id, include_ancestors=True) or []
    pairs: List[Tuple[str, str]] = []
    summaries: List[str] = []
    saw_visible_opening = False
    saw_summary = False
    for m in conv:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        if ignore_model_messages and role == "assistant":
            continue
        text = clean_captured_text(message_text(m.get("content")))
        if not text:
            continue
        if is_summary(text):
            summaries.append(text)
            saw_summary = True
            continue
        if is_system_noise(text):
            continue
        if not saw_summary:
            saw_visible_opening = True
        # Handoff/replay can persist the same user turn twice without an
        # assistant response between them.  Keep later turns with the same
        # wording; only collapse the adjacent replay introduced by the
        # transport so repeated user intent remains visible in the trajectory.
        if pairs and role == "user" and pairs[-1] == ("user", text):
            continue
        pairs.append((role, text))

    def preview(text: str) -> str:
        if preview_chars <= 0 or len(text) <= preview_chars:
            return text
        parts = [p.strip() for p in _SENT_RE.split(text) if p.strip()]
        if len(parts) >= 2:
            return smart_preview(text, preview_chars)
        return text[:preview_chars] + "…"

    # 角色配额：每个选中的真实用户轮次只保留用户消息和最后一条模型文本
    # 回复。工具过程已经被过滤；压缩后残留在首条 user 之前的 assistant
    # 片段也不属于任何真实用户轮次，不能冒充 Opening。
    turns = _sample_turns(pairs, preview)
    recent = [item for turn in turns[-recent_turns:] for item in turn]
    opening = [item for turn in turns[:opening_turns] for item in turn]
    # 摘要永远不进入 opening；只有它先于所有可见真实消息时，才作为独立弱提示。
    earlier_summary = None
    if summaries and not saw_visible_opening:
        s = summaries[0]
        n = summary_chars or preview_chars
        earlier_summary = (s[:n] + "…") if n > 0 and len(s) > n else s

    # 用户消息 = 意图轨迹；超长单条提取首尾句，超条数首尾采样
    users = [(r, t) for r, t in pairs if r == "user"]
    if user_message_preview_chars > 0:
        users = [(r, smart_preview(t, user_message_preview_chars)) for r, t in users]
    users = sample_user_messages(users, user_message_threshold)
    return recent, (users if include_all_user else []), opening, earlier_summary


def load_context(
    db,
    session_id: str,
    recent_turns: int,
    include_all_user: bool,
    opening_turns: int = 2,
    ignore_model_messages: bool = False,
    preview_chars: int = 200,
    user_message_threshold: int = 0,
    user_message_preview_chars: int = 0,
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]], List[Tuple[str, str]]]:
    """兼容旧调用：返回 (recent, all_user, opening)，不暴露摘要弱提示。"""
    recent, all_user, opening, _ = load_context_with_summary(
        db,
        session_id,
        recent_turns,
        include_all_user,
        opening_turns,
        ignore_model_messages,
        preview_chars,
        user_message_threshold,
        user_message_preview_chars,
    )
    return recent, all_user, opening
