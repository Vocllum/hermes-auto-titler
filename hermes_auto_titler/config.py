"""配置加载：插件目录 config.yaml + 默认值，运行时可经 /autotitler config 修改。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "every_n_turns": 3,
    "on_close": True,
    "recent_turns": 2,
    "opening_turns": 2,
    "ignore_model_messages": False,
    "preview_chars": 100,
    "include_all_user_messages": True,
    "user_message_threshold": 40,
    "user_message_preview_chars": 300,
    "title_style": "concise",
    "strategy": "conservative",
    "model": "",
    "min_interval_minutes": 5,
    "max_title_length": 16,
    "max_display_width": 40,
}

VALID_STRATEGIES = {"conservative", "aggressive"}


def _default_config_path() -> Path:
    # 安装后 config.yaml 与包同级（symlink 或真实文件）
    return Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: Path | None = None) -> dict[str, Any]:
    """读取配置并合并默认值；缺失/损坏时回退默认值。"""
    cfg: dict[str, Any] = dict(DEFAULTS)
    p = path or _default_config_path()
    if p.is_file():
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if v is not None})
        except Exception:
            pass  # 坏配置回退默认值
    if cfg.get("strategy") not in VALID_STRATEGIES:
        cfg["strategy"] = "conservative"
    cfg["every_n_turns"] = max(1, int(cfg.get("every_n_turns", 3)))
    cfg["recent_turns"] = max(1, int(cfg.get("recent_turns", 2)))
    cfg["min_interval_minutes"] = max(0.0, float(cfg.get("min_interval_minutes", 5)))
    cfg["max_title_length"] = max(10, min(int(cfg.get("max_title_length", 80)), 100))
    cfg["max_display_width"] = max(10, min(int(cfg.get("max_display_width", 40)), 100))
    return cfg


def save_config(cfg: dict[str, Any], path: Path | None = None) -> None:
    """把当前配置写回 config.yaml（保留全部键，不带注释）。"""
    p = path or _default_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    dumped = yaml.safe_dump(
        {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS},
        allow_unicode=True,
        sort_keys=False,
    )
    p.write_text(dumped, encoding="utf-8")
