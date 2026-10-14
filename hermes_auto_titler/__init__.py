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
from .titler import migrate_legacy_state
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


def _bind_background_secret_scope():
    """Bind the owning profile's secret scope for plugin-owned background work.

    Under ``gateway.multiplex_profiles`` the host fails ``get_secret()`` closed
    (``UnscopedSecretError``) for any task that carries no profile scope, and
    plugin *registration* is not one of the host's scoped seams (per-turn and
    per-adapter work is). The titler's retry/sweep thread is created from here,
    so without a bound scope every sweep LLM call dies and its sessions are
    parked as ``blocked`` instead of being retitled.

    The scope is a contextvar and the thread start snapshots it through
    ``copy_context()`` (``propagate_context_to_thread`` snapshots at call time),
    so resetting it right after :meth:`AutoTitler.start_retry_loop` is safe.
    Returns the reset token, or ``None`` when multiplexing is off, a scope is
    already bound, or the host API is absent (older hosts behave as before).
    """
    try:
        from agent.secret_scope import (
            build_profile_secret_scope,
            current_secret_scope,
            is_multiplex_active,
            set_secret_scope,
        )
        from hermes_constants import get_hermes_home
    except Exception:  # older host without the scope API
        return None
    try:
        if not is_multiplex_active() or current_secret_scope() is not None:
            return None
        home = get_hermes_home()
        return set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
    except Exception:
        log.debug("hermes-auto-titler: could not bind the profile secret scope", exc_info=True)
        return None


def _reset_background_secret_scope(token) -> None:
    """Unbind a scope installed by :func:`_bind_background_secret_scope` (never raises)."""
    if token is None:
        return
    try:
        from agent.secret_scope import reset_secret_scope

        reset_secret_scope(token)
    except Exception:
        log.debug("hermes-auto-titler: could not reset the profile secret scope", exc_info=True)


def register(ctx) -> None:
    """Hermes 插件入口：注册 pre_llm_call/on_session_end/on_session_finalize hook + /autotitler 命令。"""
    cfg = _resolve_config(ctx)
    if cfg.get("enabled", True):
        migrate_legacy_state()
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
        # The retry/sweep thread is created here and is not covered by the host's
        # per-turn scope: bind the profile secret scope for its context snapshot.
        _scope_token = _bind_background_secret_scope()
        try:
            titler.start_retry_loop()
        finally:
            _reset_background_secret_scope(_scope_token)
    ctx.register_command(
        "autotitler",
        make_handler(titler),
        description="Inspect/configure automatic titles, rename now, or retitle sessions",
        args_hint="status|config|rename-now|retitle-all",
    )
