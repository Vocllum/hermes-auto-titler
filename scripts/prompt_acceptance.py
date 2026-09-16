"""Real-session title prompt experiment.

This is an evaluation harness, not a synthetic acceptance test. It samples existing
sessions without writing them, replays the real conversation one user turn at a
time, and compares isolated prompt profiles on the same prefixes. An LLM judge
scores the resulting titles against the visible prefix. The original session title
is never shown to the generator or used as a lexical oracle.

Examples:
  PYTHONPATH=/path/to/hermes-agent .venv/bin/python scripts/prompt_acceptance.py
  PYTHONPATH=/path/to/hermes-agent .venv/bin/python scripts/prompt_acceptance.py --n 6 --seed 17
  PYTHONPATH=/path/to/hermes-agent .venv/bin/python scripts/prompt_acceptance.py --variants minimal,concise,detailed --styles both
  PYTHONPATH=/path/to/hermes-agent .venv/bin/python scripts/prompt_acceptance.py --output /tmp/title-experiment.jsonl

The transport is intentionally explicit and read-only: it talks to the configured
OpenAI-compatible endpoint, while prompt construction and context sampling come
from the real hermes-auto-titler code path.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from types import SimpleNamespace
from typing import Any, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_auto_titler.config import VALID_STRATEGIES, VALID_STYLES, load_config
from hermes_auto_titler.messages import (
    _sample_turns,
    clean_captured_text,
    is_summary,
    is_system_noise,
    message_text,
    sample_user_messages,
    smart_preview,
)
from hermes_auto_titler.policy import AutoTitler


VARIANTS = ("minimal", "concise", "detailed")
INPUT_VARIANTS = ("current", "minimal")
CONTEXTS = ("production", "lean")
_SCORE_FIELDS = ("coverage", "specificity", "faithfulness", "brevity", "language")


@dataclass(frozen=True)
class Sample:
    session_id: str
    original_title: str
    turns: list[list[tuple[str, str]]]


@dataclass(frozen=True)
class Prefix:
    number: int
    turns: list[list[tuple[str, str]]]
    recent: list[tuple[str, str]]
    users: list[tuple[str, str]]
    opening: list[tuple[str, str]]


class HttpLlm:
    """Small OpenAI-compatible client used only by this read-only experiment."""

    def __init__(self, *, endpoint: str, model: str, api_key: str, timeout: float = 60.0):
        self.endpoint = endpoint.rstrip("/") + "/chat/completions"
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.calls = 0

    def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> SimpleNamespace:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            # Title JSON must have enough completion budget for reasoning-capable
            # routes; the production policy uses 64, but this harness measures
            # prompt quality rather than production token-budget behavior.
            "max_tokens": int(kwargs.get("max_tokens") or 256),
        }
        if kwargs.get("response_format"):
            payload["response_format"] = kwargs["response_format"]
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        self.calls += 1
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read(600).decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM endpoint returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM endpoint unavailable: {exc.reason}") from exc
        try:
            data = json.loads(body)
            text = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("LLM endpoint returned an unexpected completion shape") from exc
        return SimpleNamespace(text=str(text), usage=data.get("usage") or {})


class PluginLlmAdapter:
    """Use Hermes' actual plugin route, including task/provider attribution."""

    def __init__(self, llm: Any):
        self.llm = llm
        self.calls = 0

    def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> SimpleNamespace:
        self.calls += 1
        response = self.llm.complete(messages, **kwargs)
        return SimpleNamespace(
            text=str(getattr(response, "text", "") or ""),
            usage=getattr(response, "usage", {}) or {},
        )

    def complete_structured(self, *, messages: list[dict[str, Any]], **kwargs: Any) -> SimpleNamespace:
        return self.complete(messages, **kwargs)


class Ctx:
    def __init__(self, llm: Any):
        self.llm = llm


def _host_route() -> tuple[str, str, str]:
    """Return configured endpoint, model, and key environment variable name."""
    endpoint = os.environ.get("HERMES_AUTOTITLER_EXPERIMENT_BASE_URL", "").strip()
    model = os.environ.get("HERMES_AUTOTITLER_EXPERIMENT_MODEL", "").strip()
    key_env = os.environ.get("HERMES_AUTOTITLER_EXPERIMENT_KEY_ENV", "").strip()
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
        model_cfg = config.get("model") or {}
        if isinstance(model_cfg, str):
            model_cfg = {"default": model_cfg}
        endpoint = endpoint or str(model_cfg.get("base_url") or "").strip()
        model = model or str(model_cfg.get("default") or "").strip()
        key_env = key_env or str(model_cfg.get("key_env") or "").strip()
    except Exception:
        pass
    if not endpoint:
        raise SystemExit("No endpoint configured; set HERMES_AUTOTITLER_EXPERIMENT_BASE_URL")
    if not model:
        raise SystemExit("No model configured; set HERMES_AUTOTITLER_EXPERIMENT_MODEL")
    if not key_env:
        key_env = "OPENAI_API_KEY"
    return endpoint, model, key_env


def _make_host_llm() -> tuple[Any, str]:
    """Build the host-owned client when no raw endpoint override is requested."""
    try:
        from agent.plugin_llm import PluginLlm  # type: ignore[import-not-found]

        return PluginLlmAdapter(PluginLlm(plugin_id="hermes-auto-titler")), "host"
    except Exception as exc:
        raise SystemExit(f"Hermes plugin LLM route unavailable: {exc}") from exc


def _make_raw_llm(endpoint: str, model: str, api_key: str) -> HttpLlm:
    return HttpLlm(endpoint=endpoint, model=model, api_key=api_key)


def _call_count(client: Any) -> int:
    return int(getattr(client, "calls", 0) or 0)


def _flatten(turns: Sequence[Sequence[tuple[str, str]]]) -> list[tuple[str, str]]:
    return [item for turn in turns for item in turn]


def extract_turns(messages: Iterable[dict[str, Any]], *, preview_chars: int = 0) -> list[list[tuple[str, str]]]:
    """Decode a real transcript into clean user turns plus the final assistant reply."""
    pairs: list[tuple[str, str]] = []
    for message in messages:
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        text = clean_captured_text(message_text(message.get("content")))
        if text and not is_summary(text) and not is_system_noise(text):
            pairs.append((role, text))

    def preview(text: str) -> str:
        return smart_preview(text, preview_chars) if preview_chars > 0 else text

    return _sample_turns(pairs, preview)


def build_prefixes(turns: list[list[tuple[str, str]]], raw_config: dict[str, Any], spec: str) -> list[Prefix]:
    """Build unique chronological prefixes using the production context budgets."""
    if not turns:
        return []
    requested: list[int] = []
    for token in (part.strip().lower() for part in spec.split(",")):
        if not token:
            continue
        if token == "all":
            requested.append(len(turns))
        else:
            try:
                requested.append(int(token))
            except ValueError as exc:
                raise SystemExit(f"Invalid --prefixes value: {token!r}") from exc
    if not requested:
        requested = [1, 2, 4, len(turns)]
    points = sorted({max(1, min(len(turns), n)) for n in requested})

    opening_k = max(0, int(raw_config.get("opening_turns", 2)))
    recent_k = max(0, int(raw_config.get("recent_turns", 2)))
    preview_k = max(0, int(raw_config.get("preview_chars", 400)))
    user_preview_k = max(0, int(raw_config.get("user_message_preview_chars", 300)))
    user_limit = int(raw_config.get("user_message_threshold", 40))
    result: list[Prefix] = []
    for number in points:
        prefix_turns = turns[:number]
        sampled = _sample_turns(_flatten(prefix_turns), lambda text: smart_preview(text, preview_k))
        opening = _flatten(sampled[:opening_k])
        recent = _flatten(sampled[-recent_k:]) if recent_k else []
        users: list[tuple[str, str]] = []
        if raw_config.get("include_all_user_messages", True):
            users = [(role, text) for role, text in _flatten(prefix_turns) if role == "user"]
            if user_preview_k > 0:
                users = [(role, smart_preview(text, user_preview_k)) for role, text in users]
            users = sample_user_messages(users, user_limit)
        result.append(Prefix(number, prefix_turns, recent, users, opening))
    return result


def _session_candidates(db: Any, *, rng: random.Random, n: int, min_users: int, max_users: int, sample: str = "random") -> list[Sample]:
    rows = list(db.list_sessions_rich(limit=5000, include_children=False, order_by_last_active=True))
    if sample == "random":
        rng.shuffle(rows)
    else:
        rows.sort(key=lambda row: str(row.get("last_active") or row.get("created_at") or ""), reverse=True)
    # Skip user-owned titles in the experiment: the generator must be evaluated
    # on sessions where an automatic title may actually be replaced.
    selected: list[Sample] = []
    for row in rows:
        if len(selected) >= n:
            break
        sid = row.get("id")
        if not sid:
            continue
        try:
            source = db.get_session_title_source(str(sid))
            if source == getattr(db, "TITLE_SOURCE_USER", "user"):
                continue
            messages = db.get_messages_as_conversation(sid, include_ancestors=True) or []
            turns = extract_turns(messages)
        except Exception:
            continue
        if not (min_users <= sum(1 for turn in turns for role, _ in turn if role == "user") <= max_users):
            continue
        if len(turns) < min_users:
            continue
        selected.append(Sample(str(sid), str(row.get("title") or ""), turns))
    return selected


def _sample_fingerprint(samples: Sequence[Sample]) -> list[str]:
    return [sample.session_id for sample in samples]


def _context_config(base: dict[str, Any], context: str) -> dict[str, Any]:
    config = dict(base)
    if context == "lean":
        config.update({
            "opening_turns": 1,
            "recent_turns": 2,
            "preview_chars": 100,
            "include_all_user_messages": False,
        })
    return config


def _experiment_config(
    base: dict[str, Any], *, variant: str, input_variant: str, style: str, strategy: str
) -> dict[str, Any]:
    config = dict(base)
    # This key is intentionally not a public config setting. It is an isolated
    # harness selector consumed by policy.AutoTitler._generate only.
    config["prompt_variant"] = variant
    config["input_variant"] = input_variant
    config["title_style"] = style
    config["strategy"] = strategy
    config["rename_confirmations"] = 0
    return config


def generate_title(titler: AutoTitler, prefix: Prefix) -> tuple[str, str | None]:
    # Give reasoning-capable experimental routes room to finish the JSON. This
    # intentionally does not alter the production ``max_tokens=64`` contract.
    titler.cfg["experiment_max_tokens"] = 256
    action, title = titler._generate(
        None,
        prefix.recent,
        prefix.users,
        prefix.opening,
        blind=True,
        session_id=None,
    )
    return action, title


def _clip(text: str, limit: int = 900) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _json_object(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def parse_score(text: str, candidate_count: int) -> list[dict[str, Any]]:
    """Parse judge output and normalize each dimension to 1..5."""
    data = _json_object(text)
    raw = data.get("scores") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raise ValueError("judge response has no scores list")
    by_index: dict[int, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        values: dict[str, Any] = {"index": index}
        for field in _SCORE_FIELDS:
            try:
                values[field] = max(1, min(5, int(round(float(item.get(field, 1))))))
            except (TypeError, ValueError):
                values[field] = 1
        values["total"] = sum(values[field] for field in _SCORE_FIELDS)
        values["note"] = _clip(str(item.get("note") or ""), 240)
        by_index[index] = values
    return [by_index.get(i, {"index": i, **{field: 1 for field in _SCORE_FIELDS}, "total": len(_SCORE_FIELDS), "note": "missing judge score"}) for i in range(1, candidate_count + 1)]


def judge_titles(llm: Any, prefix: Prefix, candidates: list[tuple[str, str]]) -> list[dict[str, Any]]:
    transcript = "\n".join(
        f"{role}: {_clip(text)}" for role, text in _flatten(prefix.turns)
    )
    labels = "\n".join(f"{i}. {title or '(empty)'}" for i, (_, title) in enumerate(candidates, 1))
    system = (
        "Evaluate candidate chat-session titles. Return JSON only in the form "
        '{"scores":[{"index":1,"coverage":1,"specificity":1,"faithfulness":1,"brevity":1,"language":1,"note":""}]}.'
        " Score every candidate from 1 to 5. Coverage: represents the visible durable subject and goal. "
        "Specificity: useful for finding the session again without vague wording. Faithfulness: uses only evidence "
        "in the visible prefix and does not overclaim. Brevity: works as a sidebar title. Language: matches the "
        "user's language and preserves meaningful identifiers. Do not reward a title for mentioning a detail that "
        "is merely an implementation step. Do not compare candidates to the original session title."
    )
    user = f"Visible prefix:\n{transcript}\n\nCandidates:\n{labels}"
    response = llm.complete([
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], temperature=0, max_tokens=max(512, 160 * len(candidates)), timeout=60, purpose="auto-title-experiment-judge")
    return parse_score(response.text, len(candidates))


def _with_retry(fn, retries: int = 1):
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception:
            if attempt >= retries:
                raise
    raise RuntimeError("retry loop exhausted")  # pragma: no cover


def aggregate_records(
    records: Sequence[dict[str, Any]], *, group_by: str = "variant"
) -> dict[str, dict[str, float | int]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        group = str(record.get(group_by) or "")
        score = record.get("score") or {}
        if (
            group
            and isinstance(score, dict)
            and "total" in score
            and record.get("status", "ok") == "ok"
        ):
            grouped.setdefault(group, []).append({**score, "title": str(record.get("title") or "")})
    result: dict[str, dict[str, float | int]] = {}
    for group, scores in grouped.items():
        result[group] = {
            "n": len(scores),
            "mean_total": round(mean(float(s.get("total", 0)) for s in scores), 2),
            "mean_title_chars": round(mean(len(str(s.get("title") or "")) for s in scores), 2),
            **{f"mean_{field}": round(mean(float(s.get(field, 0)) for s in scores), 2) for field in _SCORE_FIELDS},
        }
    return result


def _experiment_label(*, variant: str, input_variant: str, context: str, style: str, strategy: str) -> str:
    return f"prompt={variant}|input={input_variant}|context={context}|style={style}|strategy={strategy}"


def _selection(spec: str, allowed: Sequence[str], flag: str) -> list[str]:
    values = [value.strip() for value in spec.split(",") if value.strip()]
    unknown = set(values) - set(allowed)
    if not values or unknown:
        raise SystemExit(f"Unknown {flag} value(s): {', '.join(sorted(unknown)) or '(empty)'}; choose {', '.join(allowed)}")
    return values


def _evolution_changes(records: Sequence[dict[str, Any]]) -> int:
    ordered = sorted(records, key=lambda record: int(record.get("prefix", 0)))
    titles = [str(record.get("title") or "") for record in ordered if record.get("status") == "ok"]
    return sum(before != after for before, after in zip(titles, titles[1:]))


def _mean_evolution_changes(records: Sequence[dict[str, Any]]) -> float:
    by_session: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_session.setdefault(str(record.get("session_id") or ""), []).append(record)
    if not by_session:
        return 0.0
    return round(mean(_evolution_changes(group) for group in by_session.values()), 3)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score isolated title prompts on random real-session prefixes")
    parser.add_argument("--n", type=int, default=4, help="number of eligible real sessions")
    parser.add_argument("--sample", choices=("random", "recent"), default="random", help="sample random sessions or newest eligible sessions")
    parser.add_argument("--seed", type=int, default=17, help="random sampling seed")
    parser.add_argument("--min-users", type=int, default=3, help="minimum real user turns per sampled session")
    parser.add_argument("--max-users", type=int, default=80, help="maximum real user turns per sampled session")
    parser.add_argument("--prefixes", default="1,2,4,all", help="chronological prefix sizes; use all for the final prefix")
    parser.add_argument("--variants", default="minimal,concise,detailed", help="comma-separated prompt profiles")
    parser.add_argument("--input-variants", default="current,minimal", help="comma-separated input layouts")
    parser.add_argument("--contexts", default="production,lean", help="comma-separated context budget profiles")
    parser.add_argument("--styles", choices=("concise", "complete", "both"), default="concise", help="title style to compare")
    parser.add_argument("--strategies", default="conservative,aggressive", help="comma-separated decision strategies")
    parser.add_argument("--output", help="optional JSONL output path")
    parser.add_argument("--no-judge", action="store_true", help="generate titles but skip model scoring")
    parser.add_argument("--judge-model", help="optional separate model for scoring; defaults to the generation model")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    variants = _selection(args.variants, VARIANTS, "prompt variant")
    input_variants = _selection(args.input_variants, INPUT_VARIANTS, "input variant")
    contexts = _selection(args.contexts, CONTEXTS, "context profile")
    strategies = _selection(args.strategies, tuple(VALID_STRATEGIES), "strategy")
    styles = list(VALID_STYLES) if args.styles == "both" else [args.styles]
    explicit_route = bool(
        os.environ.get("HERMES_AUTOTITLER_EXPERIMENT_BASE_URL")
        or os.environ.get("HERMES_AUTOTITLER_EXPERIMENT_MODEL")
        or os.environ.get("HERMES_AUTOTITLER_EXPERIMENT_KEY_ENV")
    )
    if explicit_route:
        endpoint, model, key_env = _host_route()
        api_key = os.environ.get(key_env, "").strip()
        if not api_key:
            raise SystemExit(f"Environment variable {key_env} is not available; load Hermes credentials before running the experiment")
        llm: Any = _make_raw_llm(endpoint, model, api_key)
        judge_llm: Any = _make_raw_llm(
            endpoint,
            args.judge_model.strip() if args.judge_model else model,
            api_key,
        )
        route_label = f"raw:{model}"
    else:
        llm, route_label = _make_host_llm()
        judge_llm, _ = _make_host_llm()
        endpoint, model = "host-owned", "title_generation"

    from hermes_state import SessionDB

    db = SessionDB()
    rng = random.Random(args.seed)
    samples = _session_candidates(db, rng=rng, n=max(0, args.n), min_users=max(1, args.min_users), max_users=max(args.min_users, args.max_users), sample=args.sample)
    if not samples:
        raise SystemExit("No eligible real sessions found for the requested bounds")
    base = load_config()
    records: list[dict[str, Any]] = []
    output = open(args.output, "w", encoding="utf-8") if args.output else None
    failures = 0
    try:
        print(f"# real-session prompt experiment sessions={len(samples)} seed={args.seed} route={route_label} model={model} endpoint={endpoint}")
        print(f"# prompts={','.join(variants)} inputs={','.join(input_variants)} contexts={','.join(contexts)} styles={','.join(styles)} strategies={','.join(strategies)} prefixes={args.prefixes}")
        if output:
            output.write(json.dumps({
                "kind": "run",
                "seed": args.seed,
                "sample_fingerprint": _sample_fingerprint(samples),
                "base_context_config": {key: base.get(key) for key in (
                    "opening_turns", "recent_turns", "preview_chars", "include_all_user_messages",
                    "user_message_threshold", "user_message_preview_chars", "max_title_length",
                    "max_display_width",
                )},
                "variables": {"prompt": variants, "input": input_variants, "context": contexts,
                              "style": styles, "strategy": strategies, "prefixes": args.prefixes},
            }, ensure_ascii=False) + "\n")
        for sample_index, sample in enumerate(samples, 1):
            print(f"\n=== session {sample_index}/{len(samples)} id={sample.session_id} users={sum(1 for t in sample.turns for r, _ in t if r == 'user')} ===")
            print(f"original_title (hidden from candidates): {_clip(sample.original_title, 140) or '(empty)'}")
            sample_records: list[dict[str, Any]] = []
            prefix_by_key: dict[tuple[str, int], Prefix] = {}
            for context in contexts:
                context_cfg = _context_config(base, context)
                prefixes = build_prefixes(sample.turns, context_cfg, args.prefixes)
                for prefix in prefixes:
                    prefix_by_key[(context, prefix.number)] = prefix
                    for style in styles:
                        for strategy in strategies:
                            for input_variant in input_variants:
                                for variant in variants:
                                    label = _experiment_label(
                                        variant=variant, input_variant=input_variant, context=context,
                                        style=style, strategy=strategy,
                                    )
                                    titler = AutoTitler(
                                        Ctx(llm),
                                        _experiment_config(
                                            context_cfg, variant=variant, input_variant=input_variant,
                                            style=style, strategy=strategy,
                                        ),
                                        db=None,
                                    )
                                    try:
                                        action, title = _with_retry(lambda: generate_title(titler, prefix), retries=1)
                                    except Exception as exc:
                                        failures += 1
                                        print(f"WARN {label} prefix={prefix.number}: generation failed: {exc}")
                                        continue
                                    record: dict[str, Any] = {
                                        "kind": "result",
                                        "session_id": sample.session_id,
                                        "prefix": prefix.number,
                                        "variant": variant,
                                        "input_variant": input_variant,
                                        "context": context,
                                        "style": style,
                                        "strategy": strategy,
                                        "experiment": label,
                                        "action": action,
                                        "title": title or "",
                                        "status": "ok" if action != "error" else "generation_error",
                                    }
                                    if action == "error":
                                        failures += 1
                                    records.append(record)
                                    sample_records.append(record)
                                    print(f"  generated prefix={prefix.number:<3} {label} title={title or '(empty)'}")

            if not args.no_judge:
                # Score every candidate at the same chronological prefix together. Candidate
                # order is shuffled deterministically so prompt profiles do not inherit a
                # permanent position advantage. Context profile only changes what the generator
                # saw; the judge always sees the full real prefix.
                prefix_numbers = sorted({int(record["prefix"]) for record in sample_records})
                for prefix_number in prefix_numbers:
                    group = [r for r in sample_records if r["prefix"] == prefix_number and r.get("status") == "ok"]
                    if not group:
                        continue
                    judge_prefix = max(
                        (prefix for (context, number), prefix in prefix_by_key.items() if number == prefix_number),
                        key=lambda prefix: len(_flatten(prefix.turns)),
                    )
                    shuffled = list(group)
                    random.Random(f"{args.seed}:{sample.session_id}:{prefix_number}").shuffle(shuffled)
                    try:
                        scored = _with_retry(
                            lambda: judge_titles(judge_llm, judge_prefix, [(r["experiment"], str(r["title"])) for r in shuffled]),
                            retries=1,
                        )
                    except Exception as exc:
                        failures += 1
                        print(f"WARN prefix={prefix_number}: judge failed: {exc}")
                        continue
                    for record, score in zip(shuffled, scored):
                        record["score"] = score
                    print(f"  scored prefix={prefix_number} candidates={len(shuffled)}")

            for label in sorted({str(record["experiment"]) for record in sample_records}):
                evolution = [record for record in sample_records if record["experiment"] == label]
                chain = " → ".join(f"{record['prefix']}:{record['title'] or '(empty)'}" for record in sorted(evolution, key=lambda record: int(record["prefix"])))
                print(f"  evolution changes={_evolution_changes(evolution)} {label} :: {chain}")
            if output:
                for record in sample_records:
                    output.write(json.dumps(record, ensure_ascii=False) + "\n")
                output.flush()
    finally:
        if output:
            output.close()
        db.close()

    summary = aggregate_records(records, group_by="experiment")
    print(f"\n=== aggregate generation_calls={_call_count(llm)} judge_calls={_call_count(judge_llm)} ===")
    if summary:
        for label, metrics in sorted(summary.items(), key=lambda item: float(item[1]["mean_total"]), reverse=True):
            subset = [record for record in records if record.get("experiment") == label]
            print(f"{label} mean_changes={_mean_evolution_changes(subset)} " + " ".join(f"{key}={value}" for key, value in metrics.items()))
    elif args.no_judge:
        print("No judge scores requested; inspect generated titles or rerun without --no-judge.")
    else:
        print("No scores were returned; the transport or judge response needs inspection.")
    if failures:
        print(f"failures={failures} (excluded from quality aggregates)")
        return 2
    if not args.no_judge and not summary:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
