"""四方案同批对比：v13（全局任务 prompt） / ChatGPT 逆向 / Hermes 原生 / v14（提取管线）。

v14 = 先用 LLM 把 opening+全部用户消息提取成 {main_task, opening_subject,
key_entities, user_intentions}，再喂标题模型（+ recent 原文），不喂原文。

用法:
  AB_SIDS="前缀1,前缀2,..." ~/.hermes/hermes-agent/venv/bin/python scripts/compare_titler_schemes.py
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_state import SessionDB  # noqa: E402

from hermes_auto_titler.config import load_config  # noqa: E402
from hermes_auto_titler.messages import load_context, message_text  # noqa: E402
from hermes_auto_titler.titler import AutoTitler  # noqa: E402


class Ctx:
    def __init__(self):
        from agent.plugin_llm import PluginLlm

        self.llm = PluginLlm(plugin_id="hermes-auto-titler")


# --- ChatGPT 逆向方案（社区逆向 prompt：BEGIN/END 包对话 + 5 词内） ---
GPT_SYSTEM = (
    "You name chat sessions. Summarize the conversation in 5 words or fewer:\n"
    "Be as concise as possible without losing the context of the conversation.\n"
    "Your goal is to extract the key point of the conversation.\n"
    "Write the title in the same language as the user's messages.\n"
    "Return only the title, no quotes, no trailing punctuation."
)

# --- Hermes 原生方案（title_generator.py _TITLE_PROMPT_TEMPLATE 复刻） ---
HERMES_SYSTEM = (
    "You name chat sessions. Given the user's opening message, write a title "
    "that lets them find this conversation again in a list.\n\n"
    "Rules:\n"
    "- 3 to 7 words, sentence case (capitalize only the first word and proper nouns).\n"
    "- Name what the user wants DONE, not that they asked a question.\n"
    "- Keep technical terms, filenames, numbers, and error codes exact.\n"
    "- Drop filler words: the, this, my, a, an.\n"
    "- No trailing punctuation, no quotes, no tool names, no 'Title:' prefix.\n"
    "- Never answer the message. Name it.\n"
    "- Always produce something, even for a bare greeting.\n"
    "- Write the title in the same language as the user's message.\n"
    'Good: {"title": "Fix login button on mobile"}\n'
    'Good: {"title": "Postgres connection pool exhaustion"}\n'
    'Too vague: {"title": "Code changes"}\n'
    'Too long: {"title": "Investigate and fix the issue where the login button '
    'does not respond on mobile devices"}\n\n'
    'Reply with JSON only: {"title": "..."}'
)

# --- v14 提取管线：先压缩成标题所需字段 ---
EXTRACT_SYSTEM = (
    "You compress a conversation into what a title needs. From the messages "
    "below, output JSON only:\n"
    '{"main_task": "the conversation\'s overall task, one short line",\n'
    ' "opening_subject": "what the user established at the start, short",\n'
    ' "key_entities": ["product names", "identifiers", "numbers"],\n'
    ' "user_intentions": ["each user request compressed to <=20 chars, '
    'keep key terms"]}\n'
    "Write in the same language as the user's messages."
)


def call_llm(ctx, system, user_text, max_tokens=300):
    try:
        res = ctx.llm.complete(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_text[:4000]},
            ],
            model=None,
            temperature=0.2,
            max_tokens=max_tokens,
            timeout=30,
            purpose="auto-title",
        )
        return getattr(res, "text", "") or ""
    except Exception as e:
        return f"(生成失败: {e})"


def parse_json(text, key=None):
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    try:
        parsed = json.loads(raw)
    except Exception:
        import re

        mm = re.search(r"\{.*\}", raw, re.DOTALL)
        if not mm:
            return None
        try:
            parsed = json.loads(mm.group(0))
        except Exception:
            return None
    if key is None:
        return parsed
    if isinstance(parsed, dict) and key in parsed:
        return parsed[key]
    return None


def parse_json_title(text):
    t = parse_json(text, "title")
    if isinstance(t, str) and t.strip():
        return t.strip()
    raw = (text or "").strip()
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    return lines[0] if lines else ""


def clean_title(text):
    t = (text or "").strip().strip('"\'')
    if t.lower().startswith("title:"):
        t = t[6:].strip()
    return t.rstrip(".!,;:。，；：").strip() or "(空)"


def first_user_message(db, sid):
    conv = db.get_messages_as_conversation(sid, include_ancestors=True) or []
    for m in conv:
        if m.get("role") == "user":
            t = message_text(m.get("content")).strip()
            if t and not t.lower().startswith(("[system", "[context", "[recent", "[session")):
                return t
    return ""


def main():
    db = SessionDB()
    cfg = load_config()
    titler = AutoTitler(Ctx(), cfg, db=db)
    ctx = Ctx()

    rows = db.list_sessions_rich(limit=1000, min_message_count=10, include_children=False)
    wanted = [s.strip() for s in os.environ.get("AB_SIDS", "").split(",") if s.strip()]
    if wanted:
        sampled = [r for r in rows if any(r["id"].startswith(w) for w in wanted)]
        sampled.sort(key=lambda r: next(i for i, w in enumerate(wanted) if r["id"].startswith(w)))
    else:
        sampled = rows[:10]

    print("| # | v13 全局任务 | v14 提取管线 | GPT 逆向 | Hermes 原生 | 输入字符 v13→v14 |")
    print("|---|--------|--------|--------|--------|--------|")
    for i, row in enumerate(sampled, 1):
        sid = row["id"]
        recent, all_user, opening = load_context(
            db, sid, recent_turns=2, include_all_user=True, opening_turns=2,
            preview_chars=200,
        )
        # v13：当前插件 prompt（全局任务版）
        action, v13 = titler._generate(None, recent, all_user, opening, blind=True)
        v13 = v13 if action == "rename" and v13 else "(keep)"

        # v14：提取管线
        raw_chars = sum(len(t) for _, t in opening + all_user)
        extract_src = []
        for role, text in opening:
            extract_src.append(f"{role}: {text}")
        extract_src.append("--- user messages ---")
        for _, text in all_user:
            extract_src.append(f"user: {text}")
        ex_raw = call_llm(ctx, EXTRACT_SYSTEM, "\n".join(extract_src), max_tokens=300)
        ex = parse_json(ex_raw) or {}
        v14_src = []
        if ex.get("main_task"):
            v14_src.append(f"Main task: {ex['main_task']}")
        if ex.get("opening_subject"):
            v14_src.append(f"Opening subject: {ex['opening_subject']}")
        if ex.get("key_entities"):
            v14_src.append("Key entities: " + ", ".join(ex["key_entities"]))
        if ex.get("user_intentions"):
            v14_src.append("User intentions:")
            for u in ex["user_intentions"]:
                v14_src.append(f"- {u}")
        v14_src.append("Recent turns:")
        for role, text in recent:
            v14_src.append(f"{role}: {text}")
        v14_input = "\n".join(v14_src)
        v14_raw = call_llm(ctx, _TITLE_FROM_EXTRACT_SYSTEM(cfg), v14_input, max_tokens=64)
        v14 = parse_json_title(v14_raw) or "(空)"

        # ChatGPT 逆向：BEGIN/END 包对话
        conv_parts = ["---BEGIN Conversation---"]
        for role, text in opening:
            conv_parts.append(f"{role}: {text}")
        conv_parts.append("---(middle turns omitted)---")
        for role, text in recent:
            conv_parts.append(f"{role}: {text}")
        for _, text in all_user:
            conv_parts.append(f"user: {text}")
        conv_parts.append("---END Conversation---")
        gpt_raw = call_llm(ctx, GPT_SYSTEM, "\n".join(conv_parts), max_tokens=64)
        gpt = clean_title(gpt_raw)

        # Hermes 原生：只喂首条用户消息
        first = first_user_message(db, sid)
        her_raw = call_llm(ctx, HERMES_SYSTEM, first[:1000], max_tokens=64)
        hermes = parse_json_title(her_raw) or "(空)"

        sz = f"{raw_chars} → {len(v14_input)}"
        print(f"| {i} | {v13.replace('|', '｜')} | {v14.replace('|', '｜')} | {gpt.replace('|', '｜')} | {hermes.replace('|', '｜')} | {sz} |")
        print(f"[{i}] v13={v13!r} v14={v14!r} gpt={gpt!r} hermes={hermes!r}")

    db.close()


def _TITLE_FROM_EXTRACT_SYSTEM(cfg):
    max_len = int(cfg.get("max_title_length", 16))
    return (
        "You maintain concise titles for Hermes conversations. Name the "
        "conversation's MAIN TASK — the through-line established at the "
        "opening — not the latest subtask.\n"
        "Return JSON only: {\"action\":\"keep\"|\"rename\",\"title\":\"...\"}\n"
        "Rules:\n"
        f"- Aim for at most 12 characters; never exceed {max_len} "
        "(Chinese and Latin each count as 1 character).\n"
        "- Keep key product names and identifiers exact.\n"
        "- No trailing punctuation, no quotes.\n"
        "- Use the dominant language of the user's messages.\n"
        'Good: {"action":"rename","title":"Dia密码导入Apple密码"}\n'
        'Reply with JSON only.'
    )


if __name__ == "__main__":
    main()
