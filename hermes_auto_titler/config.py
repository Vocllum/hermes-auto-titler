"""配置加载：插件目录 config.yaml + 默认值，运行时可经 /autotitler config 修改。"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "every_n_turns": 4,
    "on_close": True,
    "early_turn_eval": False,
    # 首轮命名一键开关：builtin=插件首轮不抢（early 强制失效，首标题归内建；
    # 正常轮次/关闭评估不受影响）；plugin=插件第 1 轮就接管（等价 early_turn_eval=true）。
    "first_title_mode": "builtin",
    "recent_turns": 2,
    "opening_turns": 2,
    "ignore_model_messages": False,
    "preview_chars": 100,
    "include_all_user_messages": True,
    "user_message_threshold": 40,
    "user_message_preview_chars": 300,
    # 压缩摘要进入标题评估的截断长度（日常评估用；盲改走 retitle_summary_chars）。
    # 100 字符只够摘要标题行，中文主线常被截掉导致模型无法锚定主题。
    "summary_preview_chars": 1200,
    "retitle_summary_chars": 12000,
    "title_style": "concise",
    "strategy": "conservative",
    "provider": "",
    "model": "",
    "min_interval_minutes": 5,
    # 12 字符仍是提示词软目标；24 字符硬上限可完整容纳
    # hermes-auto-titler 这类较长的字面标识符及少量中文意图。
    "max_title_length": 24,
    "max_display_width": 40,
    # 评审协议：>1 时 llm→llm 改名候选先挂起，下一次评估裁决（approve=落库 /
    # rename=换新候选 / keep=放弃；原样重复候选视为背书）。derived/无标题升级
    # 与 blind 终局评估旁路。1 = 关闭（单次评估直接改名），上限 5。
    "rename_confirmations": 1,
    # 单会话每小时实际改名次数上限（滑动 60 分钟窗口）；0 = 不设上限。
    "renames_per_hour": 0,
}

VALID_STRATEGIES = {"conservative", "aggressive"}
VALID_STYLES = {"concise", "complete"}
VALID_FIRST_TITLE_MODES = {"builtin", "plugin"}

_TRUE_STRINGS = {"true", "1", "yes", "on"}
_FALSE_STRINGS = {"false", "0", "no", "off"}


def _coerce_bool(raw: Any) -> bool:
    """严格布尔归一化：真布尔 / 0|1 / true|1|yes|on / false|0|no|off。

    数字只接受 0/1（0.0/1.0 亦视为 0/1）：2、-1、0.5 等一律拒绝，绝不把
    任意数值静默当成 True/False。其余字符串（如 "maybe"）抛 ValueError。
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        if raw in (0, 1):
            return bool(raw)
        raise ValueError(f"numeric boolean must be 0 or 1, got {raw!r}")
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in _TRUE_STRINGS:
            return True
        if s in _FALSE_STRINGS:
            return False
    raise ValueError(f"not a boolean: {raw!r}")


def coerce_value(key: str, raw: Any) -> Any:
    """单个配置键的类型 + 边界归一化；非法类型/枚举抛 ValueError。

    load_config（YAML，非法值回退默认）与 /autotitler config（非法值明确
    报错）共用这一条路径，保证两种入口行为一致。
    """
    if key not in DEFAULTS:
        raise ValueError(f"unknown config key: {key!r}")
    default = DEFAULTS[key]
    try:
        if isinstance(default, bool):
            v = _coerce_bool(raw)
        elif key == "min_interval_minutes":
            v = float(raw)
            if not math.isfinite(v):
                raise ValueError(f"not a finite number: {raw!r}")
        elif key == "model":
            if not isinstance(raw, str):
                raise ValueError(f"not a string: {raw!r}")
            v = raw
        elif key == "provider":
            if not isinstance(raw, str):
                raise ValueError(f"not a string: {raw!r}")
            v = raw
        elif isinstance(default, int):
            if isinstance(raw, bool):  # YAML true/false 不是合法整数
                raise ValueError(f"not an integer: {raw!r}")
            if isinstance(raw, float) and (
                not math.isfinite(raw) or not raw.is_integer()
            ):
                raise ValueError(f"not an integer: {raw!r}")
            v = int(raw)
        elif key == "strategy":
            if raw not in VALID_STRATEGIES:
                raise ValueError(
                    f"invalid strategy {raw!r} (expected one of {sorted(VALID_STRATEGIES)})"
                )
            v = raw
        elif key == "title_style":
            if raw not in VALID_STYLES:
                raise ValueError(
                    f"invalid title_style {raw!r} (expected one of {sorted(VALID_STYLES)})"
                )
            v = raw
        elif key == "first_title_mode":
            if raw not in VALID_FIRST_TITLE_MODES:
                raise ValueError(
                    f"invalid first_title_mode {raw!r} (expected one of {sorted(VALID_FIRST_TITLE_MODES)})"
                )
            v = raw
        else:
            v = raw
    except (TypeError, ValueError) as e:
        raise ValueError(f"{key}: {raw!r} is invalid ({e})") from e
    # 边界钳制（与历史 load_config 行为一致）
    if key in ("every_n_turns", "recent_turns", "opening_turns"):
        v = max(1, int(v))
    elif key == "min_interval_minutes":
        v = max(0.0, float(v))
    elif key in (
        "preview_chars",
        "user_message_threshold",
        "user_message_preview_chars",
        "retitle_summary_chars",
        "summary_preview_chars",
    ):
        v = max(0, int(v))
    elif key == "max_title_length":
        v = max(10, min(int(v), 100))
    elif key == "max_display_width":
        v = max(10, min(int(v), 100))
    elif key == "rename_confirmations":
        v = max(1, min(int(v), 5))
    elif key == "renames_per_hour":
        v = max(0, int(v))
    return v


def _default_config_path() -> Path:
    # 安装后 config.yaml 与包同级（symlink 或真实文件）。
    # 注意用 absolute() 而非 resolve()：resolve 会穿透包目录的符号链接，
    # 使 symlink 手动安装时配置路径漂移到 git clone 根，插件目录里的
    # config.yaml 被忽略。absolute() 保留 symlink 视角，两种安装布局一致。
    return Path(__file__).absolute().parent.parent / "config.yaml"


def load_config(path: Path | None = None) -> dict[str, Any]:
    """读取配置并合并默认值；缺失/损坏/非法值回退默认值，不崩溃。"""
    cfg: dict[str, Any] = dict(DEFAULTS)
    p = path or _default_config_path()
    data: Any = {}
    if p.is_file():
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            log.warning("auto-titler config unreadable at %s, using defaults", p)
            data = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if v is None or k not in DEFAULTS:
                continue
            try:
                cfg[k] = coerce_value(k, v)
            except ValueError:
                log.warning(
                    "auto-titler config: invalid %s=%r, using default %r",
                    k, v, DEFAULTS[k],
                )
                cfg[k] = DEFAULTS[k]
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


def disable_builtin_title_generation() -> bool:
    """Disable Hermes' competing first-title generator for ``first_title_mode=plugin``.

    The plugin's takeover mode promises that the plugin owns the first title.  Leaving
    the host task enabled makes both generators race, and the host's short first-message
    title can win before the plugin sees the full conversation.  Use Hermes' own config
    loader/saver so profile paths, locking, and atomic writes remain host-owned.

    Returns ``True`` when this call changed the host config, ``False`` when it was already
    disabled.  Callers should catch failures: an unavailable config writer must not stop
    the title plugin from loading.
    """
    from hermes_cli.config import load_config as load_host_config, save_config as save_host_config

    host_cfg = load_host_config() or {}
    auxiliary = host_cfg.setdefault("auxiliary", {})
    if not isinstance(auxiliary, dict):
        auxiliary = {}
        host_cfg["auxiliary"] = auxiliary
    title_generation = auxiliary.setdefault("title_generation", {})
    if not isinstance(title_generation, dict):
        title_generation = {}
        auxiliary["title_generation"] = title_generation
    if title_generation.get("enabled") is False:
        return False
    title_generation["enabled"] = False
    save_host_config(host_cfg)
    return True
