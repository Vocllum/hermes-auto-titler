"""插件开 vs 关 抽样对照：20 个会话，统一口径重新生成两个标题。

- 关闭列 = Hermes 原生 derived（复用上游 derive_title：首条用户消息第一行、
  48 字符词边界截断）——这就是插件不存在的世界
- 开启列 = 插件盲改命名（blind=True，不写库）

输出 Markdown 表格（四维评分列留空，供外部模型打分）：
主线准确度 / 可检索性 / 信息覆盖 / 简洁度（各 0-5 或 1-10，评分者自定）。

用法:
  ~/.hermes/hermes-agent/venv/bin/python scripts/ab_plugin_onoff.py
"""

import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.title_generator import derive_title  # noqa: E402
from hermes_state import SessionDB  # noqa: E402

from hermes_auto_titler.config import load_config  # noqa: E402
from hermes_auto_titler.messages import load_context, message_text  # noqa: E402
from hermes_auto_titler.titler import AutoTitler  # noqa: E402

N_SAMPLES = 20
MIN_MSGS = 10
SEED = 7
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ab_plugin_onoff.md"


class Ctx:
    def __init__(self):
        from agent.plugin_llm import PluginLlm

        self.llm = PluginLlm(plugin_id="hermes-auto-titler")


def first_user_message(db, sid):
    """会话第一条真实用户消息文本（上游 derive_title 会自行剥机器前缀）。"""
    conv = db.get_messages_as_conversation(sid, include_ancestors=True) or []
    for m in conv:
        if m.get("role") == "user":
            t = message_text(m.get("content")).strip()
            if t:
                return t
    return ""


def brief(db, sid):
    """评分用简报：开头 1 轮 + 最近 1 轮（≤150 字符/条），与插件输入同源。"""
    recent, _, opening = load_context(
        db, sid, recent_turns=1, include_all_user=False, opening_turns=1,
        preview_chars=150,
    )
    parts = []
    for role, text in opening:
        parts.append(f"{role}: {text}")
    if recent != opening:
        for role, text in recent:
            parts.append(f"{role}: {text}")
    return " / ".join(parts)[:500]


def main():
    db = SessionDB()
    cfg = load_config()
    titler = AutoTitler(Ctx(), cfg, db=db)

    rows = db.list_sessions_rich(limit=1000, min_message_count=MIN_MSGS, include_children=False)
    sids_env = os.environ.get("AB_SIDS", "")
    if sids_env:
        # 固定会话集（截断 sid 前缀，逗号分隔）：按给定顺序跑，保证跨版本逐条可比
        wanted = [s.strip() for s in sids_env.split(",") if s.strip()]
        sampled = [r for r in rows if any(r["id"].startswith(w) for w in wanted)]
        sampled.sort(key=lambda r: next(
            i for i, w in enumerate(wanted) if r["id"].startswith(w)))
        print(f"[fixed-sids] 匹配 {len(sampled)}/{len(wanted)} 个会话")
    else:
        rng = random.Random(SEED)
        sampled = rng.sample(rows, min(N_SAMPLES, len(rows)))
        sampled.sort(key=lambda r: r.get("message_count") or 0)

    lines = [
        "| # | 消息数 | 会话简报（开头+最近，≤500 字符） | 关闭：Hermes 原生 derived | "
        "开启：插件盲改命名 | 主线准确度 | 可检索性 | 信息覆盖 | 简洁度 |",
        "|---|--------|--------|--------|--------|:--:|:--:|:--:|:--:|",
    ]
    for i, row in enumerate(sampled, 1):
        sid = row["id"]
        n = row.get("message_count") or 0
        b = brief(db, sid)
        # 关闭组：上游原生 derived（首条用户消息）
        off = derive_title(first_user_message(db, sid)) or "(无)"
        # 开启组：插件盲改（不写库）
        recent, all_user, opening = load_context(
            db, sid, recent_turns=2, include_all_user=True, opening_turns=2,
            preview_chars=200,
        )
        try:
            action, title = titler._generate(None, recent, all_user, opening, blind=True)
        except Exception as e:
            action, title = "keep", f"(生成失败: {e})"
        on = title if action == "rename" and title else "(keep)"
        b = b.replace("|", "｜").replace("\n", " ")
        off = off.replace("|", "｜")
        on = on.replace("|", "｜")
        lines.append(f"| {i} | {n} | {b} | {off} | {on} |  |  |  |  |")
        print(f"[{i}/{len(sampled)}] {sid[:14]} msgs={n} off={off!r} on={on!r}")

    text = "\n".join(lines) + "\n"
    Path(OUT).write_text(text, encoding="utf-8")
    print(f"\n表格已写入 {OUT}")
    db.close()


if __name__ == "__main__":
    main()
