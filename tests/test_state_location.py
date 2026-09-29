import errno
import json
import os
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


def test_sandbox_constructor_does_not_migrate_profile_state(monkeypatch):
    _isolate_data_dir(monkeypatch)
    home = Path(titler.get_hermes_home())
    legacy = home / "plugins" / "hermes-auto-titler" / "state.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"version":1,"sessions":{}}', encoding="utf-8")

    instance = _titler()
    instance._state_path = home / "sandbox" / "state.json"

    assert legacy.exists()
    assert not titler.state_path().exists()


def test_legacy_state_migrates_once_without_overwriting_new_state(tmp_path, monkeypatch):
    _isolate_data_dir(monkeypatch)
    home = Path(titler.get_hermes_home())
    legacy = home / "plugins" / "hermes-auto-titler" / "state.json"
    current = home / "plugin-data" / "hermes-auto-titler" / "state.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"version":1,"sessions":{"legacy":{}}}\n', encoding="utf-8")

    titler.migrate_legacy_state()

    assert current.read_text(encoding="utf-8") == '{"version":1,"sessions":{"legacy":{}}}\n'
    assert not legacy.exists()

    legacy.write_text('{"version":1,"sessions":{"stale":{}}}\n', encoding="utf-8")
    titler.migrate_legacy_state()

    assert set(json.loads(current.read_text())["sessions"]) == {"legacy", "stale"}
    assert legacy.read_text(encoding="utf-8") == '{"version":1,"sessions":{"stale":{}}}\n'


def test_persist_writes_only_to_plugin_data(monkeypatch):
    _isolate_data_dir(monkeypatch)
    instance = _titler()
    legacy = Path(titler.get_hermes_home()) / "plugins" / "hermes-auto-titler" / "state.json"

    instance._persist_state_locked()

    assert instance._state_path == titler.state_path()
    assert instance._state_path.is_file()
    assert not legacy.exists()


def test_cross_device_copy_migrates_state_and_removes_legacy(monkeypatch):
    _isolate_data_dir(monkeypatch)
    home = Path(titler.get_hermes_home())
    legacy = home / "plugins" / "hermes-auto-titler" / "state.json"
    current = home / "plugin-data" / "hermes-auto-titler" / "state.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"version":1,"sessions":{"legacy":{}}}\n', encoding="utf-8")

    def cross_device(_source, _destination):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(titler.os, "link", cross_device)
    titler.migrate_legacy_state()

    assert current.read_text(encoding="utf-8") == '{"version":1,"sessions":{"legacy":{}}}\n'
    assert not legacy.exists()


def test_cross_device_copy_is_invisible_until_complete(monkeypatch):
    _isolate_data_dir(monkeypatch)
    home = Path(titler.get_hermes_home())
    legacy = home / "plugins/hermes-auto-titler/state.json"
    current = titler.state_path()
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"version":1,"sessions":{}}', encoding="utf-8")
    def fail_link(*_):
        raise OSError(errno.EXDEV, "cross-device")

    monkeypatch.setattr(titler.os, "link", fail_link)

    def interrupted_copy(source, target):
        target.write(b'{"version":')
        target.flush()
        assert not current.exists()
        raise OSError(errno.EIO, "interrupted")

    monkeypatch.setattr(titler.shutil, "copyfileobj", interrupted_copy)
    titler.migrate_legacy_state()
    assert not current.exists()
    assert legacy.exists()


def test_fallback_when_hard_links_unsupported(monkeypatch):
    _isolate_data_dir(monkeypatch)
    home = Path(titler.get_hermes_home())
    legacy = home / "plugins/hermes-auto-titler/state.json"
    current = titler.state_path()
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"version":1,"sessions":{"unsupported":{}}}', encoding="utf-8")

    def fail_unsupported(*_):
        raise OSError(getattr(errno, "EOPNOTSUPP", errno.ENOSYS), "not supported")

    monkeypatch.setattr(titler.os, "link", fail_unsupported)
    titler.migrate_legacy_state()
    assert json.loads(current.read_text())["sessions"] == {"unsupported": {}}
    assert not legacy.exists()


def test_mkdir_failure_is_fail_open(monkeypatch):
    _isolate_data_dir(monkeypatch)
    home = Path(titler.get_hermes_home())
    legacy = home / "plugins/hermes-auto-titler/state.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"version":1,"sessions":{}}', encoding="utf-8")

    def fail_mkdir(*args, **kwargs):
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(titler.Path, "mkdir", fail_mkdir)
    # Should not raise
    titler.migrate_legacy_state()
    assert legacy.exists()


def test_failed_cross_device_copy_does_not_delete_concurrent_destination(monkeypatch):
    _isolate_data_dir(monkeypatch)
    home = Path(titler.get_hermes_home())
    legacy = home / "plugins" / "hermes-auto-titler" / "state.json"
    current = home / "plugin-data" / "hermes-auto-titler" / "state.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('legacy', encoding="utf-8")

    def cross_device(_source, _destination):
        raise OSError(errno.EXDEV, "cross-device link")

    def concurrent_replacement(_source, _destination):
        replacement = current.with_suffix(".new")
        replacement.write_text('newer state', encoding="utf-8")
        os.replace(replacement, current)
        raise OSError(errno.EIO, "source read failed")

    monkeypatch.setattr(titler.os, "link", cross_device)
    monkeypatch.setattr(titler.shutil, "copyfileobj", concurrent_replacement)
    titler.migrate_legacy_state()

    assert current.read_text(encoding="utf-8") == 'newer state'
    assert legacy.read_text(encoding="utf-8") == 'legacy'
