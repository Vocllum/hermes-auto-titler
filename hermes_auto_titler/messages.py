"""从 Hermes SessionDB 提取用于标题评估的对话上下文。

只取纯文本：附件（图片/文件）只保留占位符，不展开内容，控制成本。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional, Set, Tuple


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

# 真实压缩载体的首部行：必须以行首（允许前导空白）的
# `[CONTEXT COMPACTION` 或 `[PRIOR CONTEXT` 开头，并且必须含有正文标记
# `avoid repeating it:`。两个条件缺一不可：
# - 只认首部：agent 在讨论、调试或用 JSON/代码块转储消息流时，会把这两个
#   字符串连同结束标记一起引用出来。实测全库 392 条这样的消息会被旧逻辑
#   误判成摘要，其中 308 条 role=tool（插件本就不读）、72 条 role=assistant
#   ——它们把「讨论压缩机制的长篇大论」当成摘要喂给标题模型，是噪声注入。
# - 要求正文标记：真实载体的前导指令段固定以 `avoid repeating it:` 收尾，
#   引用场景极少连它一起抄。
_COMPACTION_HEADER_LINE_RE = re.compile(
    r"^[ \t]*\[(?:CONTEXT COMPACTION|PRIOR CONTEXT)",
    re.IGNORECASE | re.MULTILINE,
)
_COMPACTION_BODY_MARK = "avoid repeating it:"

# 围栏代码块：agent 调试/转储时会把真实载体连同样式粘进 ``` 块里。判别首部行
# 是否落在未闭合的围栏内，避免把这种复制粘贴当成真载体。
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*$", re.MULTILINE)


def _in_fenced_block(text: str, pos: int) -> bool:
    """``pos`` 是否落在某个未闭合的围栏代码块内部。"""
    depth = 0
    for m in _FENCE_RE.finditer(text):
        if m.start() >= pos:
            break
        depth = 1 - depth if m.group(1)[0] * 3 == m.group(1)[:3] else depth
        if depth < 0:
            depth = 0
    return depth == 1


def is_compaction_carrier(text: str) -> bool:
    """这条消息是不是真正的 Hermes 压缩载体（而非 agent 对压缩机制的讨论）。

    判据是「首部行 + 正文标记」同时成立：行首（允许前导空白）出现
    `[CONTEXT COMPACTION` 或 `[PRIOR CONTEXT`，且消息里含有
    `avoid repeating it:`。全库实测：旧逻辑仅凭字符串出现就提取，会把 agent
    讨论/转储压缩标记的 392 条消息也当成摘要（其中 72 条 role=assistant），
    把长篇排错文字灌进标题模型。

    首部行还必须不在围栏代码块内——agent 调试时会把真实载体连同样式粘进
    ``` 块，那种复制粘贴同样不是这条消息在讲的内容。
    """
    if not text:
        return False
    if _COMPACTION_BODY_MARK not in text.lower():
        return False
    for m in _COMPACTION_HEADER_LINE_RE.finditer(text):
        if not _in_fenced_block(text, m.start()):
            return True
    return False


def has_compaction_handoff(text: str) -> bool:
    """这条消息是否含「需要按 handoff 切一刀」的压缩结构。

    比 ``is_compaction_carrier`` 宽一档：有正文标记、或有独占整行的结束标记
    即可。用于 ``clean_captured_text`` 的切割决策。

    **但切割有一个额外的必要条件：消息必须以压缩首部开头。** 子代理汇报会把
    真实载体连同样式整段粘进报告正文，那种消息里的压缩文本是「被引用的材料」
    而不是当前载体；在它身上切一刀，报告的尾部就会被当成真实用户消息回灌。
    实测这 7 条各 8733 字符，共 52,398 字符的「用户意图」其实来自别的 agent
    对压缩机制的讨论。因此首部行必须是全文第一个非空字符。
    """
    if not text:
        return False
    if not re.match(
        r"^[ \t]*\[(?:CONTEXT COMPACTION|PRIOR CONTEXT)", text, re.IGNORECASE
    ):
        return False
    if _COMPACTION_BODY_MARK in text.lower():
        return True
    return _HANDOFF_END_LINE_RE.search(text) is not None

# 系统噪声包装（user 角色）：包装本身是运行时元数据，但同一user消息里
# 包装之后紧跟的正文是用户原话。实测（370 个血缘>=100 的 session）：
# - `interrupted mid-run`：34 条，全部带正文，其中 4 条 payload 长度 >40
#   且是人类原话（「给aside装上浏览器拓展…」），其余是被截断的助手工作现场；
# - `model has changed`：121 条，101 条正文为空、20 条带正文，带正文的 20 条
#   全是真实人类原话（「我想让你每天自己运营…」）。
# 所以正确规则是「一律解包、正文为空则丢弃」，而不是按包装类型分流：
# 空正文自动退化成丢弃，不需要为每种包装单独设阈值。
_UNPACK_SYSTEM_WRAPPER_RE = re.compile(
    r"^\[(?:"
    r"system note:\s*your previous turn was interrupted mid-run"
    r"|system:\s*the active model for this chat has changed"
    r"|system:\s*the previous response was cut off by a network error"
    r")[^\]]*\]",
    re.IGNORECASE,
)

# 群聊信封：每条群聊消息都以 `[Group chat: "<room>"] …` 开头，正文是
# 「New messages in the room」段，尾部固定追加 `Rules for this room:`。
# 实测 116 封 / 315,175 字符中，规则块占 32.4%、首部占 6.9%——
# 模型看到的「用户意图」有 39.5% 是房间规则样板。剥掉首尾、只留消息段。
_GROUP_CHAT_RE = re.compile(r"^\[Group chat:", re.IGNORECASE)
_GROUP_MESSAGES_RE = re.compile(r"^New messages in the room[^\n]*\n", re.IGNORECASE | re.MULTILINE)
_GROUP_RULES_RE = re.compile(r"^Rules for this room:[\s\S]*$", re.IGNORECASE | re.MULTILINE)


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
# 结束标记的「可信」形态：独占一整行、位于第 0 列、匹配本身不跨行。
# `_HANDOFF_END_RE` 的尾部 `---\s*` 会把紧随的换行吞进 group，所以按行匹配、
# 用 `$` 锚定行尾，天然排除正文里反引号引用造成的提前命中。找不到可信行时
# 调用方回退到 `_HANDOFF_END_RE` 的首次命中——某些载体（例如被
# `[STILL IN PROGRESS …]` 紧跟的真实收尾）只有非独占形态。
_HANDOFF_END_LINE_RE = re.compile(
    r"^---\s*end of context summary[^\n]*---\s*$",
    re.IGNORECASE | re.MULTILINE,
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


def _compaction_body_bounds(t: str, body_start: int) -> int:
    """摘要正文的结束下标：优先取独占整行的结束标记，否则回退首次命中。

    独占整行的标记才是真正的收尾；正文小节里反引号引用的示例文本会在更早的
    位置命中同样的 `--- end of context summary … ---`，把可用摘要砍掉一半，
    而且被砍掉的后半段随后又被 `clean_captured_text` 当成「真实用户消息」
    回灌——同一条摘要被当成两种东西各喂一遍（实测 afa67c：85% 的载体内容
    重复进入 prompt）。找不到独占行时才回退，避免误伤被 `[STILL IN
    PROGRESS …]` 紧跟的真实收尾。
    """
    m = _HANDOFF_END_LINE_RE.search(t, body_start)
    if m is not None:
        return m.start()
    m = _HANDOFF_END_RE.search(t, body_start)
    return m.start() if m else len(t)


def _extract_compaction_summary(text: str) -> Optional[str]:
    """从 Hermes 压缩包装（含普通与 merged 载体）中提取摘要正文。

    只对真实压缩载体生效（见 ``is_compaction_carrier``）。前导指令段以
    ``avoid repeating it:`` 结尾，正文在其后、可信的结束标记之前。
    """
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not is_compaction_carrier(t):
        return None

    # 优先定位 avoid repeating it:
    marker = _COMPACTION_BODY_MARK
    idx = t.lower().find(marker)
    body_start = (idx + len(marker)) if idx >= 0 else None
    if body_start is None:
        # 有首部却没有正文标记的情况不该发生（载体判定已要求两者），防御性回退
        return None

    body = t[body_start:_compaction_body_bounds(t, body_start)].strip()
    if not body:
        return None
    # 清洗掉 Hermes 写给主 Agent 的框架级行为指令，避免误导标题模型偏向尾部
    body = re.sub(r"(?i)historical only;\s*newer protected-tail messages after this summary win\.?", "", body)
    body = re.sub(r"(?i)respond ONLY to the latest user message.*?\n", "", body)
    return body.strip() if body.strip() else None


def unpack_system_wrapper(text: str) -> Optional[str]:
    """剥掉运行时元数据包装，取出同一消息里的真实用户内核。

    `[System note: … interrupted mid-run]` / `[System: … model has changed]` /
    `[System: … network error]` 这三类包装之后紧跟的正文就是用户原话（实测
    20 条 `model has changed` 带正文的全是人类原话，34 条 `interrupted
    mid-run` 里有 4 条 >40 字符的人类原话）。包装本身不含主题信息，但它后面
    的东西含——旧逻辑用 `startswith` 前缀匹配把整条消息连同真实请求一起丢掉。

    正文为空时返回 None，等价于「整条丢弃」，因此不需要按包装类型分流。
    """
    if not text:
        return None
    m = _UNPACK_SYSTEM_WRAPPER_RE.match(text.lstrip())
    if not m:
        return None
    payload = text.lstrip()[m.end():].strip()
    return payload or None


def strip_group_chat_envelope(text: str) -> Optional[str]:
    """剥掉群聊信封的首部与房间规则，只保留「New messages」段。

    群聊消息的信封占 39.5% 的字符（规则块 32.4% + 首部 6.9%），这些样板对
    标题没有主题信息，却以「用户意图」的形态进入 prompt。房间规则是固定的
    行为约束，不是这条会话在讲什么。
    """
    if not text:
        return None
    if not _GROUP_CHAT_RE.match(text.lstrip()):
        return None
    m = _GROUP_MESSAGES_RE.search(text)
    if not m:
        return None
    body = text[m.end():]
    r = _GROUP_RULES_RE.search(body)
    if r:
        body = body[:r.start()]
    body = body.strip()
    return body or None


def clean_captured_text(text: str) -> Optional[str]:
    """Remove Hermes handoff wrappers while preserving the real user turn.

    Context compaction and replay markers are persisted as ordinary user
    messages. A whole compaction handoff is not useful title evidence, but
    its final message after ``END OF CONTEXT SUMMARY`` is a real user turn and
    must be retained. An unfinished handoff is discarded rather than fed to
    the title model as if it were user intent.

    Runtime-metadata wrappers (interrupted mid-run / model changed / network
    error) are unwrapped: the wrapper itself carries no subject, but the text
    that follows it is the real request. An empty payload is discarded.
    """
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not t:
        return None

    # 运行时元数据包装：先解包，正文才是真实用户请求
    unpacked = unpack_system_wrapper(t)
    if unpacked is not None:
        t = unpacked
        if not t:
            return None
    else:
        # 群聊信封：剥掉首部与房间规则，只保留消息段
        stripped = strip_group_chat_envelope(t)
        if stripped is not None:
            t = stripped
            if not t:
                return None

    # 如果文本包含 context compaction 或 handoff 结束标记，提取结束标记之后的真实用户消息。
    # 只在「这条消息含压缩 handoff 结构」时才走这条分支：agent 讨论压缩机制时
    # 长篇引用同样的字符串，那些正文不是用户消息，必须整体丢弃而不是切一刀。
    if has_compaction_handoff(t):
        match = _HANDOFF_END_LINE_RE.search(t)
        if match is not None:
            t = t[match.end():].strip()
        else:
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
    - 尾池按索引跳过头池已取的节：同一节被注入两遍会让模型把同一段文字当成
      「开头和结尾各自说了同一件事」加权，比 opening/recent 重叠更直接
    - 首节单独超出预算时只切它；尾节永不切——半截尾节会伪造一个结尾
    - 预算足够吃下全部小节时直接返回，不再走尾池补满
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
    taken: Set[int] = set()
    for idx, section in enumerate(sections):
        if used >= head_budget:
            break
        room = head_budget - used
        if len(section) <= room:
            chosen.append(section)
            taken.add(idx)
            used += len(section)
        elif not chosen:
            chosen.append(section[:room].rstrip())
            taken.add(idx)
            used = head_budget
    if len(taken) == len(sections):
        return "\n\n".join(chosen)
    used_tail = 0
    tail: List[str] = []
    for idx in range(len(sections) - 1, -1, -1):
        if used_tail >= tail_budget:
            break
        if idx in taken:
            continue
        section = sections[idx]
        if len(section) <= tail_budget - used_tail:
            tail.insert(0, section)
            taken.add(idx)
            used_tail += len(section)
    if not tail:
        # 尾池装不下任何整节时，退回句子边界窗口，别把预算白白空着。
        # 窗口只能覆盖尚未选中的节：对整篇原文或剩余全文取首句，会把某个标题行
        # 粘到另一节的正文前面，拼出一个冒充小节的脏标题行（下游按 `## ` 解析
        # 就会把那段正文误判成该标题的内容）。取最后一个未选中的节，标题行与
        # 正文必然同源。
        remaining = [
            section for idx, section in enumerate(sections) if idx not in taken
        ]
        return "\n\n".join(chosen + [smart_preview(remaining[-1], tail_budget)])
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
    # 摘要永远不进入 opening、recent 或用户意图轨迹，而是作为独立的历史锚点
    # 恒常透传：压缩后可见历史再短，主线证据也不该被一句「继续」清空。
    # 旧的 `not saw_visible_opening` 门禁会在出现任意一条真实用户消息时把
    # 摘要整体丢弃（实测 225 个含载体会话里 141 个因此看不到摘要）。
    # 多轮压缩会产生多段载体，按时间顺序拼接而不是只取 summaries[0]——
    # 后一段是更新的状态，但它是在前一段的基础上推进的，丢掉前段会丢失
    # 任务的原始目标。
    earlier_summary = None
    if summaries:
        n = summary_chars or preview_chars
        if n > 0:
            merged = "\n\n".join(summaries)
            earlier_summary = summary_preview(merged, n) if len(merged) > n else merged
        else:
            earlier_summary = "\n\n".join(summaries)

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
