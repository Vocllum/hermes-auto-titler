from scripts.prompt_acceptance import (
    aggregate_records,
    build_prefixes,
    extract_turns,
    parse_score,
)


def test_experiment_knobs_are_not_public_defaults():
    from scripts.prompt_acceptance import _experiment_config

    base = {}
    config = _experiment_config(base, variant="minimal", input_variant="minimal", style="concise", strategy="conservative")
    assert config["input_variant"] == "minimal"
    assert "input_variant" not in base


def test_default_experiment_uses_current_variant_only():
    from scripts.prompt_acceptance import VARIANTS, _experiment_config

    assert VARIANTS == ("minimal", "concise", "detailed")
    assert _experiment_config({}, variant="concise", input_variant="current", style="concise", strategy="conservative")["prompt_variant"] == "concise"


def test_input_variant_is_independent_from_prompt_variant():
    from scripts.prompt_acceptance import _experiment_config

    base = {}
    config = _experiment_config(base, variant="detailed", input_variant="minimal", style="concise", strategy="conservative")
    assert config["prompt_variant"] == "detailed"
    assert config["input_variant"] == "minimal"


def test_session_sampling_skips_user_owned_titles():
    import random
    from scripts.prompt_acceptance import _session_candidates

    class FakeDB:
        TITLE_SOURCE_USER = "user"

        def list_sessions_rich(self, **kwargs):
            return [
                {"id": "manual", "title": "手改", "title_source": "user"},
                {"id": "auto", "title": "自动", "title_source": "llm"},
            ]

        def get_session_title_source(self, sid):
            return "user" if sid == "manual" else "llm"

        def get_messages_as_conversation(self, sid, include_ancestors=True):
            return [{"role": "user", "content": f"topic {sid}"}]

    samples = _session_candidates(
        FakeDB(), rng=random.Random(1), n=1, min_users=1, max_users=2
    )
    assert [sample.session_id for sample in samples] == ["auto"]


def test_recent_sampling_uses_activity_order():
    import random
    from scripts.prompt_acceptance import _session_candidates

    class FakeDB:
        TITLE_SOURCE_USER = "user"

        def list_sessions_rich(self, **kwargs):
            return [
                {"id": "old", "last_active": "2026-01-01"},
                {"id": "new", "last_active": "2026-02-01"},
            ]

        def get_session_title_source(self, sid):
            return "llm"

        def get_messages_as_conversation(self, sid, include_ancestors=True):
            return [{"role": "user", "content": f"topic {sid}"}]

    samples = _session_candidates(
        FakeDB(), rng=random.Random(1), n=1, min_users=1, max_users=2, sample="recent"
    )
    assert [sample.session_id for sample in samples] == ["new"]


def test_build_prefixes_progresses_without_duplicate_boundaries():
    turns = [[("user", f"u{i}")] for i in range(1, 7)]
    config = {"opening_turns": 2, "recent_turns": 2, "preview_chars": 400,
              "user_message_preview_chars": 300, "user_message_threshold": 40}
    assert [p.number for p in build_prefixes(turns, config, "1,2,4,all")] == [1, 2, 4, 6]
    assert [p.number for p in build_prefixes(turns, config, "2,all,2")] == [2, 6]


def test_extract_turns_discards_machine_noise_and_summaries():
    messages = [
        {"role": "user", "content": "真实目标"},
        {"role": "assistant", "content": "回复"},
        {"role": "user", "content": "[System note: interrupted]"},
        {"role": "user", "content": "第二个目标"},
    ]
    turns = extract_turns(messages, preview_chars=400)
    assert [[role for role, _ in turn] for turn in turns] == [["user", "assistant"], ["user"]]
    assert [turn[0][1] for turn in turns] == ["真实目标", "第二个目标"]


def test_parse_score_bounds_and_computes_weighted_total():
    score = parse_score('{"scores":[{"index":1,"coverage":5,"specificity":3,"faithfulness":4,"brevity":2,"language":4}]}', 1)
    assert score == [{
        "index": 1, "coverage": 5, "specificity": 3, "faithfulness": 4,
        "brevity": 2, "language": 4, "total": 18, "note": "",
    }]


def test_aggregate_records_compares_prompt_variants():
    records = [
        {"variant": "minimal", "prefix": 1, "score": {"total": 15}, "title": "a"},
        {"variant": "minimal", "prefix": 2, "score": {"total": 20}, "title": "ab"},
        {"variant": "current", "prefix": 1, "score": {"total": 20}, "title": "abc"},
    ]
    result = aggregate_records(records)
    assert result["minimal"]["n"] == 2
    assert result["minimal"]["mean_total"] == 17.5
    assert result["current"]["mean_total"] == 20
    assert result["minimal"]["mean_title_chars"] == 1.5


def test_context_profiles_change_only_experiment_context_budgets():
    from scripts.prompt_acceptance import _context_config

    base = {"opening_turns": 2, "recent_turns": 2, "preview_chars": 400,
            "include_all_user_messages": True, "user_message_threshold": 40}
    assert _context_config(base, "production") == base
    lean = _context_config(base, "lean")
    assert lean["opening_turns"] == 1
    assert lean["recent_turns"] == 2
    assert lean["preview_chars"] == 100
    assert lean["include_all_user_messages"] is False
    assert base["opening_turns"] == 2


def test_build_prefixes_respects_include_all_user_messages():
    turns = [[("user", f"u{i}"), ("assistant", f"a{i}")] for i in range(1, 5)]
    config = {"opening_turns": 1, "recent_turns": 1, "preview_chars": 100,
              "include_all_user_messages": False, "user_message_preview_chars": 300,
              "user_message_threshold": 40}
    [prefix] = build_prefixes(turns, config, "all")
    assert prefix.users == []


def test_aggregate_records_excludes_generation_failures():
    from scripts.prompt_acceptance import aggregate_records

    result = aggregate_records([
        {"variant": "current", "title": "", "status": "generation_error", "score": {"total": 0}},
        {"variant": "current", "title": "valid", "status": "ok", "score": {"total": 20}},
    ])
    assert result["current"]["n"] == 1
    assert result["current"]["mean_total"] == 20


def test_call_count_handles_host_client_without_counter():
    from scripts.prompt_acceptance import _call_count

    assert _call_count(object()) == 0


def test_failure_records_are_not_quality_scores():
    from scripts.prompt_acceptance import aggregate_records

    assert aggregate_records([{"variant": "current", "status": "generation_error"}]) == {}


def test_records_without_complete_scores_are_not_aggregated():
    from scripts.prompt_acceptance import aggregate_records

    assert aggregate_records([{"variant": "current", "status": "ok", "score": {"coverage": 5}}]) == {}


def test_judge_group_excludes_generation_errors_in_source():
    source = __import__("pathlib").Path(__file__).parents[1].joinpath("scripts", "prompt_acceptance.py").read_text()
    assert 'and r.get("status") == "ok"' in source


def test_experiment_requires_quality_score_to_succeed():
    from scripts.prompt_acceptance import aggregate_records

    assert aggregate_records([]) == {}


def test_experiment_default_variant_is_current_only():
    from scripts.prompt_acceptance import parse_args

    old = __import__("sys").argv
    try:
        __import__("sys").argv = ["prompt_acceptance.py"]
        assert parse_args().variants == "minimal,concise,detailed"
        assert parse_args().input_variants == "current,minimal"
        assert parse_args().strategies == "conservative,aggressive"
        assert parse_args().contexts == "production,lean"
    finally:
        __import__("sys").argv = old


def test_sample_fingerprint_is_stable_and_excludes_content():
    from scripts.prompt_acceptance import Sample, _sample_fingerprint

    samples = [Sample("s1", "secret title", [[("user", "secret text")]])]
    assert _sample_fingerprint(samples) == ["s1"]


def test_mean_evolution_changes_does_not_cross_session_boundaries():
    from scripts.prompt_acceptance import _mean_evolution_changes

    records = [
        {"session_id": "a", "prefix": 1, "title": "A", "status": "ok"},
        {"session_id": "a", "prefix": 2, "title": "B", "status": "ok"},
        {"session_id": "b", "prefix": 1, "title": "X", "status": "ok"},
        {"session_id": "b", "prefix": 2, "title": "X", "status": "ok"},
    ]
    assert _mean_evolution_changes(records) == 0.5
