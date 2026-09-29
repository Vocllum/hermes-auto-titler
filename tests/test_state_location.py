from pathlib import Path
from types import SimpleNamespace

from hermes_auto_titler.config import DEFAULTS
from hermes_auto_titler import titler

AutoTitler = titler.AutoTitler


def _titler():
    return AutoTitler(
        SimpleNamespace(),
        {**DEFAULTS, "rename_confirmations": 0},
        db=object(),
    )


def _isolate_data_dir(monkeypatch):
    monkeypatch.setattr(
        titler,
        "plugin_data_dir",
        lambda name: Path(titler.get_hermes_home()) / "plugin-data" / name,
    )


def test_state_path_uses_plugin_data_outside_install_directory(monkeypatch):
    _isolate_data_dir(monkeypatch)
    path = titler.state_path()
    home = Path(titler.get_hermes_home())

    assert path.name == "state.json"
    assert path == home / "plugin-data" / "hermes-auto-titler" / "state.json"
    assert home / "plugins" / "hermes-auto-titler" not in path.parents


def test_legacy_state_migrates_once_without_overwriting_new_state(tmp_path, monkeypatch):
    _isolate_data_dir(monkeypatch)
    home = Path(titler.get_hermes_home())
    legacy = home / "plugins" / "hermes-auto-titler" / "state.json"
    current = home / "plugin-data" / "hermes-auto-titler" / "state.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"version":1,"sessions":{"legacy":{}}}\n', encoding="utf-8")

    _titler()

    assert current.read_text(encoding="utf-8") == '{"version":1,"sessions":{"legacy":{}}}\n'
    assert not legacy.exists()

    legacy.write_text('{"version":1,"sessions":{"stale":{}}}\n', encoding="utf-8")
    _titler()

    assert current.read_text(encoding="utf-8") == '{"version":1,"sessions":{"legacy":{}}}\n'
    assert legacy.read_text(encoding="utf-8") == '{"version":1,"sessions":{"stale":{}}}\n'


def test_persist_writes_only_to_plugin_data(monkeypatch):
    _isolate_data_dir(monkeypatch)
    instance = _titler()
    legacy = Path(titler.get_hermes_home()) / "plugins" / "hermes-auto-titler" / "state.json"

    instance._persist_state_locked()

    assert instance._state_path == titler.state_path()
    assert instance._state_path.is_file()
    assert not legacy.exists()
