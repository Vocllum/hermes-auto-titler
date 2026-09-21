"""Hermes auto-titler — continuous session-title maintenance.

Periodic evaluation runs in a coalescing daemon worker. Close/finalize hooks do
not start network work: they briefly wait for an existing worker and persist a
typed, epoch-tagged finalize intent for a normal-lifecycle retry. SessionDB
writeback preserves title provenance and never overwrites user-authored titles.
"""

from __future__ import annotations

import logging

from .config import disable_builtin_title_generation, load_config
from . import titler as _titler
from . import policy as _policy
from .policy import AutoTitler
from .commands import make_handler

# Keep direct imports (`from hermes_auto_titler.titler import AutoTitler`) on the
# same policy-specialized class as the package/runtime entry point. Core lifecycle
# and DB plumbing remain in titler.py; prompt/review policy lives in policy.py.
_titler.AutoTitler = AutoTitler
# Existing diagnostics/tests consume the historical titler logger; policy logs
# remain on that channel even though the implementation is split across modules.
_policy.log = _titler.log

log = logging.getLogger(__name__)


def register(ctx) -> None:
    """Hermes 插件入口：注册 pre_llm_call/on_session_end/on_session_finalize hook + /autotitler 命令。"""
    cfg = load_config()
    titler = AutoTitler(ctx, cfg)
    if cfg.get("enabled", True):
        if cfg.get("first_title_mode", "plugin") == "plugin":
            try:
                changed = disable_builtin_title_generation()
                log.info(
                    "hermes-auto-titler takeover mode: built-in title generation %s",
                    "disabled" if changed else "already disabled",
                )
            except Exception:
                log.warning(
                    "hermes-auto-titler could not disable built-in title generation; "
                    "plugin takeover may race the host titler",
                    exc_info=True,
                )
        ctx.register_hook("pre_llm_call", titler.on_pre_llm_call)
        ctx.register_hook("on_session_end", titler.on_session_end)
        # Close/finalize only queues durable work; it never starts an LLM call.
        ctx.register_hook("on_session_finalize", titler.on_session_finalize)
        titler.restore_state()
        titler.start_retry_loop()
    ctx.register_command(
        "autotitler",
        make_handler(titler),
        description="Inspect/configure automatic titles, rename now, or retitle sessions",
        args_hint="status|config|rename-now|retitle-all",
    )
