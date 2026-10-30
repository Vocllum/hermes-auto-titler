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
def test_all_profiles_have_style_strategy_and_language_aware_length(variant):
    compact = system_prompt(variant)
    complete = system_prompt(variant, style="complete")
    aggressive = system_prompt(variant, strategy="aggressive")
    assert "compact label" in compact
    assert "primary intent" in complete
    assert "Strategy: conservative" in compact
    assert "Strategy: aggressive" in aggressive
    assert compact != complete != aggressive
    assert "20 CJK characters" in compact
    assert "26 CJK characters" in complete
    assert "40 display columns" in compact
    assert "52 display columns" in complete


def test_length_hint_follows_effective_hard_bounds():
    prompt = system_prompt("minimal", max_display_width=30, max_title_length=9)
    assert "9 CJK characters" in prompt
    assert "at most 9 characters" in prompt
    assert "30 display columns" in prompt


def test_pr_references_are_excluded_only_when_incidental():
    prompt = system_prompt("concise")
    assert "workflow references (including PRs), unless they are themselves the subject" in prompt


def test_minimal_is_compact_but_carries_the_same_essential_controls():
    prompts = {name: system_prompt(name) for name in ("minimal", "concise", "detailed")}
    assert len(prompts["minimal"]) < len(prompts["concise"]) < len(prompts["detailed"])
    assert len(prompts["minimal"]) < 1100
    assert len(prompts["concise"]) < 2000
    assert len(prompts["detailed"]) < 2800
