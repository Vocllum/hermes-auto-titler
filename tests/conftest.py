"""测试环境：mock Hermes 内部模块，避免依赖 hermes-agent 源码环境。"""

import sys
import types

# hermes_state.SessionDB（titler.py 需要常量与类型）
fake_state = types.ModuleType("hermes_state")


class SessionDB:
    TITLE_SOURCE_DERIVED = "derived"
    TITLE_SOURCE_LLM = "llm"
    TITLE_SOURCE_USER = "user"
    MAX_TITLE_LENGTH = 100


fake_state.SessionDB = SessionDB
sys.modules.setdefault("hermes_state", fake_state)

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
