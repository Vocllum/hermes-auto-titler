"""/autotitler slash command: status / config / rename-now / retitle-all."""

from __future__ import annotations

from typing import Any, Callable


def _set_config(titler, key: str, value: str) -> str:
    cfg = titler.cfg
    from .config import coerce_value, save_config

    if key not in cfg:
        return f"Unknown config key: {key}"
    try:
        v = coerce_value(key, value)
    except ValueError as e:
        return f"Invalid value: {value} ({e})"
    cfg[key] = v
    # model / title_model 是同一个设置的内部键与对外别名，写任一个都同步另一个
    if key == "model":
        cfg["title_model"] = v
    elif key == "title_model":
        cfg["model"] = v
    save_config(cfg)
    if key == "enabled":
        return (
            f"{key} = {v} (saved to config.yaml; hooks are selected at plugin "
            "load, so changing false to true requires a Hermes restart)"
        )
    if key == "first_title_mode":
        return (
            f"{key} = {v} (saved to config.yaml; startup-level setting that "
            "requires a Hermes restart; the host's own title generator is never "
            "modified by this plugin)"
        )
    return f"{key} = {v} (saved to config.yaml; effective immediately)"


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
            recent_logs = "\n".join(f"  {line}" for line in list(titler._audit_log)[-5:])
            log_section = f"\nrecent audits (last {min(5, len(titler._audit_log))}):\n{recent_logs}" if recent_logs else ""
            return (
                f"autotitler: {'enabled' if c['enabled'] else 'disabled'}"
                f" | every {c['every_n_turns']} turns | first_title={c.get('first_title_mode', 'builtin')}"
                f" | early_turn_eval={c['early_turn_eval']}"
                f" | on_close={c['on_close']}"
                f" | strategy={c['strategy']}"
                f" | provider={c['provider'] or '(host default)'}"
                f" | model={c['model'] or '(host default)'}"
                f" | interval={c['min_interval_minutes']}m | max_len={c['max_title_length']}"
                f" | max_renames={c.get('max_renames_per_session', 0)}"
                f" | retry_queue={len(titler._failed_sessions)}"
                f" | finalize_queue={len(titler._finalize_intents)}"
                f"{log_section}"
            )

        if cmd == "config":
            if len(args) >= 3:
                return _set_config(titler, args[1], args[2])
            if len(args) == 2:
                return f"{args[1]}: {titler.cfg.get(args[1], '(unknown key)')}"
            return "Usage: /autotitler config <key> [value]"

        if cmd == "rename-now":
            raw_arg = args[1] if len(args) > 1 else ""
            db = titler.db
            # 解析目标会话 ID：优先精准匹配或前缀匹配持久化 ID，否则回退当前活跃会话
            sid = None
            if raw_arg:
                sid = getattr(db, "resolve_session_id", lambda x: None)(raw_arg) or raw_arg
            if not sid:
                sid = titler._current_session
            if not sid:
                return (
                    "No active session is available. Specify one with: "
                    "/autotitler rename-now <session_id>"
                )
            # 手动重命名代表用户显式即时意图，必须旁路评审协议（blind=True），立即生成并落库
            r = titler.evaluate(sid, force=True, blind=True)
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
                lines.append(f"  ... {len(results)} sessions total")
            return "\n".join(lines)

        return (
            "Usage:\n"
            "  /autotitler status\n"
            "  /autotitler config <key> [value]\n"
            "  /autotitler rename-now [session_id]\n"
            "  /autotitler retitle-all [--dry-run] [--limit N] [--min-messages N]"
        )

    return handler
