"""从 Hermes SessionDB 提取用于标题评估的对话上下文。

只取纯文本：附件（图片/文件）只保留占位符，不展开内容，控制成本。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple


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
    r"---\s*end of context summary.*?---\s*",
    re.IGNORECASE | re.DOTALL,
)
_HANDOFF_REPLAY_RE = re.compile(
    r"^\s*\[still in progress[^\]]*\]\s*",
    re.IGNORECASE,
)

_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_TOOL_SYNTAX_RE = re.compile(r"\[(?:tool|terminal|patch|read_file|search_files|write_file)[^\]]*\]", re.IGNORECASE)
_XML_CONTROL_TAGS_RE = re.compile(
    r"<(?:command-message|local-command-stdout|system-reminder|tool_call|tool_response|thought|thinking)\b[\s\S]*?</(?:command-message|local-command-stdout|system-reminder|tool_call|tool_response|thought|thinking)>",
    re.IGNORECASE,
)
_STANDALONE_XML_TAGS_RE = re.compile(r"</?(?:command-message|local-command-stdout|system-reminder|tool_call|tool_response|thought|thinking)\b[^>]*>", re.IGNORECASE)


def clean_assistant_dialog(text: str) -> str:
    """剔除代码块、工具调用标记与原生 XML 控制标签，提取纯净的自然语言对白与结论。"""
    if not text:
        return ""
    t = _CODE_BLOCK_RE.sub(" [代码] ", text)
    t = _XML_CONTROL_TAGS_RE.sub("", t)
    t = _STANDALONE_XML_TAGS_RE.sub("", t)
    t = _TOOL_SYNTAX_RE.sub("", t)
    lines = [line.strip() for line in t.splitlines() if line.strip()]
    return " ".join(lines)


def _extract_compaction_summary(text: str) -> Optional[str]:
    """从 Hermes 压缩包装（含普通与 merged 载体）中提取摘要正文。

    前导指令段以 ``avoid repeating it:`` 结尾，正文在其后；``--- END OF CONTEXT
    SUMMARY ---`` 之前。
    """
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    # 只要包含上下文压缩标记即可，不要求严格处于消息首字符（支持 merged carrier）
    idx_compaction = t.lower().find("[context compaction")
    if idx_compaction < 0:
        return None

    # 优先定位 avoid repeating it:
    marker = "avoid repeating it:"
    idx = t.lower().find(marker, idx_compaction)
    body_start = (idx + len(marker)) if idx >= 0 else (idx_compaction + len("[context compaction"))

    # 找到结束标记
    end_match = _HANDOFF_END_RE.search(t, body_start)
    body_end = end_match.start() if end_match else len(t)
    body = t[body_start:body_end].strip()
    if not body:
        return None
    # 清洗掉 Hermes 写给主 Agent 的框架级行为指令，避免误导标题模型偏向尾部
    body = re.sub(r"(?i)historical only;\s*newer protected-tail messages after this summary win\.?", "", body)
    body = re.sub(r"(?i)respond ONLY to the latest user message.*?\n", "", body)
    return body.strip() if body.strip() else None


def clean_captured_text(text: str) -> Optional[str]:
    """Remove Hermes handoff wrappers while preserving the real user turn.

    Context compaction and replay markers are persisted as ordinary user
    messages. A whole compaction handoff is not useful title evidence, but
    its final message after ``END OF CONTEXT SUMMARY`` is a real user turn and
    must be retained. An unfinished handoff is discarded rather than fed to
    the title model as if it were user intent.
    """
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not t:
        return None

    # 如果文本包含 context compaction 或 handoff 结束标记，提取结束标记之后的真实用户消息
    match = _HANDOFF_END_RE.search(t)
    if match:
        t = t[match.end():].strip()
    elif "[context compaction" in t.lower() or "[prior context" in t.lower():
        # 未完成的 handoff 包装，丢弃
        return None

    t = _HANDOFF_REPLAY_RE.sub("", t).strip()
    if not t or is_system_noise(t):
        return None
    return t


# 句子边界：中文/通用标点 + 换行 + 英文句点后跟空白
_SENT_RE = re.compile(r"[。！？…!?]|(?<=\.)\s|\n")


_SECTION_RE = re.compile(r"^##\s+.*$", re.MULTILINE)


def _summary_sections(text: str) -> List[str]:
    """按 markdown 二级标题把摘要切成完整小节；无标题时返回空列表。"""
    marks = list(_SECTION_RE.finditer(text))
    sections: List[str] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        piece = text[m.start():end].strip()
        if piece:
            sections.append(piece)
    return sections


def summary_preview(text: str, limit: int, head_share: float = 0.6) -> str:
    """压缩摘要的结构化预览：按小节整体取舍，不做线性硬切。

    压缩块的信息密度集中在 markdown 小节里，而小节的先后顺序本身就编码了
    「主线在前、最新状态在后」。线性前切会系统性丢掉尾部小节，按句子边界切的
    窗口又会退化（压缩块标题不是句子，首句往往只是包装残片）。

    - 无 markdown 小节（自定义压缩模板）：退回 ``smart_preview`` 的句子边界行为
    - 有标题的小节：头池按顺序取整节，尾池从末尾向前取整节，中段整体丢弃
    - 首节单独超出预算时只切它；尾节永不切——半截尾节会伪造一个结尾
    - 尾池装不下整节时按句子边界补满，预算不空转；输出上限由 ``smart_preview``
      的省略号放宽到 ``limit + 1``，调用方的长度断言按此判定
    """
    if limit <= 0 or len(text) <= limit:
        return text
    sections = _summary_sections(text)
    if not sections:
        return smart_preview(text, limit)
    head_budget = max(1, int(limit * head_share))
    tail_budget = max(1, limit - head_budget)
    chosen: List[str] = []
    used = 0
    for section in sections:
        if used >= head_budget:
            break
        room = head_budget - used
        if len(section) <= room:
            chosen.append(section)
            used += len(section)
        elif not chosen:
            chosen.append(section[:room].rstrip())
            used = head_budget
    used_tail = 0
    tail: List[str] = []
    for section in reversed(sections):
        if used_tail >= tail_budget:
            break
        if len(section) <= tail_budget - used_tail:
            tail.insert(0, section)
            used_tail += len(section)
    if not tail:
        # 尾池装不下任何整节时，退回句子边界窗口，别把预算白白空着
        return "\n\n".join(chosen + [smart_preview(text, tail_budget)])
    return "\n\n".join(chosen + tail)


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
    """确定性分层采样：保留开头、结尾，并在中段均匀采样，覆盖整个会话弧线。

    避免中间关键阶段被整体丢弃（防止 U 型注意力断层），严格按原始时间序列返回。
    threshold <= 0 表示不限。
    """
    n = len(users)
    if threshold <= 0 or n <= threshold:
        return users
    if threshold == 1:
        return [users[0]]
    if threshold == 2:
        return [users[0], users[-1]]

    # 分配预算：head 2 条，tail 取 threshold // 3，其余给中段均匀覆盖
    head_count = min(2, threshold - 2)
    tail_count = max(2, threshold // 3)
    mid_count = threshold - head_count - tail_count

    head_indices = list(range(head_count))
    tail_indices = list(range(n - tail_count, n))

    mid_start = head_count
    mid_end = n - tail_count
    if mid_count > 0 and mid_end > mid_start:
        step = (mid_end - mid_start) / (mid_count + 1)
        mid_indices = [int(mid_start + step * (i + 1)) for i in range(mid_count)]
    else:
        mid_indices = []

    chosen = sorted(set(head_indices + mid_indices + tail_indices))
    return [users[i] for i in chosen]


def _sample_turns(
    pairs: List[Tuple[str, str]],
    preview,
) -> List[List[Tuple[str, str]]]:
    """按真实用户消息切轮，每轮只保留用户消息和最后一条模型回复。"""
    turns: List[List[Tuple[str, str]]] = []
    user_text: Optional[str] = None
    assistant_text: Optional[str] = None

    def _apply_preview(role: str, text: str) -> str:
        try:
            return preview(role, text)
        except TypeError:
            return preview(text)

    def flush() -> None:
        if user_text is None:
            return
        turn = [("user", _apply_preview("user", user_text))]
        if assistant_text is not None:
            turn.append(("assistant", _apply_preview("assistant", assistant_text)))
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
        raw_text = message_text(m.get("content"))
        # 优先检测 [CONTEXT COMPACTION] 包装的压缩摘要：clean_captured_text
        # 会把它当作 handoff wrapper 丢弃（只保留末尾跟随的真实用户消息），
        # 但摘要正文本身包含主线信息，标题评估需要它。
        compaction_body = _extract_compaction_summary(raw_text)
        if compaction_body:
            summaries.append(compaction_body)
            saw_summary = True
            # 不 continue——继续走 clean_captured_text 提取末尾可能的真实用户消息
        text = clean_captured_text(raw_text)
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

    def preview(role: str, text: str) -> str:
        if role == "assistant":
            text = clean_assistant_dialog(text)
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
    # opening 与 recent 可能取到同一批轮次（压缩后可见轮次不足时必然如此）。
    # 同一上下文在 prompt 里出现两遍会被模型当成「开头和结尾说的是同一件事」
    # 加权，锚定效应加倍。按轮次下标剔除重叠，opening 只保留 recent 之前的轮次；
    # 若因此为空，说明可见历史整体就是 recent，此时降级保留首批轮次而不是返回空
    # ——policy 侧需要 opening 段来承载 subject 线索，空列表会让它整段消失。
    recent_turn_start = max(0, len(turns) - recent_turns)
    opening = [
        item
        for turn_idx, turn in enumerate(turns[:opening_turns])
        if turn_idx < recent_turn_start
        for item in turn
    ]
    if not opening:
        # 可见历史整体就是 recent：降级只保留首批轮次的用户消息，
        # 既不让 opening 段消失，也不把同一轮重复两遍
        opening = [
            item
            for turn_idx, turn in enumerate(turns[:opening_turns])
            if turn_idx == 0
            for item in turn
            if item[0] == "user"
        ]
    # 摘要永远不进入 opening；只有它先于所有可见真实消息时，才作为独立弱提示。
    earlier_summary = None
    if summaries and not saw_visible_opening:
        s = summaries[0]
        n = summary_chars or preview_chars
        earlier_summary = summary_preview(s, n) if n > 0 and len(s) > n else s

    # 用户消息 = 意图轨迹；超长单条提取首尾句，超条数分层采样
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
