# Paired prompt experiment, 2026-10-30

Baseline: main `4906737f827b554417e3673071990d1811832185`, version 0.3.0.
This branch is independent of release PR #12. No version, manifest, changelog,
default setting, confirmation gate, cooldown, provenance or write algorithm changes.

## Finding

Keep the existing production **concise** default. Its 64 counterfactual decisions
reduced unnecessary changes from 3 to 0 and missed changes from 2 to 0, with
62/64 to 64/64 mainline coverage. Its measured token total increased about 7.9%.
This is a small development-sample observation, not a general quality guarantee.

There are tradeoffs: minimal introduced one missed change; Gemini's semantic
blind-review score decreased slightly, including one unsupported output-shape
detail. Exact identifier preservation improved overall but not for both routes.
Do not describe this as an across-the-board win or a cost reduction.

## Frozen protocol

- Six manually redacted real conversation excerpts and two explicitly synthetic
  adversarial cases; technical, document, coursework, creative and automation work.
- Real excerpts preserve selected-message order but omit whole turns and shorten
  necessary evidence. No invented user turns. Original titles are not quality oracles.
- Incumbents and expected keep/rename labels were frozen before model generation.
- Full 3 prompt variants x 2 styles x 2 strategies: 12 cells, each tested on both routes.
- The same production context sampler and budgets for before/after comparisons:
  opening/recent 2 turns, preview 300 characters, user preview 300, summary 1200.
- Temperature 0, common 1024 output-token cap, 90-second HTTP timeout, at most two
  explicit attempts. All completed calls needed one attempt and parsed successfully.
- Language rule fixed to the primary natural language of user messages, not a live
  host locale. Accurate kept titles are not expected to restyle themselves.
- Independent agent blind review: shuffled anonymous candidates; no route, preset,
  revision or original-session-title labels. 94 unique main-matrix candidates plus
  8 independent synthetic safety candidates. Not a human review.

See [paired harness documentation](../../paired-evaluation.md) for reusable APIs
and the distinction between generation-only, counterfactual and stateful tests.

## Observed routes

| Requested route | Returned model |
|---|---|
| `workbuddy-ai/deepseek-v4.1-flash` | `deepseek-v4.1-flash` |
| `antigravity/gemini-3.8-flash` | `gemini-3.8-flash-n` |

These are authenticated gateway observations, not proof of exact provider weights
or rolling snapshots. No group fallback was requested.

## Final counterfactual results

Each variant has 64 observations: 32 expected keeps and 32 expected renames.
These are frozen decisions, **not actual production-profile renames**.

| Variant | Wrong rename before -> after | Missed rename | Mainline /64 | Exact identifiers /64 |
|---|---:|---:|---:|---:|
| minimal | 4 -> 2 | 0 -> 1 | 64 -> 63 | 61 -> 62 |
| concise | 3 -> 0 | 2 -> 0 | 62 -> 64 | 60 -> 61 |
| detailed | 4 -> 1 | 0 -> 0 | 64 -> 64 | 58 -> 63 |

| Route | Wrong rename | Missed rename | Mainline /96 | Exact identifiers /96 | Blind paired win/loss/tie |
|---|---:|---:|---:|---:|---:|
| DeepSeek | 11 -> 3 | 1 -> 0 | 95 -> 96 | 94 -> 93 | 6/3/87 |
| Gemini | 0 -> 0 | 1 -> 1 | 95 -> 95 | 85 -> 93 | 1/2/93 |

Overall: wrong rename 11 -> 3; missed rename 2 -> 1; mainline 190 -> 191/192;
faithfulness 192 -> 191/192; exact identifiers 179 -> 186/192. No completion claims.
One baseline language/fluency failure was an incomplete truncated adjunct, not a
switch of natural language. Exact casing is scored separately from semantic quality.

Paired blind scores use four good binary dimensions (mainline, faithfulness,
later recognition, language/fluency) and two inverse defect dimensions (completion
claim and incidental noise). Equal scores are ties. No input mismatches or missing reviews.

Detailed rename-only title averages decreased from about 21.6/22.6 characters
(concise/complete style) to 19.4/21.1. Minimal's style averages became 19.2/22.7
instead of nearly equal. These are descriptive, not statistically significant;
the before/after rename subsets have slightly different counts.

## Stateful and held-out safety checks

The synchronous E2E track covers one accurate incumbent and one sustained shift,
both styles, both strategies and both routes, using the default concise prompt.
There were 32 real calls per revision. Both revisions kept 8/8 accurate titles and
wrote 8/8 justified changes by the final checkpoint, with no final pending candidate.
This confirms preservation of the pending/approval/CAS path, not improved scheduler gates.
Manual-title protection and first naming are separately covered by deterministic tests.

One independent **synthetic** PR-as-subject case adds 48 real calls across the full
matrix and both revisions. All preserve the repository identifier and PR number;
all pass the independent mainline/faithfulness/recognition/language checks. This is
a safety check for the incidental-PR exception, not a held-out real-session benchmark.

## Billing and evidence

562 successful HTTP model responses, all with request ID and measured usage:
384 final matrix + 64 superseded concise calls + 64 E2E + 48 safety + 2 route probes.
An earlier 384 locally network-blocked attempts received no completion; they are
separate and do not count toward model quality or supplier token totals.

Final matrix token totals: 139,278 -> 154,589 (+11.0%). Default concise:
46,516 -> 50,176 (+7.9%). All successful scopes: 424,998 tokens
(318,539 input; 106,459 output, including reported reasoning).

USD cost is **unknown**, not zero: the gateway did not report it, and these routes
are not comparable to direct public-API list pricing. Earlier nested cache-write
fields remain unknown where not captured; later trace fixes do not backfill zeros.

- [Content-free aggregate metrics](metrics.json), including all 12 cells.
- [Metadata-only call ledger](calls.jsonl), including superseded and blocked scopes.
- [Verification](verification.json): 192 final revised system-prompt hashes match
  the current branch's generated prompts, with zero mismatches.
- [Upstream reference evidence](upstream-reference.md), fixed commits/blobs.
- [Aggregate generator](summarize.py), no credentials or model transport.

No conversation excerpts, generated titles, source-session IDs, local paths, secret
material or private blind mappings are included in the public evidence. The original
dirty installation was backed up and is byte-for-byte unchanged on the nine files.

## Automated tests and limitations

Baseline: 396 passed, 1 skipped. Final: 441 passed, 1 skipped; 45 added regressions.
The skipped real-SessionDB test remains unverified. Tests ran in the existing local
Python 3.14 environment; repository CI separately covers Python 3.11.

This is six selected real excerpts, not a random population or an independent
real held-out set. Samples informed prompt edits. Sparse, shortened evidence can
change topic weights; small binary agent scores are not statistical significance.
Private transcripts prevent third-party reproduction of semantic scores, while
code, synthetic tests, counts and billing metadata remain inspectable.

E2E forces synchronous evaluate and bypasses cooldown/turn scheduling; the basic
SQLite sandbox cannot reconstruct full compaction provenance. No claim is made
about live host lifecycle, UI, Windows behavior, installation or activation. The
main matrix contains incumbents only, so it does not establish first-naming quality.
Existing hard display-width truncation can still cut an arbitrarily long identifier.
