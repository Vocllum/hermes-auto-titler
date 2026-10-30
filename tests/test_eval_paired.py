import copy
import json
import stat
from types import SimpleNamespace

import pytest

from eval.paired import (
    FrozenCase, PromptCell, export_blind, load_cases, make_prompt_cells,
    run_evolution, run_frozen,
)


class Client:
    model = "test-model"
    provider = "test-provider"

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append({"messages": copy.deepcopy(messages), **kwargs})
        item = next(self.responses)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(text=item, model=self.model, id=f"test-{len(self.calls)}",
                               usage={"prompt_tokens": 30, "completion_tokens": 8})


def case(**changes):
    values = dict(case_id="anonymous-1", prefix=2, current_title="AutoTitler prompts",
                  events=[{"role": "user", "content": "Improve AutoTitler prompts"},
                          {"role": "assistant", "content": "Measure title drift first"},
                          {"role": "user", "content": "Keep the broader goal while debugging tests"}])
    return FrozenCase(**{**values, **changes})


def test_prompt_matrix_is_full_factorial_and_unique():
    cells = make_prompt_cells()
    assert len(cells) == len({cell.cell_id for cell in cells}) == 12
    assert {cell.variant for cell in cells} == {"minimal", "concise", "detailed"}
    assert {cell.style for cell in cells} == {"concise", "complete"}
    assert {cell.strategy for cell in cells} == {"conservative", "aggressive"}
    with pytest.raises(ValueError):
        PromptCell("unsupported", "concise", "conservative")


def test_frozen_incumbent_and_input_budget_do_not_drift_across_cells():
    sample = case()
    original = copy.deepcopy(sample.events)
    user_prompts, hashes = [], set()
    for cell in make_prompt_cells():
        client = Client('{"action":"keep"}')
        result = run_frozen(sample, cell, client, revision="baseline")
        user_prompts.append(client.calls[0]["messages"][1]["content"])
        hashes.add(result["evidence_hash"])
        assert "Current title: AutoTitler prompts" in user_prompts[-1]
        assert result["title_before"] == result["title_after"] == sample.current_title
        assert not result["changed"]
        assert result["attempts"][0]["transport_kind"] == "mock"
        assert result["attempts"][0]["usage"]["input_tokens"] == 30
    assert len(set(user_prompts)) == len(hashes) == 1
    assert sample.events == original


def test_frozen_retry_retains_failed_and_successful_attempts():
    client = Client(TimeoutError("timed out"), '{"action":"rename","title":"AutoTitler evaluation"}')
    result = run_frozen(case(), make_prompt_cells()[0], client, revision="baseline", max_attempts=2,
                        transport_kind="real_http")
    assert result["status"] == "ok"
    assert len(result["attempts"]) == 2
    failed, success = result["attempts"]
    assert failed["status"] == "error" and failed["error_type"] == "timeout"
    assert failed["usage"] is None and failed["attempt"] == 1
    assert success["parser_valid"] and success["attempt"] == 2
    assert all(attempt["transport_kind"] == "real_http" for attempt in result["attempts"])
    assert len(client.calls) == 2


def test_frozen_parse_failure_is_separate_from_http_success():
    result = run_frozen(case(), make_prompt_cells()[0], Client("not JSON"), revision="baseline")
    assert result["status"] == "failed"
    assert result["attempts"][0]["status"] == "success"
    assert result["attempts"][0]["parser_valid"] is False


def test_frozen_keep_without_incumbent_is_not_a_successful_first_title():
    result = run_frozen(case(current_title=None), make_prompt_cells()[0],
                        Client('{"action":"keep"}'), revision="baseline")
    assert result["status"] == "failed"
    assert result["attempts"][0]["parser_valid"] is True
    assert result["title_after"] is None


def test_frozen_proposed_title_approval_uses_frozen_candidate():
    sample = case(proposed_title="AutoTitler evaluation")
    result = run_frozen(sample, make_prompt_cells()[0], Client('{"action":"approve"}'), revision="baseline")
    assert result["title_after"] == sample.proposed_title
    assert result["changed"]


def test_language_rule_is_explicit_and_does_not_require_host_config(monkeypatch):
    def fail():
        raise AssertionError("live host configuration must not be read")
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", fail)
    client = Client('{"action":"keep"}')
    result = run_frozen(case(), make_prompt_cells()[0], client, revision="baseline",
                        language_rule="Write in English.")
    assert result["language_rule"] == "Write in English."
    assert "Write in English." in client.calls[0]["messages"][0]["content"]


def test_frozen_case_rejects_invalid_prefix_and_unknown_provenance():
    with pytest.raises(ValueError, match="prefix"):
        case(prefix=3)
    with pytest.raises(ValueError, match="provenance"):
        case(provenance="real")


@pytest.mark.parametrize("prefix", [True, False, 1.5, "2", None])
def test_frozen_case_rejects_non_integer_or_boolean_prefix(prefix):
    with pytest.raises(ValueError, match="prefix"):
        case(prefix=prefix)


@pytest.mark.parametrize("points", [[True], [False], [1.5], ["2"], [None]])
def test_evolution_rejects_non_integer_or_boolean_prefixes_before_calls(tmp_path, points):
    client = Client()
    with pytest.raises(ValueError, match="prefix"):
        run_evolution(case(), make_prompt_cells()[0], client, tmp_path,
                      revision="baseline", prefixes=points)
    assert client.calls == []
    assert not list(tmp_path.iterdir())


def test_frozen_approve_cleans_long_prefixed_proposal_like_pending_evolution(tmp_path):
    proposal = "Title: AutoTitler evaluation and ranking with reproducible evidence"
    sample = case(proposed_title=proposal)
    cell = make_prompt_cells()[0]
    frozen = run_frozen(sample, cell, Client('{"action":"approve"}'), revision="baseline")
    transitions = run_evolution(
        sample, cell, Client(json.dumps({"action": "rename", "title": proposal}), '{"action":"approve"}'),
        tmp_path, revision="baseline", prefixes=[1, 2],
    )
    assert frozen["status"] == "ok"
    assert frozen["title_after"] != proposal
    assert not frozen["title_after"].startswith("Title:")
    assert frozen["title_after"] == transitions[0]["pending"]["title"] == transitions[1]["title_after"]


def test_frozen_approve_without_proposal_is_invalid_and_not_a_hypothetical_change():
    result = run_frozen(case(), make_prompt_cells()[0], Client('{"action":"approve"}'), revision="baseline")
    assert result["status"] == "failed"
    assert result["changed"] is False


def test_load_cases_requires_unique_explicit_ids(tmp_path):
    path = tmp_path / "cases.json"
    entry = {"case_id": "one", "events": [{"role": "user", "content": "Topic"}], "prefix": 1}
    path.write_text(json.dumps([entry, entry]))
    with pytest.raises(ValueError, match="duplicate"):
        load_cases(path)
    path.write_text(json.dumps([entry]))
    assert load_cases(path)[0].provenance == "synthetic"


def test_evolution_uses_pending_confirmation_then_real_cas_write(tmp_path):
    sample = case(current_title="Old topic")
    client = Client('{"action":"rename","title":"AutoTitler evaluation"}', '{"action":"approve"}')
    transitions = run_evolution(sample, make_prompt_cells()[0], client, tmp_path,
                               revision="baseline", prefixes=[1, 2])
    first, second = transitions
    assert first["state_action"] == "pending"
    assert first["title_after"] == "Old topic" and not first["changed"]
    assert first["pending"]["title"] == "AutoTitler evaluation"
    assert second["state_action"] == "renamed" and second["changed"]
    assert second["title_before"] == "Old topic"
    assert second["title_after"] == "AutoTitler evaluation" and second["title_source"] == "llm"
    assert all(record["scheduler_gates"] == "bypassed" for record in transitions)
    assert [record["attempts"][0]["prefix"] for record in transitions] == [1, 2]
    assert len(list(tmp_path.glob("paired-*/state.db"))) == 1


def test_evolution_protects_manual_title_without_model_call(tmp_path):
    sample = case(initial_title_source="user")
    client = Client()
    [record] = run_evolution(sample, make_prompt_cells()[0], client, tmp_path, revision="baseline")
    assert record["state_action"] == "skipped"
    assert record["title_before"] == record["title_after"] == sample.current_title
    assert record["attempts"] == client.calls == []


def test_evolution_first_title_bypasses_review_and_preserves_source_events(tmp_path):
    sample = case(current_title=None, initial_title_source=None)
    original = copy.deepcopy(sample.events)
    [record] = run_evolution(sample, make_prompt_cells()[0],
                             Client('{"action":"rename","title":"AutoTitler evaluation"}'),
                             tmp_path, revision="baseline")
    assert record["state_action"] == "renamed"
    assert record["title_source"] == "llm"
    assert sample.events == original


def test_evolution_rejects_out_of_order_prefixes_and_archived_rows(tmp_path):
    with pytest.raises(ValueError, match="prefixes"):
        run_evolution(case(), make_prompt_cells()[0], Client(), tmp_path,
                      revision="baseline", prefixes=[2, 1])
    sample = case(events=[{"role": "user", "content": "Original", "active": 0, "compacted": 1},
                         {"role": "user", "content": "Current"}])
    with pytest.raises(ValueError, match="inactive"):
        run_evolution(sample, make_prompt_cells()[0], Client(), tmp_path, revision="baseline")


def test_export_blind_hides_all_routes_and_revision_and_keeps_private_map(tmp_path):
    sample = case(provenance="real_redacted")
    records = [run_frozen(sample, cell, Client('{"action":"keep"}'), revision="secret-revision")
               for cell in make_prompt_cells()[:2]]
    packets_path, map_path = tmp_path / "review.json", tmp_path / "mapping.json"
    packets_path.write_text("old")
    packets_path.chmod(0o644)
    counts = export_blind(records, packets_path, map_path, cases=[sample])
    assert counts == {"groups": 1, "candidates": 2, "excluded": 0}
    packets = json.loads(packets_path.read_text())
    assert set(packets[0]) == {"review_id", "candidates", "evidence"}
    assert all(set(candidate) == {"candidate_id", "title"} for candidate in packets[0]["candidates"])
    assert "secret-revision" not in packets_path.read_text()
    assert "anonymous-1" not in packets_path.read_text()
    assert len(json.loads(map_path.read_text())) == 2
    assert stat.S_IMODE(packets_path.stat().st_mode) == stat.S_IMODE(map_path.stat().st_mode) == 0o600
    with pytest.raises(ValueError, match="differ"):
        export_blind(records, packets_path, packets_path)


def test_transport_labels_require_explicit_supported_kind():
    with pytest.raises(ValueError, match="transport"):
        run_frozen(case(), make_prompt_cells()[0], Client(), revision="baseline", transport_kind="real")
