"""End-to-end config plumbing checks against the real Hermes host parsers.

Run with the host interpreter:

    /Users/Vocllum/.hermes/hermes-agent/venv/bin/python scripts/verify_config_plumbing.py

Exercises: manifest config_schema declaration -> host settings fields ->
ctx.get_config -> effective plugin config, the legacy config.yaml fallback,
per-key precedence, and the removal of every host-config writer.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

PLUGIN_DIR = Path(__file__).resolve().parents[1]
HOST_DIR = Path("/Users/Vocllum/.hermes/hermes-agent")

sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(HOST_DIR))

from hermes_cli.plugins_manifest import (  # noqa: E402
    parse_manifest_file,
    validate_config_schema,
)
from hermes_cli.plugins_settings import plugin_settings_fields  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "at_config", PLUGIN_DIR / "hermes_auto_titler" / "config.py"
)
if _spec is None or _spec.loader is None:
    raise SystemExit("cannot load plugin config module")
cfg_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cfg_mod)

FAILURES: list[str] = []


def _unsavable_keys(schema: Mapping) -> list[str]:
    """Declared keys the host would refuse to write, found without writing.

    ``save_plugin_settings`` validates ``_plugin_relative_segments(key)``
    before touching disk, and the reserved-root/format rules there are the
    same ones ``plugin_settings_fields`` uses to drop a field. Reusing the
    validator keeps this check read-only: a manifest key that cannot be
    saved is reported instead of silently vanishing from the panel.
    """
    try:
        from hermes_cli.plugins_state import _plugin_relative_segments
    except ImportError:
        return []
    bad: list[str] = []
    for key in schema:
        try:
            _plugin_relative_segments(str(key))
        except Exception:
            bad.append(str(key))
    return bad


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(label)


def main() -> int:
    manifest = parse_manifest_file(
        PLUGIN_DIR / "plugin.yaml", PLUGIN_DIR, "local", "hermes-auto-titler"
    )
    schema = manifest.config_schema or {}

    # 1. manifest declares v2 + a schema for every plugin-side default key.
    check("manifest_version == 2", manifest.manifest_version == 2)
    check("version is 0.3.0", manifest.version == "0.3.0")
    plugin_defaults = dict(cfg_mod.DEFAULTS)
    # `model` is the internal name of the setting declared externally as
    # `title_model`: the host reserves the bare `model` settings root, so it
    # must not appear in config_schema. The alias pair is the one allowed
    # divergence between DEFAULTS and the declared schema.
    alias_pairs = {"model": "title_model"}
    internal_only = {k for k in plugin_defaults if k in alias_pairs}
    check(
        "schema covers every externally-settable DEFAULTS key",
        (set(plugin_defaults) - internal_only) <= set(schema),
        f"missing={sorted((set(plugin_defaults) - internal_only) - set(schema))}",
    )
    check(
        "every aliased internal key has its external declaration",
        all(alias_pairs[k] in schema for k in internal_only),
        f"missing={[alias_pairs[k] for k in internal_only if alias_pairs[k] not in schema]}",
    )
    check("no schema key is unknown to DEFAULTS", set(schema) <= set(plugin_defaults))
    check(
        "validate_config_schema reports nothing",
        validate_config_schema("hermes-auto-titler", schema, {}) == [],
    )

    # 2. first_title_mode defaults to the non-invasive builtin mode.
    check(
        "schema default first_title_mode == builtin",
        schema["first_title_mode"]["default"] == "builtin",
    )
    check(
        "DEFAULTS first_title_mode == builtin",
        plugin_defaults["first_title_mode"] == "builtin",
    )

    # 3. Desktop/CLI settings surface renders, and every declared key is
    #    actually reachable: the host silently drops keys it refuses to save
    #    (e.g. a reserved settings root such as "model"), which would make a
    #    declared field invisible in the panel and unwritable from the CLI.
    fields = plugin_settings_fields("hermes-auto-titler", PLUGIN_DIR)
    rendered = {f.get("key") for f in fields}
    check("settings fields rendered", len(fields) >= 20, f"{len(fields)} fields")
    check(
        "every schema key is renderable by the host",
        set(schema) == rendered,
        f"dropped={sorted(set(schema) - rendered)}",
    )
    check(
        "every schema key is savable by the host",
        _unsavable_keys(schema) == [],
        f"unsavable={_unsavable_keys(schema)}",
    )
    ftm = [f for f in fields if f.get("key") == "first_title_mode"]
    check("first_title_mode rendered as enum", bool(ftm) and ftm[0]["type"] == "enum")
    check(
        "rendered default is builtin",
        bool(ftm) and ftm[0].get("default") == "builtin",
        repr(ftm[0].get("default")) if ftm else "field missing",
    )

    # 4. ctx.get_config wins over everything else.
    ctx = SimpleNamespace(
        get_config=lambda key, default=None: {
            "every_n_turns": 7,
            "first_title_mode": "plugin",
        }.get(key, default)
    )
    eff = cfg_mod.load_config(path=PLUGIN_DIR / "config.yaml", ctx=ctx)
    check("ctx value wins (every_n_turns=7)", eff["every_n_turns"] == 7)
    check("ctx value wins (first_title_mode=plugin)", eff["first_title_mode"] == "plugin")

    # 5. local config.yaml is the fallback below ctx.
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "config.yaml"
        p.write_text("every_n_turns: 5\nrecent_turns: 4\n", encoding="utf-8")
        eff2 = cfg_mod.load_config(path=p, ctx=ctx)
        check("ctx beats config.yaml", eff2["every_n_turns"] == 7)
        check(
            "config.yaml applies where ctx is silent",
            eff2["recent_turns"] == 4,
            f"got {eff2['recent_turns']}",
        )

    # 6. A zero-arg loader stub still registers (no ctx dependency).
    import hermes_auto_titler as entry
    from hermes_auto_titler import config as entry_cfg

    real_loader = entry_cfg.load_config
    entry_cfg.load_config = lambda: {**entry_cfg.DEFAULTS, "enabled": True}
    registered: list[str] = []
    stub_ctx = SimpleNamespace(
        register_hook=lambda *a, **k: registered.append("hook"),
        register_command=lambda *a, **k: registered.append("command"),
        llm=SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(text="{}")),
    )
    entry.register(stub_ctx)
    entry_cfg.load_config = real_loader
    check(
        "register works with zero-arg loader",
        registered.count("hook") == 3 and registered.count("command") == 1,
        repr(registered),
    )

    # 7. No host-config writer survives anywhere in the package.
    banned = (
        "disable_builtin_title_generation",
        "enable_builtin_title_generation",
    )
    offenders: list[str] = []
    for py in (PLUGIN_DIR / "hermes_auto_titler").glob("*.py"):
        text = py.read_text(encoding="utf-8")
        for name in banned:
            if f"def {name}" in text:
                offenders.append(f"{py.name}:{name}")
    check("no host-config writer function defined", not offenders, str(offenders))
    check(
        "config module exposes no host writer",
        not hasattr(cfg_mod, "disable_builtin_title_generation"),
    )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("all config plumbing checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
