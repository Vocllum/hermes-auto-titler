"""Hermes auto-titler — 会话标题自动维护插件。

on_session_end hook 按配置的轮数间隔评估当前会话标题是否需要更新，
评估在后台 daemon 线程执行（hook 立即返回，同一会话 in-flight 去重）；
真实会话关闭/终局走 on_session_finalize（正常节流）。判断模型独立于
主对话（默认宿主辅助模型，可配置任意可用模型），写回 Hermes
session DB，遵守标题来源优先级（用户手改的永不覆盖）。
"""

from __future__ import annotations

import logging

from .config import disable_builtin_title_generation, load_config
from . import titler as _titler
from .policy import AutoTitler
from .commands import make_handler

# Keep direct imports (`from hermes_auto_titler.titler import AutoTitler`) on the
# same policy-specialized class as the package/runtime entry point. Core lifecycle
# and DB plumbing remain in titler.py; prompt/review policy lives in policy.py.
_titler.AutoTitler = AutoTitler

log = logging.getLogger(__name__)


def register(ctx) -> None:
    """Hermes 插件入口：注册 on_session_end/on_session_finalize hook + /autotitler 命令。"""
    cfg = load_config()
    titler = AutoTitler(ctx, cfg)
    if cfg.get("enabled", True):
        if cfg.get("first_title_mode", "builtin") == "plugin":
            try:
                changed = disable_builtin_title_generation()
                log.info(
                    "hermes-auto-titler takeover mode: built-in title generation %s",
                    "disabled" if changed else "already disabled",
                )
            except Exception:
                log.warning(
                    "hermes-auto-titler could not disable built-in title generation; "
                    "plugin takeover may race the host titleer",
                    exc_info=True,
                )
        ctx.register_hook("on_session_end", titler.on_session_end)
        # 真实会话关闭/终局（CLI 退出、TUI 关闭、gateway 过期、/new）走 lifecycle
        # hook：正常节流（force=False），且不重复进行中的自动评估
        ctx.register_hook("on_session_finalize", titler.on_session_finalize)
    ctx.register_command(
        "autotitler",
        make_handler(titler),
        description="Hermes auto-titler：查看/修改自动标题配置，手动改名，批量重生成",
        args_hint="status|config|rename-now|retitle-all",
    )
