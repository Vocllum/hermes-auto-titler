# Paired Prompt Evaluation

`eval.paired` compares prompt changes without opening or updating a live Hermes
database. It accepts an already-reviewed corpus and an injected LLM client. It
does not discover credentials, select providers, redact transcripts, or start
network requests on its own.

This database isolation is not a promise of zero host-directory side effects:
when the full Hermes SDK is imported, its initialization or home/path helpers
may create profile directories. Use a separate process with an isolated
`HERMES_HOME` set **before imports** when using that SDK. Do not temporarily
change the home setting or patch global helpers inside parallel frozen runs.

## Three Different Experiments

| Experiment | Entry point | What it measures | What it does not establish |
| --- | --- | --- | --- |
| Generation only | `scripts/prompt_acceptance.py` | Titles generated independently at each visible prefix | Incumbent preservation, false/missed renames, production evolution |
| Frozen decision | `run_frozen()` | Counterfactual keep/rename/approve decisions with the same incumbent and evidence | Actual database writes, title authority, confirmation or scheduler behavior |
| Synchronous evolution | `run_evolution()` | Production `evaluate()` decisions, pending review, candidate cleaning and isolated SQLite writes | Live host hooks, scheduling, full-fidelity compaction or live activation |

In a frozen decision, `title_after` is a hypothetical outcome, not a persisted
title. The generator is deliberately called directly, so a case marked with a
user-owned title does **not** receive the production authority gate. Exclude such
cases from automatic-rename quality conclusions. Test authority protection with
`run_evolution()`, which invokes `evaluate()` and skips protected titles without
calling the model.

First naming is a separate task: `current_title=None` or a `derived` source makes
the frozen generator use the rename-only contract. An incumbent-only corpus
cannot establish first-naming performance, even if deterministic tests cover that
branch.

## Matrix and Frozen Inputs

`make_prompt_cells()` returns the complete 12-cell product:

- `variant`: `minimal`, `concise`, `detailed`.
- `style`: `concise`, `complete`.
- `strategy`: `conservative`, `aggressive`.

The selector remains experimental; it does not expose a new production setting.
For a before/after comparison, keep the corpus, human-turn prefixes, incumbent,
proposed title, language rule, context budgets, client settings and retry policy
fixed. The production sampler builds each input; the prompt revision and the
three registered dimensions are the intended variables. Do not change input
layout or context budgets at the same time as a prompt change.

Run the same cells on each requested model. Record both requested and returned
model identities; route names alone do not prove the exact backend snapshot.
Freeze the source revision used to instantiate each `titler_class`. The
`revision` argument is an evidence label, not an automatic source-code verifier.

The number of logical frozen decisions is:

```text
cases x prefixes per case x 12 cells x models x prompt revisions
```

Actual request attempts can exceed that count when retries occur. Report attempts
and successful decisions separately. Repeated prefixes of the same session are
correlated observations, not independent sessions.

## Reusable API

```python
from eval.paired import (
    FrozenCase,
    PromptCell,
    export_blind,
    load_cases,
    make_prompt_cells,
    run_evolution,
    run_frozen,
)
```

`FrozenCase` requires `case_id`, `events` and `prefix`, where `prefix` is a valid
integer human-turn number, not a boolean or fractional value. It also accepts:

| Field | Default | Purpose |
| --- | --- | --- |
| `current_title` | `None` | Fixed incumbent for counterfactual decisions and initial sandbox title |
| `proposed_title` | `None` | Fixed candidate for frozen review calls |
| `initial_title_source` | `llm` | Explicit initial provenance; also accepts `derived`, `user`, or `None` |
| `provenance` | `synthetic` | `real_redacted`, `synthetic`, or `synthetic_adversarial` |
| `category` | `unspecified` | Pre-registered scenario label |

`load_cases(path)` reads a JSON array of these fields and rejects duplicate case
IDs. It validates structure, **not** whether sensitive content has been removed
or whether claimed provenance is true. Initial title provenance must come from
independent evidence, not the final title source of a historical session.

Common call options are `revision`, `config`, `transport_kind`, `titler_class` and
`language_rule`. The default language rule follows the primary natural language
of user messages instead of consulting live host configuration. Supply an
explicit rule to test a fixed locale. Keep that rule identical across matched
cells and revisions.

The injected client implements:

```python
response = llm.complete(messages=messages, **kwargs)
# response.text: model output as a string
# response.usage: provider usage object/dict, or None
# response.model / response.provider / response.id: optional metadata
```

Production request arguments are forwarded. If the client applies its own output
budget, reasoning settings or retries, record those effective settings separately
and keep them fixed. Disable hidden client retries if every network attempt must
appear in the harness ledger.

`run_frozen(..., max_attempts=1)` returns one decision record, including every
observed attempt. Each run owns a separate tracer, so frozen runs may be scheduled
independently; a shared client must itself be concurrency-safe. Retries repeat
the same frozen evidence and incumbent. An HTTP-successful response can still
fail JSON parsing or first-naming requirements.

`run_evolution(..., workdir, prefixes=[...])` returns a transition list. Prefixes
must be unique, increasing human-turn numbers. Runs should be serial within each
session. Each invocation creates a new sandbox directory; its SQLite/state files
remain available for inspection. A supplied `proposed_title` is only for frozen
decisions; evolution creates its own pending candidates through the state machine.

## No-Network Example

Run this Python example in a development environment where the Hermes modules
and this repository are importable. All messages and responses here are
synthetic. The script does not make model calls.

```python
from pathlib import Path
from types import SimpleNamespace

from eval.paired import FrozenCase, export_blind, make_prompt_cells, run_frozen, run_evolution


class ScriptedClient:
    model = "scripted-test"
    provider = "local-test"

    def __init__(self, *responses):
        self.responses = iter(responses)

    def complete(self, messages, **kwargs):
        return SimpleNamespace(
            text=next(self.responses),
            model=self.model,
            provider=self.provider,
            usage=None,
        )


sample = FrozenCase(
    case_id="synthetic-001",
    events=[
        {"role": "user", "content": "Build a documentation search tool."},
        {"role": "user", "content": "Measure search quality before choosing an index."},
    ],
    prefix=2,
    current_title="Documentation search",
    provenance="synthetic",
    category="broad_goal_with_subtask",
)

records = [
    run_frozen(
        sample, cell, ScriptedClient('{"action":"keep"}'),
        revision="example-baseline", transport_kind="mock",
    )
    for cell in make_prompt_cells()
]

export_blind(
    records, Path("private-evaluation/review.json"),
    Path("private-evaluation/mapping.json"), cases=[sample],
)

transitions = run_evolution(
    sample, make_prompt_cells()[0],
    ScriptedClient(
        '{"action":"rename","title":"Documentation search evaluation"}',
        '{"action":"approve"}',
    ),
    Path("private-evaluation/sandboxes"),
    revision="example-baseline", prefixes=[1, 2], transport_kind="mock",
)
assert [row["state_action"] for row in transitions] == ["pending", "renamed"]
```

The deterministic API tests run without a provider or credentials:

```sh
python -m pytest tests/test_eval_paired.py tests/test_eval_trace.py -q
```

For real inference, replace `ScriptedClient` with an explicitly authorized client
and set `transport_kind="real_http"`. There is intentionally no credential or
provider-discovery example here.

## Real Calls, Recorded Replay and Usage

`transport_kind` describes the injected transport, not proof that inference
occurred:

- `real_http`: attempted external requests. Count transport failures, blocked
  requests and parsing failures separately from successful model decisions.
- `mock`: scripted test responses. These establish harness behavior, not model
  quality or production cost.
- `recorded_replay`: previously captured responses replayed locally. No new
  inference occurred, and recorded token counts must not be billed again.

The ledger includes prompt hashes, section lengths, request metadata, returned
model/provider, response hash, elapsed time, parse status, and normalized usage.
It preserves failed attempts instead of excluding them from the denominator.
No error-response body or raw prompt is included in that ledger, but candidate
titles and case metadata can still be sensitive.

Usage supports host objects, OpenAI-compatible dictionaries, and native Gemini
token-field names. Missing values remain `null`:

- `measured`: both input and output tokens were supplied.
- `partial`: some usage fields were supplied, but the input/output pair is incomplete.
- `unavailable`: no usable usage fields were supplied.

Reasoning and cache counts remain separate fields. Native Gemini candidate tokens
can exclude reasoning tokens; use the supplied total and reasoning fields rather
than assuming all providers use identical accounting. Do not add reasoning tokens
to a provider's output-token total if that total already includes them.

Report measured-call coverage, missing-usage counts, and known token totals as a
lower bound when any attempt lacks usage. Character lengths and guessed tokens
are not measured billing. A reported `cost_usd` field must be qualified by the
client's accounting semantics; provider-reported usage is not automatically a
settled invoice. If prices are used to estimate cost, publish the rate source,
date, currency and estimate separately. Unknown costs remain unknown.

## Blind Review and Privacy

Freeze prefix-level expected mainlines, legitimate shifts, necessary identifiers
and language criteria before generating candidates. Review semantic faithfulness,
durable subject coverage, local-task takeover, language, identifier fidelity and
sidebar suitability; a small exact-title whitelist is not a semantic oracle.
Distinguish independent agent review from actual human review in reports.

`export_blind()` shuffles groups and candidates, omits model/cell/revision labels,
and writes a private candidate-to-record mapping. Passing `cases` includes only
role/content evidence at the visible prefix. Reviewers must not see the mapping
until judgments are frozen. Keep training/tuning examples separate from held-out
validation and retain disagreements rather than silently resolving them in favor
of a prompt change.

Both exported files are `0600`, including when an existing file is overwritten.
File permissions are not encryption, and a blinded review packet is **not** a
public artifact. Keep source transcripts, re-identification mappings, request
traces, titles, review packets and SQLite sandboxes outside Git. Review the exact
redacted payload and authorize its destination before external calls. Redaction
must cover sensitive values, paths and identifiers, not just field names.

Public evidence should contain methodology, source/prompt revisions, anonymous
corpus statistics, hashes, aggregate results, failures and limits. Do not publish
the underlying conversation or a mapping back to it.

## Evolution Limits

The evolution harness deliberately calls `evaluate(force=True)` synchronously.
It preserves pending confirmations, candidate normalization, protection checks
and an atomic SQLite CAS boundary, but bypasses cooldown, turn-frequency and
normal scheduling gates. Records state this explicitly with
`scheduler_gates="bypassed"`. It does not test hook concurrency, close/finalize
recovery, restart loading, live host activation or latency under normal scheduling.

The basic sandbox is not a production SessionDB implementation. Inactive rows are
rejected because it cannot faithfully reconstruct archived compaction provenance.
Structured message content and other host-specific fields may require a richer
adapter. Keep compaction-quality frozen decisions separate from claims about
full-fidelity state replay.

Calculate actual sandbox writes from `title_before != title_after`, and retain
`state_action`, `model_action` and `pending` separately. A proposed frozen rename
is not an applied write; a `pending` transition is not a completed rename. Judge
false renames and missed renames against pre-frozen intent labels and a uniform
confirmation window, and report unavailable model responses as operational
failures rather than silently treating them as successful keep decisions.
