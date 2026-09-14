# Changelog

## 0.2.0 — 2026-09-14

- Reframe title generation around long-horizon Session Identity: a topic shift alone is never sufficient reason to erase a historically substantial main thread; late work is incorporated as an extension, secondary topic, or phase evolution (umbrella or dual-subject) unless earlier work was explicitly abandoned or minor.
- Unify `conservative` and `aggressive` strategies around overall session identity: `conservative` maintains higher evidence thresholds before expanding or changing the summary; `aggressive` incorporates substantial new phases or direction shifts sustained across substantive turns sooner without erasing historical investments.
- Shift title length control to prompt guidance: set `max_title_length: null` by default so code-level character slicing is disabled, preventing accidental truncation of CJK words after long repository or command identifiers; title compactness is guided softly by prompt (~12 CJK characters) and display-width column constraints.
- Implement true N-round review semantics: `rename_confirmations: N` now requires N follow-up endorsements before an llm→llm title write; replacing the candidate resets the count, and a pending candidate is discarded if its base automatic title changes between review rounds.
- Change the new-install default to `rename_confirmations: 1`; set it to `0` for immediate writes after one rename decision.
- Simplify the title prompt into five evidence-first decision principles. Conversation evidence is evaluated before title hypotheses, assistant text is lower-confidence evidence, and structural duplication across opening/recent/trajectory sections is not counted as repeated user intent.
- Make blind and forced regeneration use a rename-only JSON contract while review mode keeps `keep` / `approve` / `rename`.
- Keep literal identifiers and uncertain names conservative while retaining language inheritance and mixed-script formatting rules.
- Make `scripts/review_sample.py` reuse the loaded production context configuration and support strategy overrides, so real-history prompt/model reviews match production sampling more closely.
- Expand semantic acceptance matrix (`scripts/prompt_acceptance.py`) to 20 curated paired cases across full project lifecycles, and add step-by-step evolution simulator (`scripts/simulate_evolution.py`).
- Add/extend regression coverage for N-round counting, candidate replacement, base-title invalidation, strategy thresholds, evidence-before-title ordering, rename-only regeneration, soft length bounds, and the v0.2 default.
- Clean up dead code in base AutoTitler by delegating generation completely to policy, and align prompt soft-length guidance with explicit `max_title_length` configuration.
- Eliminate over-engineered pseudo-NLP regex casing heuristics (`_name_hints`, `_canonicalize_name_case`, `_normalize_mixed_script_spacing`), trusting the LLM's system prompt contract for identifier casing.
- Remove redundant `renames_per_hour` sliding-window limiter, keeping anti-jitter strictly focused on confirmation rounds and interval cooldown.
- Implement bounded failed-session retry ledger (`_failed_sessions`): track un-titled sessions after model errors (503/timeout), apply exponential backoff (30s-600s), evict `_last_eval` on error to avoid throttling locks, add global backpressure (max 2 claims per sweep), thread-safe retry state locks, fail-closed provenance CAS, wait on in-flight workers during finalize, and synchronously snapshot `base_title`.

## 0.1.3 — 2026-09-12

- Fix context extraction: replace hard front-truncation with dynamic head-and-tail window extraction to preserve tail imperatives.
- Restructure system prompt: decouple durable session subject from transient tool mechanisms (e.g. ego, browser).
- Align review protocol semantics: `rename_confirmations: 0` for direct writes and `1` for 1-round review confirmation.
- Enhance manual retitle: `/autotitler rename-now` now bypasses review hysteresis (`blind=True`) for instant updates and supports prefix ID matching.

## 0.1.2 — 2026-09-08

- Make `first_title_mode: plugin` disable Hermes' competing built-in title generator through the host config API.
- Add regression coverage and document the takeover behavior.

## 0.1.1 — 2026-09-07

- Make title language inherit Hermes `display.language` through the shared i18n locale resolver.
- Keep the title prompt locale-driven without a plugin-owned language-name allowlist.
- Regenerate anomalous English titles for Chinese conversations while preserving `title_source: llm`.
- Add regression coverage for display-language propagation and clean the test/release metadata.

## 0.1.0

- Initial public release.
