"""Hermes auto-titler — 会话标题自动维护插件。

on_session_end hook 按配置的轮数间隔评估当前会话标题是否需要更新，
用独立模型（默认宿主辅助模型，本地可配 deepseek-v4-flash）判断，
写回 Hermes session DB，遵守标题来源优先级（用户手改的永不覆盖）。
"""

from __future__ import annotations

from .config import load_config
from .titler import AutoTitler
from .commands import make_handler


def register(ctx) -> None:
    """Hermes 插件入口：注册 on_session_end hook + /autotitler 命令。"""
    cfg = load_config()
    titler = AutoTitler(ctx, cfg)
    if cfg.get("enabled", True):
        ctx.register_hook("on_session_end", titler.on_session_end)
    ctx.register_command(
        "autotitler",
        make_handler(titler),
        description="Hermes auto-titler：查看/修改自动标题配置，手动改名，批量重生成",
        args_hint="status|config|rename-now|retitle-all",
    )
