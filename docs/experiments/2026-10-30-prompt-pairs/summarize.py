"""Aggregate private ATT recordings into content-free public evidence.

This file does not call a model or mutate the source corpus. Frozen decisions
are counterfactual decisions, not confirmed scheduler/writeback outcomes.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from itertools import product
import json
import math
from pathlib import Path
import re
from statistics import mean
import unicodedata


VARIANTS = ("minimal", "concise", "detailed")
STYLES = ("concise", "complete")
STRATEGIES = ("conservative", "aggressive")
GOOD_FIELDS = ("mainline", "faithful", "recognize", "language")
BAD_FIELDS = ("completion_claim", "incidental_noise")
USAGE_FIELDS = (
    "input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens",
    "cache_write_tokens", "total_tokens", "cost_usd",
)
SAFE_ACTIONS = {
    "keep", "rename", "approve", "error", "renamed", "pending", "failed",
    "skipped", "skip", "throttled", "protected", "unchanged",
}
SAFE_ERRORS = {"route", "auth", "rate_limit", "timeout", "parse", "other", "unclassified"}


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def numeric(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def safe_identifier(value, *, limit=160):
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._@/:-]+", value):
        return None
    if "://" in value or value.startswith(("/", "~")) or "@" in value:
        return None
    return value


def safe_hash(value):
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{16,64}", value) else None


def normalized_score(raw):
    aliases = {"completion_claim": "completion", "incidental_noise": "incidentalnoise"}
    score = {}
    for field in GOOD_FIELDS + BAD_FIELDS:
        value = raw.get(field, raw.get(aliases.get(field, "")))
        if value not in (0, 1) or not isinstance(value, (bool, int)):
            return None
        score[field] = int(value)
    return score


def score_total(score):
    return sum(score[x] for x in GOOD_FIELDS) + sum(1 - score[x] for x in BAD_FIELDS)


def load_scores(map_path, scores_path, case_ids):
    if not map_path.exists() or not scores_path.exists():
        return {}, {"available": False, "reason": "final_blind_review_not_available"}
    mapping = json.loads(map_path.read_text())
    raw_scores = json.loads(scores_path.read_text())
    mapping = mapping.get("mapping", mapping) if isinstance(mapping, dict) else mapping
    raw_scores = raw_scores.get("scores", raw_scores) if isinstance(raw_scores, dict) else raw_scores
    if isinstance(raw_scores, dict):
        raw_scores = [dict(v, candidate_id=k) for k, v in raw_scores.items()]
    if not isinstance(mapping, dict) or not isinstance(raw_scores, list):
        raise ValueError("Unexpected blind-review schema")
    lookup = {}
    invalid, unmapped = 0, 0
    for raw in raw_scores:
        target = mapping.get(raw.get("candidate_id"))
        if not isinstance(target, dict) or target.get("case_id") not in case_ids:
            unmapped += 1
            continue
        title = target.get("title", target.get("title_after"))
        score = normalized_score(raw)
        if not isinstance(title, str) or score is None:
            invalid += 1
            continue
        key = target["case_id"], title
        if key in lookup and lookup[key] != score:
            raise ValueError("Conflicting scores for one anonymous case/title")
        lookup[key] = score
    return lookup, {
        "available": True, "unique_scored_case_titles": len(lookup),
        "invalid_score_rows": invalid, "unmapped_score_rows": unmapped,
        "reviewer_type": "independent_agent_blind_review",
        "scoring": "four good binary fields plus two inverse bad binary fields; total 0-6",
        "language_evidence": "independent blind language score; no CJK-character accuracy proxy",
    }


def validate_rows(rows, corpus):
    for row in rows:
        if row.get("case_id") not in corpus:
            raise ValueError("Recording contains a case outside the frozen corpus")
        if row.get("variant") not in VARIANTS or row.get("style") not in STYLES or row.get("strategy") not in STRATEGIES:
            raise ValueError("Recording contains an unknown matrix cell")
        if safe_identifier(row.get("requested_model")) is None:
            raise ValueError("Unsafe or missing requested model metadata")
        if row.get("category") != corpus[row["case_id"]]["category"]:
            raise ValueError("Recording category differs from frozen corpus")
        if row.get("revision") not in {"baseline", "revised"}:
            raise ValueError("Recording contains an unexpected revision label")


def group_key(row):
    return row["requested_model"], row["revision"], row["variant"], row["style"], row["strategy"]


def group_metadata(key):
    model, revision, variant, style, strategy = key
    return {"requested_model": model, "revision": revision, "variant": variant,
            "style": style, "strategy": strategy, "cell_id": f"{variant}/{style}/{strategy}"}


def last_attempt(row):
    attempts = row.get("attempts") or []
    return attempts[-1] if attempts else {}


def parser_valid(row):
    return bool(last_attempt(row).get("parser_valid")) and row.get("model_action") in {"keep", "rename", "approve"}


def successful(row):
    return any(a.get("status") == "success" for a in row.get("attempts") or [])


def review_for(row, scores):
    return scores.get((row["case_id"], row.get("title_after"))) if parser_valid(row) else None


def usage_summary(attempts):
    result = {}
    for field in USAGE_FIELDS:
        values = [numeric((a.get("usage") or {}).get(field)) for a in attempts]
        known = [x for x in values if x is not None]
        # Partial reports must never masquerade as a full experiment total.
        result[field] = sum(known) if values and len(known) == len(values) else None
        result[field + "_known_sum"] = sum(known) if known else None
        result[field + "_reported_attempts"] = len(known)
        result[field + "_unknown_attempts"] = len(values) - len(known)
    return result


def normalize_probe_usage(raw):
    if not isinstance(raw, dict):
        return {field: None for field in USAGE_FIELDS}
    prompt_details = raw.get("prompt_tokens_details") or {}
    completion_details = raw.get("completion_tokens_details") or {}
    return {
        "input_tokens": numeric(raw.get("input_tokens", raw.get("prompt_tokens", raw.get("promptTokenCount")))),
        "output_tokens": numeric(raw.get("output_tokens", raw.get("completion_tokens", raw.get("candidatesTokenCount")))),
        "reasoning_tokens": numeric(raw.get("reasoning_tokens", completion_details.get("reasoning_tokens", raw.get("thoughtsTokenCount")))),
        "cache_read_tokens": numeric(raw.get("cache_read_tokens", prompt_details.get("cached_tokens", raw.get("cachedContentTokenCount")))),
        "cache_write_tokens": numeric(raw.get("cache_write_tokens", prompt_details.get("cache_write_tokens"))),
        "total_tokens": numeric(raw.get("total_tokens", raw.get("totalTokenCount"))),
        "cost_usd": numeric(raw.get("cost_usd")),
    }


def read_probe_rows(path):
    if not path.exists():
        return []
    raw = json.loads(path.read_text())
    if isinstance(raw, dict):
        raw = raw.get("probes", raw.get("records", [raw]))
    if not isinstance(raw, list):
        raise ValueError("Unexpected route-probe ledger schema")
    rows = []
    for index, probe in enumerate(raw, 1):
        requested = probe.get("requested_model", probe.get("requested"))
        returned = probe.get("returned_model", probe.get("response_model", probe.get("returned")))
        request_id = probe.get("request_id", probe.get("id"))
        status = probe.get("status") or ("success" if request_id and returned else "error")
        if status == "ok":
            status = "success"
        rows.append({
            "case_id": f"probe-{index:02d}", "category": "route_probe", "revision": "probe",
            "variant": None, "style": None, "strategy": None, "prefix": None,
            "requested_model": requested, "mode": "route_probe",
            "attempts": [{"attempt": 1, "status": status, "transport_kind": "real_http",
                          "response_model": returned, "request_id": request_id,
                          "usage": normalize_probe_usage(probe.get("usage")),
                          "elapsed": probe.get("elapsed", probe.get("elapsed_seconds")),
                          "system_prompt_hash": probe.get("system_prompt_hash"),
                          "user_prompt_hash": probe.get("user_prompt_hash"),
                          "response_hash": probe.get("response_hash")}],
        })
    return rows


def scope_metrics(rows):
    attempts = [attempt for row in rows for attempt in row.get("attempts") or []]
    actual = [attempt for attempt in attempts if attempt.get("transport_kind") == "real_http"]
    return {
        "records": len(rows), "attempts": len(attempts),
        "supplier_route_attempts": len(actual),
        "successful_supplier_completions": sum(a.get("status") == "success" for a in actual),
        "mock_or_replay_attempts": len(attempts) - len(actual),
        "usage": usage_summary(actual),
    }


def display_width(text):
    return sum(0 if unicodedata.category(c) in {"Mn", "Me", "Cf"} else
               2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1 for c in text)


def raw_candidate(row):
    if row.get("model_action") != "rename":
        return None
    raw = last_attempt(row).get("model_title")
    return raw if isinstance(raw, str) else row.get("candidate")


def count_identifiers(rows, corpus):
    raw_eligible = raw_pass = after_eligible = after_pass = truncation_loss = 0
    for row in rows:
        if not parser_valid(row):
            continue
        identifiers = corpus[row["case_id"]].get("required_identifiers") or []
        if not identifiers:
            continue
        after = str(row.get("title_after") or "")
        after_ok = all(ident in after for ident in identifiers)
        after_eligible += 1
        after_pass += after_ok
        candidate = raw_candidate(row)
        if isinstance(candidate, str) and row.get("model_action") == "rename":
            raw_ok = all(ident in candidate for ident in identifiers)
            raw_eligible += 1
            raw_pass += raw_ok
            if row.get("mode") == "frozen_decision":
                truncation_loss += raw_ok and not after_ok
    return {"raw_candidate_eligible": raw_eligible, "raw_candidate_exact_pass": raw_pass,
            "prepared_after_eligible": after_eligible, "prepared_after_exact_pass": after_pass,
            "lost_by_preparation": truncation_loss if all(r.get("mode") == "frozen_decision" for r in rows) else None,
            "after_means": "prepared_counterfactual_title" if all(r.get("mode") == "frozen_decision" for r in rows) else "actual_stored_title",
            "case_sensitive": True, "not_a_semantic_score": True}


def decision_metrics(rows, corpus, scores, *, from_initial=False):
    valid = [r for r in rows if parser_valid(r)]
    reviewed = [s for r in valid if (s := review_for(r, scores)) is not None]
    wrong = missed = missed_review_unknown = keep_eligible = rename_eligible = 0
    for row in valid:
        case = corpus[row["case_id"]]
        before = case.get("current_title") if from_initial else row.get("title_before")
        changed = row.get("title_after") != before
        if case["expected_action"] == "keep":
            keep_eligible += 1
            wrong += changed
        else:
            rename_eligible += 1
            score = review_for(row, scores)
            if not changed or (score is not None and not score["mainline"]):
                missed += 1
            elif score is None:
                missed_review_unknown += 1
    attempts = [a for row in rows for a in row.get("attempts") or []]
    candidates = [r for r in valid if isinstance(raw_candidate(r), str)]
    return {
        "attempted": sum(bool(r.get("attempts")) for r in rows),
        "call_attempts": len(attempts), "successful": sum(successful(r) for r in rows),
        "parser_valid": len(valid), "reviewed": len(reviewed),
        "failures": sum(not parser_valid(r) for r in rows),
        "failure_attempt_types": dict(Counter(a.get("error_type") if a.get("error_type") in SAFE_ERRORS else "unclassified"
                                              for a in attempts if a.get("status") != "success")),
        "parser_invalid_completion_attempts": sum(a.get("status") == "success" and not a.get("parser_valid") for a in attempts),
        "transport_counts": dict(Counter("real_http" if a.get("transport_kind") == "real_http" else "mock_or_replay" for a in attempts)),
        "wrong_rename": wrong, "keep_evaluable": keep_eligible,
        "missed_rename": missed, "rename_evaluable": rename_eligible,
        "missed_rename_review_unknown": missed_review_unknown,
        "blind": {field: {"pass_or_flag_count": sum(s[field] for s in reviewed),
                           "reviewed": len(reviewed),
                           "rate": mean(s[field] for s in reviewed) if reviewed else None}
                  for field in GOOD_FIELDS + BAD_FIELDS},
        "blind_mean_score": mean(score_total(s) for s in reviewed) if reviewed else None,
        "identifiers": count_identifiers(rows, corpus),
        "candidate_lengths": {
            "renames_only": True, "count": len(candidates),
            "raw_mean_characters": mean(len(raw_candidate(r)) for r in candidates) if candidates else None,
            "raw_mean_display_width": mean(display_width(raw_candidate(r)) for r in candidates) if candidates else None,
            "prepared_mean_characters": mean(len(r.get("title_after") or "") for r in candidates) if candidates else None,
            "prepared_mean_display_width": mean(display_width(r.get("title_after") or "") for r in candidates) if candidates else None,
            "preparation_changed_candidate": sum(raw_candidate(r) != r.get("title_after") for r in candidates)
                if all(r.get("mode") == "frozen_decision" for r in rows) else None,
        },
        "usage": usage_summary(attempts),
    }


def summarize_frozen(rows, corpus, scores):
    grouped = defaultdict(list)
    for row in rows:
        grouped[group_key(row)].append(row)
    models = sorted({r["requested_model"] for r in rows})
    revisions = sorted({r["revision"] for r in rows})
    groups = []
    for key in product(models, revisions, VARIANTS, STYLES, STRATEGIES):
        selected = grouped[key]
        case_ids = [r["case_id"] for r in selected]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Duplicate case/cell/model/revision; replace superseded rows before summarizing")
        groups.append({**group_metadata(key), "eligible": len(corpus), "recorded": len(selected),
                       "missing": len(corpus) - len(selected),
                       **decision_metrics(selected, corpus, scores)})
    return groups


def summarize_pairs(rows, scores):
    paired = defaultdict(dict)
    for row in rows:
        key = row["requested_model"], row["variant"], row["style"], row["strategy"], row["case_id"]
        if row["revision"] in paired[key]:
            raise ValueError("Duplicate paired observation")
        paired[key][row["revision"]] = row
    outcomes = defaultdict(Counter)
    for key, versions in paired.items():
        tally = outcomes[key[:4]]
        baseline, revised = versions.get("baseline"), versions.get("revised")
        tally["eligible"] += 1
        if baseline is None or revised is None:
            tally["missing_pair"] += 1
            continue
        if baseline.get("evidence_hash") != revised.get("evidence_hash") or baseline.get("context_budget") != revised.get("context_budget") or baseline.get("title_before") != revised.get("title_before"):
            tally["input_mismatch"] += 1
            continue
        tally["paired"] += 1
        if not parser_valid(baseline) or not parser_valid(revised):
            tally["operationally_incomplete"] += 1
            continue
        before, after = review_for(baseline, scores), review_for(revised, scores)
        if before is None or after is None:
            tally["review_missing"] += 1
            continue
        tally["reviewed_pairs"] += 1
        delta = score_total(after) - score_total(before)
        tally["win" if delta > 0 else "loss" if delta < 0 else "tie"] += 1
        diffs = [after[f] - before[f] for f in GOOD_FIELDS] + [before[f] - after[f] for f in BAD_FIELDS]
        tally["dimension_tradeoff"] += any(x > 0 for x in diffs) and any(x < 0 for x in diffs)
        tally["exact_title_unchanged"] += baseline.get("title_after") == revised.get("title_after")
    fields = ("eligible", "paired", "missing_pair", "input_mismatch", "operationally_incomplete",
              "review_missing", "reviewed_pairs", "win", "loss", "tie", "dimension_tradeoff", "exact_title_unchanged")
    return [{"requested_model": key[0], "variant": key[1], "style": key[2], "strategy": key[3],
             **{field: tally[field] for field in fields}} for key, tally in sorted(outcomes.items())]


def summarize_e2e(rows, corpus, scores):
    grouped, final = defaultdict(list), {}
    for row in rows:
        grouped[group_key(row)].append(row)
        key = group_key(row) + (row["case_id"],)
        if key not in final or int(row["prefix"]) >= int(final[key]["prefix"]):
            final[key] = row
    groups = []
    for key, transitions in sorted(grouped.items()):
        endpoints = [row for k, row in final.items() if k[:5] == key]
        metrics = decision_metrics(endpoints, corpus, scores, from_initial=True)
        metrics["usage"] = usage_summary([a for r in transitions for a in r.get("attempts") or []])
        groups.append({**group_metadata(key), "final_points": len(endpoints),
                       "transitions": len(transitions),
                       "all_prefix_call_attempts": sum(len(r.get("attempts") or []) for r in transitions),
                       "final_state_actions": dict(Counter(r.get("state_action", "unknown") for r in endpoints)),
                       "final_changed_from_initial": sum(r.get("title_after") != corpus[r["case_id"]].get("current_title") for r in endpoints),
                       "final_transition_changed": sum(bool(r.get("changed")) for r in endpoints),
                       "final_pending": sum(bool(r.get("pending")) for r in endpoints),
                       "final_prefix_counts": dict(Counter(str(int(r["prefix"])) for r in endpoints)),
                       **metrics})
    return groups


def public_attempts(rows, corpus, *, scope):
    for row in rows:
        for index, attempt in enumerate(row.get("attempts") or [], 1):
            route = attempt.get("model_route") or {}
            returned = attempt.get("response_model")
            raw_usage = attempt.get("usage") or {}
            blocked = scope == "sandbox_blocked"
            real_http = not blocked and attempt.get("transport_kind") == "real_http"
            request_id = safe_identifier(attempt.get("request_id"))
            if request_id and not re.match(r"^(?:chatcmpl-|resp_|req_|msg_|request[-_])", request_id):
                request_id = None
            yield {
                "scope": scope, "case_id": row["case_id"], "category": corpus[row["case_id"]]["category"],
                "revision": safe_identifier(row.get("revision")),
                "variant": row.get("variant"), "style": row.get("style"), "strategy": row.get("strategy"),
                "prefix": numeric(row.get("prefix")), "attempt": numeric(attempt.get("attempt")) or index,
                "requested_model": safe_identifier(row.get("requested_model") or route.get("model")),
                "returned_model": safe_identifier(returned),
                "request_id": request_id,
                "system_prompt_hash": safe_hash(attempt.get("system_prompt_hash")),
                "user_prompt_hash": safe_hash(attempt.get("user_prompt_hash")),
                "response_hash": safe_hash(attempt.get("response_hash")),
                "evidence_hash": safe_hash(row.get("evidence_hash")),
                "policy_revision_sha256": safe_hash(row.get("policy_revision_sha256")),
                "status": "blocked_local_network" if blocked else
                    "success" if attempt.get("status") == "success" else "error",
                "transport_kind": "blocked_local_network" if blocked else
                    "real_http" if attempt.get("transport_kind") == "real_http" else "mock_or_replay",
                "supplier_completion_received": real_http and attempt.get("status") == "success",
                "parser_valid": None if scope == "route_probe" else False if blocked else bool(attempt.get("parser_valid")),
                "usage": {field: numeric(raw_usage.get(field)) for field in USAGE_FIELDS},
                "elapsed_seconds": numeric(attempt.get("elapsed")),
                "model_action": attempt.get("model_action") if attempt.get("model_action") in SAFE_ACTIONS else None,
            }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--require-reviews", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    work, experiment, outputs = root / "work", root / "work/experiment", root / "outputs"
    cases = json.loads((work / "corpus.json").read_text())
    corpus = {case["case_id"]: case for case in cases}
    if len(corpus) != len(cases) or any(not re.fullmatch(r"[rs][0-9]{2}", c) for c in corpus):
        raise ValueError("Corpus IDs must be unique anonymous rNN/sNN IDs")
    if any(not re.fullmatch(r"[a-z0-9_]+", c["category"]) for c in cases):
        raise ValueError("Corpus categories must be safe anonymous labels")
    rows = read_jsonl(experiment / "baseline.jsonl") + read_jsonl(experiment / "revised.jsonl")
    # The other 128 initial rows are retained unchanged in revised.jsonl.
    # Only the 64 replaced concise observations are additional model calls.
    superseded = [row for row in read_jsonl(experiment / "revised-initial.jsonl")
                  if row.get("variant") == "concise"]
    blocked = read_jsonl(experiment / "sandbox-blocked.jsonl")
    e2e = [row for path in sorted(experiment.glob("*-e2e.jsonl")) for row in read_jsonl(path)]
    safety = read_jsonl(experiment / "safety.jsonl")
    safety_path = experiment / "safety-case.json"
    if safety and not safety_path.exists():
        raise ValueError("Safety recordings exist but their frozen case is not available yet")
    safety_case = json.loads(safety_path.read_text()) if safety_path.exists() else None
    safety_corpus = {safety_case["case_id"]: safety_case} if safety_case else {}
    probes = read_probe_rows(experiment / "probe-ledger.json")
    validate_rows(rows + superseded + blocked + e2e, corpus)
    validate_rows(safety, safety_corpus)
    score_case_ids = set(corpus) | set(safety_corpus)
    scores, review_meta = load_scores(experiment / "final-blind-map.json", experiment / "final-blind-scores.json", score_case_ids)
    if args.require_reviews and not review_meta["available"]:
        raise ValueError("Final blind review is required but not yet available")
    blocked_attempts = [a for row in blocked for a in row.get("attempts") or []]
    scope_rows = {"frozen": rows, "superseded": superseded, "e2e": e2e,
                  "heldout_synthetic": safety, "route_probe": probes}
    recorded_attempts = [a for selected in scope_rows.values() for row in selected for a in row.get("attempts") or []]
    supplier_attempts = [a for a in recorded_attempts if a.get("transport_kind") == "real_http"]
    public_corpus = {**corpus, **safety_corpus,
                     **{row["case_id"]: {"category": "route_probe"} for row in probes}}
    metrics = {
        "schema": 1,
        "corpus": {"cases": len(cases), "provenance_counts": dict(Counter(c["provenance"] for c in cases)),
                   "category_counts": dict(Counter(c["category"] for c in cases)),
                   "expected_action_counts": dict(Counter(c["expected_action"] for c in cases))},
        "review": review_meta,
        "frozen_decision_limit": "Counterfactual keep/rename at one frozen prefix; not scheduler confirmation or real-profile writeback.",
        "identifier_limit": "Exact case-sensitive reference identifiers; capitalization differences do not establish semantic failure.",
        "style_limit": "An accurate kept incumbent is not required to adopt a new style. Candidate length statistics cover renames only.",
        "cost_limit": "Only provider-reported cost is accepted. Unknown cost remains null; known sums are explicitly partial.",
        "frozen_cells": summarize_frozen(rows, corpus, scores),
        "paired_cells": summarize_pairs(rows, scores),
        "e2e_cells": summarize_e2e(e2e, corpus, scores),
        "heldout_synthetic": {
            "separate_from_main_matrix": True,
            "case_count": len(safety_corpus),
            "category": "pr_is_the_subject",
            "expected_action": "rename",
            "identifier_check": "Exact primary repository name and PR number in raw candidate and prepared title.",
            "cells": summarize_frozen(safety, safety_corpus, scores) if safety else [],
            "paired_cells": summarize_pairs(safety, scores) if safety else [],
        },
        "sandbox_blocked": {"records": len(blocked), "attempts": len(blocked_attempts),
                            "supplier_completion_received": 0, "excluded_from_supplier_quality_and_usage": True},
        "calls": {"recorded_nonblocked_attempts": len(recorded_attempts),
                  "mock_or_replay_attempts": len(recorded_attempts) - len(supplier_attempts),
                  "supplier_route_attempts": len(supplier_attempts),
                  "successful_supplier_completions": sum(a.get("status") == "success" for a in supplier_attempts),
                  "frozen_attempts": sum(len(r.get("attempts") or []) for r in rows),
                  "e2e_attempts": sum(len(r.get("attempts") or []) for r in e2e),
                  "by_scope": {scope: scope_metrics(selected) for scope, selected in scope_rows.items()},
                  "planned_successful_calls": {"frozen": 384, "superseded": 64, "e2e": 64,
                                               "heldout_synthetic": 48, "route_probe": 2, "total": 562},
                  "usage": usage_summary(supplier_attempts)},
    }
    outputs.mkdir(exist_ok=True)
    (outputs / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=True, indent=2) + "\n")
    with (outputs / "call-ledger.jsonl").open("w") as file:
        for scope, selected in (*scope_rows.items(), ("sandbox_blocked", blocked)):
            for record in public_attempts(selected, public_corpus, scope=scope):
                file.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
    print("Wrote safe aggregate metrics and metadata-only call ledger.")


if __name__ == "__main__":
    main()
