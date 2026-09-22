"""Release gate runner's three-state contract.

The gate exists to stop whoever is holding the tag, so its failure modes must be
unmistakable: exit 0 = in sync, exit 1 = catalog lags, exit 2 = could not verify
(network unreachable / tag missing). Exit 2 must never look like a pass -- that
would render an unrun verification as a green light.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_catalog_sync.py"


def _load_module(monkeypatch):
    spec = importlib.util.spec_from_file_location("check_catalog_sync", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # main() parses sys.argv; under pytest that is pytest's own argv
    monkeypatch.setattr(sys, "argv", ["check_catalog_sync.py"])
    return mod


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def _serve(monkeypatch, mod, payload):
    monkeypatch.setattr(
        mod.urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(payload)
    )


def test_gate_runner_is_tracked_in_git():
    """A gate with no runner in the repo passes silently in CI -- worse than red."""
    assert SCRIPT.is_file(), "scripts/check_catalog_sync.py must exist in the repo"
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "scripts/check_catalog_sync.py"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert tracked.returncode == 0, "gate runner must be tracked, not left untracked"


def test_release_workflow_binds_the_gate():
    """Being in the repo is not the same as being run: the workflow must call it."""
    wf = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "tests.yml").read_text())
    jobs = wf["jobs"]
    assert "catalog-sync" in jobs, "release-time catalog gate job missing"
    job = jobs["catalog-sync"]
    runs = " ".join(s.get("run", "") for s in job["steps"])
    assert "scripts/check_catalog_sync.py" in runs, "no step runs the gate"
    # a missing runner must fail loudly, not ride on python's "file not found" code
    assert "test -f scripts/check_catalog_sync.py" in runs
    # per-push it would exit 2 whenever plugin.yaml's tag does not exist yet
    assert "workflow_dispatch" in str(job.get("if", ""))
    # the gate needs the tag, so the clone must fetch them
    checkout = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout")]
    assert checkout and checkout[0].get("with", {}).get("fetch-tags") is True


def test_exit_2_when_tag_for_local_version_is_missing(monkeypatch):
    """plugin.yaml's version has no tag yet -> exit 2, not 0 and not 1."""
    mod = _load_module(monkeypatch)

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(128, cmd)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod, "local_version", lambda: "9.9.9")
    assert mod.main() == 2


def test_exit_2_when_live_catalog_unreachable(monkeypatch):
    """Network down -> exit 2 (unknown), never "assume in sync"."""
    mod = _load_module(monkeypatch)
    monkeypatch.setattr(mod, "local_version", lambda: "0.2.3.1")
    monkeypatch.setattr(mod, "local_commit_for", lambda tag: "a" * 40)

    def boom(req, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    assert mod.main() == 2


def test_exit_1_when_catalog_lags(monkeypatch, capsys):
    """Live sha differs from the plugin.yaml version's commit -> exit 1."""
    mod = _load_module(monkeypatch)
    monkeypatch.setattr(mod, "local_version", lambda: "0.2.3.1")
    monkeypatch.setattr(mod, "local_commit_for", lambda tag: "b" * 40)
    _serve(monkeypatch, mod, {
        "generated_at": "2026-09-22T00:00:00Z",
        "entries": [{"name": "hermes-auto-titler", "sha": "a" * 40,
                     "version": "0.2.3.1"}],
    })
    assert mod.main() == 1
    assert "catalog lags" in capsys.readouterr().out


def test_exit_0_when_in_sync(monkeypatch):
    """sha and version both match -> exit 0."""
    mod = _load_module(monkeypatch)
    monkeypatch.setattr(mod, "local_version", lambda: "0.2.3.1")
    monkeypatch.setattr(mod, "local_commit_for", lambda tag: "c" * 40)
    _serve(monkeypatch, mod, {
        "entries": [{"name": "hermes-auto-titler", "sha": "c" * 40,
                     "version": "0.2.3.1"}],
    })
    assert mod.main() == 0


def test_short_sha_fails_instead_of_being_cosmetic(monkeypatch, capsys):
    """A short sha makes the host silently drop the whole entry: that is a failure."""
    mod = _load_module(monkeypatch)
    monkeypatch.setattr(mod, "local_version", lambda: "0.2.3.1")
    monkeypatch.setattr(mod, "local_commit_for", lambda tag: "d" * 40)
    _serve(monkeypatch, mod, {
        "entries": [{"name": "hermes-auto-titler", "sha": "f97cb94",
                     "version": "0.2.3.1"}],
    })
    assert mod.main() == 1
    assert "40-hex" in capsys.readouterr().out


def test_missing_entry_fails(monkeypatch):
    """No entry named like us is not a valid state."""
    mod = _load_module(monkeypatch)
    monkeypatch.setattr(mod, "local_version", lambda: "0.2.3.1")
    monkeypatch.setattr(mod, "local_commit_for", lambda tag: "e" * 40)
    _serve(monkeypatch, mod, {"entries": []})
    assert mod.main() == 1
