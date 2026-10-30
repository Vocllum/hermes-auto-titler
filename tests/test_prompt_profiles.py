from types import SimpleNamespace

import pytest

from hermes_auto_titler.config import DEFAULTS
from hermes_auto_titler.policy import AutoTitler


def system_prompt(variant, style="concise", strategy="conservative", **config):
    calls = []

    class Llm:
        def complete(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(text='{"action":"keep"}')

    titler = AutoTitler(SimpleNamespace(llm=Llm()), {
        **DEFAULTS, **config, "prompt_variant": variant,
        "title_style": style, "strategy": strategy,
    }, db=SimpleNamespace())
    titler._language_rule = lambda: "Use the user's primary language."
    titler._generate("AcmeShop checkout", [], [], [])
    return calls[-1]["messages"][0]["content"]


@pytest.mark.parametrize("variant", ["minimal", "concise", "detailed"])
def test_all_profiles_preserve_accurate_incumbent_and_avoid_completion_claims(variant):
    prompt = system_prompt(variant)
    assert "do not polish, shorten or restyle" in prompt
    assert "never claim completion" in prompt
    assert "desired outcome" in prompt
    assert "weeks later" in prompt


@pytest.mark.parametrize("variant", ["minimal", "concise", "detailed"])
def test_all_profiles_have_style_strategy_and_natural_brevity(variant):
    compact = system_prompt(variant)
    complete = system_prompt(variant, style="complete")
    aggressive = system_prompt(variant, strategy="aggressive")
    assert "compact label" in compact
    assert "primary intent" in complete
    assert "Strategy: conservative" in compact
    assert "Strategy: aggressive" in aggressive
    assert compact != complete != aggressive
    for prompt in (compact, complete, aggressive):
        assert "use a compact, natural sidebar label" in prompt
        assert "preserves the subject and essential identifiers" in prompt
        assert "Do not truncate names" in prompt
        assert "CJK characters" not in prompt
        assert "display columns" not in prompt
        assert "words or" not in prompt


@pytest.mark.parametrize("variant", ["minimal", "concise", "detailed"])
def test_legacy_length_keys_do_not_change_prompts(variant):
    assert system_prompt(variant, max_display_width=30, max_title_length=9) == system_prompt(variant)


def test_pr_references_are_excluded_only_when_incidental():
    prompt = system_prompt("concise")
    assert "workflow references (including PRs), unless they are themselves the subject" in prompt


def test_minimal_is_compact_but_carries_the_same_essential_controls():
    prompts = {name: system_prompt(name) for name in ("minimal", "concise", "detailed")}
    assert len(prompts["minimal"]) < len(prompts["concise"]) < len(prompts["detailed"])
    assert len(prompts["minimal"]) < 1100
    assert len(prompts["concise"]) < 2000
    assert len(prompts["detailed"]) < 2800
