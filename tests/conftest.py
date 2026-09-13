"""测试环境：mock Hermes 内部模块，避免依赖 hermes-agent 源码环境。"""

import sys
import types

import pytest

# hermes_state.SessionDB（titler.py 需要常量与类型）
fake_state = types.ModuleType("hermes_state")


class SessionDB:
    TITLE_SOURCE_DERIVED = "derived"
    TITLE_SOURCE_LLM = "llm"
    TITLE_SOURCE_USER = "user"
    MAX_TITLE_LENGTH = 100


fake_state.SessionDB = SessionDB
sys.modules.setdefault("hermes_state", fake_state)

# hermes_cli.config（语言解析 / takeover 配置测试需要宿主模块名存在）
fake_cli = types.ModuleType("hermes_cli")
fake_cli_config = types.ModuleType("hermes_cli.config")
fake_cli_config.load_config_readonly = lambda: {}
fake_cli_config.load_config = lambda: {}
fake_cli_config.save_config = lambda cfg: None
fake_cli.config = fake_cli_config
sys.modules.setdefault("hermes_cli", fake_cli)
sys.modules.setdefault("hermes_cli.config", fake_cli_config)

# agent.plugin_llm.PluginLlmTextInput
fake_agent = types.ModuleType("agent")
fake_plugin_llm = types.ModuleType("agent.plugin_llm")


class PluginLlmTextInput:
    def __init__(self, text):
        self.text = text


fake_plugin_llm.PluginLlmTextInput = PluginLlmTextInput
fake_agent.plugin_llm = fake_plugin_llm
sys.modules.setdefault("agent", fake_agent)
sys.modules.setdefault("agent.plugin_llm", fake_plugin_llm)


@pytest.fixture(autouse=True)
def _isolate_core_titler_tests_from_review_default(request):
    """Keep legacy core tests focused on the behavior they actually target.

    v0.2 changes the public default review depth from 0 to 1. Most tests in
    ``test_titler`` exercise writeback, provenance, cleaning, lifecycle, and
    race handling rather than the review policy itself; their historical
    single-call expectations should remain explicit. Review-specific tests in
    that module pass ``rename_confirmations`` themselves, so they still opt in.

    Config-default behavior is tested separately in ``test_messages_config``.
    """
    if request.module.__name__.endswith("test_titler"):
        from hermes_auto_titler.config import DEFAULTS

        old = DEFAULTS["rename_confirmations"]
        DEFAULTS["rename_confirmations"] = 0
        try:
            yield
        finally:
            DEFAULTS["rename_confirmations"] = old
        return
    yield
