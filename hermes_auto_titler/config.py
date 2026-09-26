"""配置解析：宿主 config_schema 声明优先，插件目录 config.yaml 兜底。

单一归一化路径（coerce_value）同时服务三个入口：config.yaml 加载、
/autotitler config 运行期修改、以及宿主 plugins.entries.<id>.settings
的类型校验。非法值在 YAML 入口回退默认值，在命令入口明确报错。

0.3 起插件不再改写任何宿主配置：此前按 first_title_mode=plugin 在加载期
写死 auxiliary.title_generation.enabled=false 的接管逻辑已移除，插件目录
被删除或停用时宿主原生标题链路零残留。
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "every_n_turns": 2,
    "on_close": True,
    # 首轮命名权：builtin（默认）=首轮归宿主毫秒级起名，插件不抢首轮；
    # plugin=插件首轮即评估。两种模式都不写宿主配置（0.3 非侵入约定）。
    "first_title_mode": "builtin",
    "recent_turns": 2,
    "opening_turns": 2,
    "ignore_model_messages": False,
    # 每条消息预览上限：120 字符（约 1~2 句话），Assistant 与 User 消息对称精炼
    "preview_chars": 120,
    "include_all_user_messages": True,
    "user_message_threshold": 40,
    "user_message_preview_chars": 300,
    # 压缩摘要进入标题评估的截断长度（日常评估用；盲改走 retitle_summary_chars）。
    # 1200 字符足以覆盖 Goal + 核心 Constraints，无需更长。
    "summary_preview_chars": 1200,
    "retitle_summary_chars": 12000,
    "title_style": "concise",
    "strategy": "conservative",
    "provider": "",
    "model": "",
    # 宿主 settings 的对外键名。宿主保留 `model` 作为 settings 根
    # （_PLUGIN_SETTING_RESERVED_ROOTS），ctx.get_config("model") 与
    # save_plugin_setting 都会拒绝该键，因此对外声明为 title_model，
    # 读入后归一化回内部的 model 键。
    "title_model": "",
    "min_interval_minutes": 5,
    # None 表示不强加代码层字符数硬截断（由提示词与显示列宽约束）。
    "max_title_length": None,
    "max_display_width": 40,
    # 0 = 单次判定直接写；N>0 = 首次提出候选后，再要求 N 次后续背书。
    # 默认 1：降低单次误判导致的标题跳动；需要更快响应可显式改回 0。
    "rename_confirmations": 1,
    # 0 = 不限制；N>0 = 每个插件进程内的会话最多自动替换 N 次。
    # 手动 blind 重生成不受此门控，首次无标题生成也不消耗次数。
    "max_renames_per_session": 0,
    # 可选的自定义提示词片段，追加到 system prompt 末尾。留空则无影响。
    # 可用于注入个人偏好，如 "标题使用英文" 或 "永远包含项目名前缀"。
    "custom_instructions": "",
}

VALID_STRATEGIES = {"conservative", "aggressive"}
VALID_STYLES = {"concise", "complete"}
VALID_FIRST_TITLE_MODES = {"builtin", "plugin"}

_TRUE_STRINGS = {"true", "1", "yes", "on"}
_FALSE_STRINGS = {"false", "0", "no", "off"}

# config.yaml 里显式出现才算用户覆盖的键。宿主 settings 与 DEFAULTS
# 一致时不产生歧义；只有本地文件写过的键才走 load_config 的兜底合并。
_LOCAL_FILE_KEYS: frozenset[str] = frozenset(DEFAULTS)


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
        elif key in ("title_model",):
            if not isinstance(raw, str):
                raise ValueError(f"not a string: {raw!r}")
            v = raw
        elif key == "provider":
            if not isinstance(raw, str):
                raise ValueError(f"not a string: {raw!r}")
            v = raw
        elif key == "custom_instructions":
            v = str(raw) if raw is not None else ""
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
        if v is None or str(v).strip().lower() in ("", "none", "null", "0"):
            v = None
        else:
            v = max(10, min(int(v), 100))
    elif key == "max_display_width":
        v = max(10, min(int(v), 100))
    elif key == "rename_confirmations":
        v = max(0, int(v))
    elif key == "max_renames_per_session":
        v = max(0, int(v))
    return v


def _default_config_path() -> Path:
    # 安装后 config.yaml 与包同级（symlink 或真实文件）。
    # 注意用 absolute() 而非 resolve()：resolve 会穿透包目录的符号链接，
    # 使 symlink 手动安装时配置路径漂移到 git clone 根，插件目录里的
    # config.yaml 被忽略。absolute() 保留 symlink 视角，两种安装布局一致。
    return Path(__file__).absolute().parent.parent / "config.yaml"


def _host_settings() -> dict[str, Any]:
    """Read the host-declared settings via ``ctx.get_config``-equivalent reads.

    Returns ``{}`` when the plugin runs outside a host (tests, standalone
    scripts) or when no ``plugins.entries.hermes-auto-titler.settings``
    subtree exists. Never raises: a config source that cannot be read is
    simply not consulted.
    """
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.plugins_state import _plugin_settings_entry, _nested_plugin_value
    except Exception:
        return {}
    try:
        root = load_config_readonly() or {}
        entry = _plugin_settings_entry(root, "hermes-auto-titler") or {}
        raw = entry.get("settings")
        if not isinstance(raw, dict):
            raw = entry.get("config")  # migration fallback mirroring ctx.get_config
        if not isinstance(raw, dict):
            return {}
        return {
            k: v
            for k, v in raw.items()
            if k in _LOCAL_FILE_KEYS and v is not None
        }
    except Exception:
        return {}


def load_config(path: Path | None = None, *, ctx: Any = None) -> dict[str, Any]:
    """解析生效配置，按键取最先显式设置它的来源。

    逐键优先级：``ctx.get_config()``（插件运行期权威通道）> 宿主
    ``plugins.entries.hermes-auto-titler.settings``（Desktop/CLI 面板）
    > 插件目录 ``config.yaml``（0.2 存量配置，显式写过的键继续生效）
    > ``DEFAULTS``。按键而非按整份合并，是为了让存量 config.yaml 里
    调好的键不被宿主默认值静默推翻。

    某个来源给出的值无法归一化时，该来源视为未设置，继续向下一级回退：
    上层配置写错不该把下层的有效值一起废掉。
    """
    cfg: dict[str, Any] = dict(DEFAULTS)
    explicit: set[str] = set()

    def _apply(key: str, raw: Any, source: str) -> None:
        try:
            cfg[key] = coerce_value(key, raw)
        except ValueError:
            log.warning(
                "auto-titler config: %s setting %s=%r invalid, trying next source",
                source, key, raw,
            )
            return
        explicit.add(key)

    if ctx is not None:
        for key in DEFAULTS:
            try:
                value = ctx.get_config(key, None)
            except Exception:
                value = None
            if value is None:
                continue
            _apply(key, value, "host ctx")
    for key, value in _host_settings().items():
        _apply(key, value, "host settings")
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
            if v is None:
                continue
            if k not in DEFAULTS:
                log.warning("auto-titler config: unknown key %r ignored", k)
                continue
            if k in explicit:
                continue
            _apply(k, v, "config.yaml")
    # 对外键 title_model 归一化回内部 model 键。ctx 通道读不到 model
    # （宿主保留该 settings 根），所以这里只接受 title_model 与 config.yaml
    # 的 model 两种写法；显式的内部 model 优先，避免 title_model 覆盖它。
    if cfg.get("title_model") and not cfg.get("model"):
        cfg["model"] = cfg["title_model"]
    return cfg


def save_config(cfg: dict[str, Any], path: Path | None = None) -> None:
    """把当前配置写回 config.yaml（保留全部键，不带注释）。

    ``/autotitler config`` 走本地文件：宿主 settings 只在 Desktop/CLI
    的 Plugins 面板里改，两者不共享同一份权威源，本地写入不会覆盖宿主
    已声明的默认值语义。

    ``title_model`` 是对外别名，落盘时镜像成内部 ``model`` 键，避免同一
    个设置在文件里出现两份。
    """
    p = path or _default_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    if payload.get("title_model"):
        payload["model"] = payload["title_model"]
    payload.pop("title_model", None)
    dumped = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    p.write_text(dumped, encoding="utf-8")
