"""Hermes auto-titler — continuous session-title maintenance.

Periodic evaluation runs in a coalescing daemon worker. Close/finalize hooks do
not start network work: they briefly wait for an existing worker and persist a
typed, epoch-tagged finalize intent for a normal-lifecycle retry. SessionDB
writeback preserves title provenance and never overwrites user-authored titles.

0.3: the plugin never writes host configuration. First titles stay with Hermes
(``first_title_mode: builtin``), so disabling or deleting the plugin leaves the
host's native title pipeline untouched. ``plugin`` mode only changes which side
evaluates first; it still does not rewrite ``auxiliary.title_generation``.
"""

from __future__ import annotations

import logging

from .config import load_config
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


def _resolve_config(ctx) -> dict:
    """Load the effective config through the host ``ctx`` when it exposes it.

    ``load_config`` accepts the context as a keyword argument; hosts and
    stubs that provide a zero-argument loader still work, so plugin
    registration never depends on a particular ctx surface.
    """
    try:
        return load_config(ctx=ctx)
    except TypeError:
        return load_config()


def register(ctx) -> None:
    """Hermes 插件入口：注册 pre_llm_call/on_session_end/on_session_finalize hook + /autotitler 命令。"""
    cfg = _resolve_config(ctx)
    titler = AutoTitler(ctx, cfg)
    if cfg.get("enabled", True):
        mode = str(cfg.get("first_title_mode", "builtin")).lower()
        log.info(
            "hermes-auto-titler: coexistence mode (first_title_mode=%s); "
            "host title generation left untouched",
            mode,
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
