"""从 Hermes SessionDB 提取用于标题评估的对话上下文。

只取纯文本：附件（图片/文件）只保留占位符，不展开内容，控制成本。
"""

from __future__ import annotations

import re
from typing import Any, List, Tuple


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
)

# 压缩摘要（混在 user 角色）：同样不是用户意图，不进轨迹；但它是压缩续接
# 会话唯一的历史浓缩（原始消息已被替换），作为 opening 的「历史锚点」——
# 否则模型只能看到压缩点之后的助手干活消息，主线锚点丢失（标题漂移根因）。
_SUMMARY_PREFIXES = (
    "[recent summary",
    "[session arc summary",
    "[session summary",
)


def is_system_noise(text: str) -> bool:
    t = (text or "").lstrip().lower()
    return any(t.startswith(p) for p in _SYSTEM_NOISE_PREFIXES)


def is_summary(text: str) -> bool:
    t = (text or "").lstrip().lower()
    return any(t.startswith(p) for p in _SUMMARY_PREFIXES)


# 句子边界：中文/通用标点 + 换行 + 英文句点后跟空白
_SENT_RE = re.compile(r"[。！？…!?]|(?<=\.)\s|\n")


def smart_preview(text: str, limit: int) -> str:
    """超长消息提取首尾句（替代硬切）。

    - 长度 ≤ limit：原样
    - 有句子边界：保留第一句 + 最后一句（各限 limit//2），中间省略
    - 无句子边界（单行长串：日志/代码）：硬切前 2/3 + 后 1/3
    """
    if limit <= 0 or len(text) <= limit:
        return text
    parts = [p.strip() for p in _SENT_RE.split(text) if p.strip()]
    if len(parts) >= 2:
        head, tail = parts[0], parts[-1]
        budget = max(1, limit // 2)
        if len(head) > budget:
            head = head[:budget].rstrip()
        if len(tail) > budget:
            tail = tail[-budget:].lstrip()
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
    """返回 (最近 N 轮 user/assistant 对, 全部用户消息, 开头 M 轮)，均为 (role, text)。

    开头几轮用于让模型看到会话主线（标题不应被最新小任务带偏）。
    ignore_model_messages=True 时过滤掉 assistant 消息（recent/opening 只含 user）。
    preview_chars：opening/recent 的每条消息只保留前 N 字符（≈ 前几句话），
    让模型看到的是「开头两句 + 结尾两句」的全文梗概，而不是被超长回复淹没。
    user_message_threshold：用户消息条数上限，超限时 head+tail 采样（开头 1 条 + 最近 N-1 条）。
    user_message_preview_chars：单条用户消息超长时提取首尾句（smart_preview）。
    """
    conv = db.get_messages_as_conversation(session_id, include_ancestors=True) or []
    pairs: List[Tuple[str, str]] = []
    summaries: List[str] = []
    for m in conv:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        if ignore_model_messages and role == "assistant":
            continue
        text = message_text(m.get("content")).strip()
        if not text:
            continue
        if is_summary(text):
            summaries.append(text)
            continue
        if is_system_noise(text):
            continue
        pairs.append((role, text))

    def preview(text: str) -> str:
        if preview_chars > 0 and len(text) > preview_chars:
            return text[:preview_chars] + "…"
        return text

    recent: List[Tuple[str, str]] = []
    seen_user = 0
    for role, text in reversed(pairs):
        recent.append((role, preview(text)))
        if role == "user":
            seen_user += 1
            if seen_user >= recent_turns:
                break
    recent.reverse()

    # 按轮切分（user 消息开头，含其后的 assistant 回应），取前 opening_turns 轮
    rounds: List[List[Tuple[str, str]]] = []
    cur: List[Tuple[str, str]] = []
    for role, text in pairs:
        if role == "user" and cur:
            rounds.append(cur)
            cur = []
        cur.append((role, preview(text)))
    if cur:
        rounds.append(cur)
    opening = [item for r in rounds[:opening_turns] for item in r]
    # 压缩摘要作历史锚点：放在 opening 最前（时间上早于所有可见消息）。
    # 取最早一条（最接近会话起点，主线线索最原始）；截断与 opening 一致。
    if summaries:
        opening.insert(0, ("user", preview(summaries[0])))

    # 用户消息 = 意图轨迹；超长单条提取首尾句，超条数首尾采样
    users = [(r, t) for r, t in pairs if r == "user"]
    if user_message_preview_chars > 0:
        users = [(r, smart_preview(t, user_message_preview_chars)) for r, t in users]
    users = sample_user_messages(users, user_message_threshold)
    return recent, (users if include_all_user else []), opening
