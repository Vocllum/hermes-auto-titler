"""Regression: plugin-owned background work must carry the profile secret scope.

Under ``gateway.multiplex_profiles`` the host fails ``get_secret()`` closed for any
task with no profile scope installed.  Plugin registration is not one of the host's
scoped seams, so before the fix the titler's retry/sweep thread ran unscoped: every
sweep evaluation died with ``UnscopedSecretError`` and its session was parked as
``blocked`` instead of being retitled.  These tests pin the two halves of the fix —
the helper binds/does not bind in the right conditions, and ``register()`` starts
the retry loop inside the bound window so ``copy_context()`` carries the scope.
"""

import sys
import types

import pytest

import hermes_auto_titler
from hermes_auto_titler import _bind_background_secret_scope, _reset_background_secret_scope


class _FakeSecretScope:
    """Minimal stand-in for ``agent.secret_scope`` (call recording included)."""

    def __init__(self, *, multiplex=True, bound=None, keys=None):
        self.multiplex = multiplex
        self.bound = bound
        self.keys = keys if keys is not None else {"OPENCODE_GO_API_KEY": "value"}
        self.bound_calls = []
        self.reset_calls = []

    def is_multiplex_active(self):
        return self.multiplex

    def current_secret_scope(self):
        return self.bound

    def build_profile_secret_scope(self, home):
        self.home_seen = home
        return dict(self.keys)

    def set_secret_scope(self, scope, profile_home=None):
        self.bound = scope
        token = object()
        self.bound_calls.append((dict(scope), profile_home, token))
        return token

    def reset_secret_scope(self, token):
        self.reset_calls.append(token)
        self.bound = None


@pytest.fixture
def fake_scope(monkeypatch):
    scope = _FakeSecretScope()
    module = types.ModuleType("agent.secret_scope")
    for name in (
        "is_multiplex_active",
        "current_secret_scope",
        "build_profile_secret_scope",
        "set_secret_scope",
        "reset_secret_scope",
    ):
        setattr(module, name, getattr(scope, name))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", module)

    constants = sys.modules.get("hermes_constants") or types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: "/root/.hermes"
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    return scope


def test_binds_profile_scope_when_multiplexing_and_unscoped(fake_scope):
    token = _bind_background_secret_scope()

    assert token is not None
    assert len(fake_scope.bound_calls) == 1
    scope, profile_home, _ = fake_scope.bound_calls[0]
    assert scope == fake_scope.keys
    assert profile_home == "/root/.hermes"
    assert fake_scope.home_seen == "/root/.hermes"


def test_no_binding_when_multiplexing_is_off(fake_scope):
    fake_scope.multiplex = False

    assert _bind_background_secret_scope() is None
    assert fake_scope.bound_calls == []


def test_no_binding_when_a_scope_is_already_installed(fake_scope):
    fake_scope.bound = {"EXISTING": "1"}

    assert _bind_background_secret_scope() is None
    assert fake_scope.bound_calls == []
    assert fake_scope.bound == {"EXISTING": "1"}


def test_no_binding_when_the_host_lacks_the_scope_api(monkeypatch):
    # A ``None`` entry in sys.modules makes ``import agent.secret_scope`` fail,
    # standing in for an older host without the multi-profile secret scope API.
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)

    assert _bind_background_secret_scope() is None


def test_reset_unbinds_and_swallows_missing_api(fake_scope, monkeypatch):
    token = _bind_background_secret_scope()
    _reset_background_secret_scope(token)
    assert fake_scope.reset_calls == [token]

    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)
    _reset_background_secret_scope(object())  # must not raise
    _reset_background_secret_scope(None)  # nothing bound -> no-op


def test_register_starts_the_retry_loop_inside_the_bound_scope(fake_scope, monkeypatch):
    """The sweep thread snapshots the context at ``start_retry_loop()`` time."""
    seen = {}

    def fake_start_retry_loop(self):
        seen["scope_during_start"] = fake_scope.current_secret_scope()

    monkeypatch.setattr(hermes_auto_titler.AutoTitler, "start_retry_loop", fake_start_retry_loop)
    monkeypatch.setattr(hermes_auto_titler.AutoTitler, "restore_state", lambda self: None)
    monkeypatch.setattr(hermes_auto_titler, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(hermes_auto_titler, "load_config", lambda ctx=None: {"enabled": True})

    class FakeCtx:
        def register_hook(self, *args, **kwargs):
            return None

        def register_command(self, *args, **kwargs):
            return None

    hermes_auto_titler.register(FakeCtx())

    assert seen["scope_during_start"] == fake_scope.keys
    # ... and the binding is released again, so the loader thread is not left scoped.
    assert fake_scope.reset_calls, "the scope token must be reset after the thread starts"
    assert fake_scope.current_secret_scope() is None
