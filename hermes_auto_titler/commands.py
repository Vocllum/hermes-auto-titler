"""/autotitler 斜杠命令：status / config / rename-now / retitle-all。"""

from __future__ import annotations

from typing import Any, Callable


def _set_config(titler, key: str, value: str) -> str:
    cfg = titler.cfg
    if key not in cfg:
        return f"未知配置键: {key}"
    try:
        if isinstance(cfg[key], bool):
            v = value.lower() in ("true", "1", "yes", "on")
        elif isinstance(cfg[key], int):
            v = int(value)
        elif isinstance(cfg[key], float):
            v = float(value)
        else:
            v = value
    except ValueError:
        return f"值无效: {value}"
    cfg[key] = v
    from .config import save_config

    save_config(cfg)
    return f"{key} = {v}（已写入 config.yaml，立即生效）"


def _fmt_result(r: dict[str, Any]) -> str:
    act = r.get("action", "?")
    extra = r.get("title") or r.get("reason") or ""
    return f"  {str(r.get('session_id', ''))[:8]} {act} {extra}".rstrip()


def make_handler(titler) -> Callable[[str], str]:
    def handler(raw: str) -> str:
        args = raw.split()
        cmd = args[0].lower() if args else "status"

        if cmd == "status":
            c = titler.cfg
            return (
                f"autotitler: {'enabled' if c['enabled'] else 'disabled'}"
                f" | every {c['every_n_turns']} turns | on_close={c['on_close']}"
                f" | recent {c['recent_turns']} turns | all_user_msgs={c['include_all_user_messages']}"
                f" | strategy={c['strategy']} | model={c['model'] or '(host default)'}"
                f" | interval={c['min_interval_minutes']}m | max_len={c['max_title_length']}"
            )

        if cmd == "config":
            if len(args) >= 3:
                return _set_config(titler, args[1], args[2])
            if len(args) == 2:
                return f"{args[1]}: {titler.cfg.get(args[1], '（未知键）')}"
            return "用法: /autotitler config <key> [value]"

        if cmd == "rename-now":
            sid = args[1] if len(args) > 1 else titler._current_session
            if not sid:
                return "没有可用的会话（最近未发生对话）。可指定: /autotitler rename-now <session_id>"
            r = titler.evaluate(sid, force=True)
            return _fmt_result({"session_id": sid, **r})

        if cmd == "retitle-all":
            dry = "--dry-run" in args
            limit = None
            min_messages = 0
            for i, a in enumerate(args):
                if a == "--limit" and i + 1 < len(args):
                    try:
                        limit = int(args[i + 1])
                    except ValueError:
                        pass
                if a == "--min-messages" and i + 1 < len(args):
                    try:
                        min_messages = int(args[i + 1])
                    except ValueError:
                        pass
            results = titler.retitle_all(dry_run=dry, limit=limit, min_messages=min_messages)
            renamed = [r for r in results if r.get("action") == "renamed"]
            lines = [
                f"retitle-all{' (dry-run)' if dry else ''}: "
                f"{len(renamed)}/{len(results)} renamed"
            ]
            lines += [_fmt_result(r) for r in results[:20]]
            if len(results) > 20:
                lines.append(f"  ... 共 {len(results)} 个会话")
            return "\n".join(lines)

        return (
            "用法:\n"
            "  /autotitler status\n"
            "  /autotitler config <key> [value]\n"
            "  /autotitler rename-now [session_id]\n"
            "  /autotitler retitle-all [--dry-run] [--limit N] [--min-messages N]"
        )

    return handler
