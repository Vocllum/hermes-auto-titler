# Changelog

## Unreleased

## 0.2.0-beta.5 — 2026-09-19

- **Default `first_title_mode` to `plugin`**: the plugin now owns first-title generation from turn 1 out of the box, and disables the host's built-in `auxiliary.title_generation` at plugin load to eliminate race conditions. Explicit `builtin` remains available for users who prefer Hermes to own the opening title.
- **Capacity-aware retry queue**: distinguish capacity/quota errors (429, 503, overloaded, quota, rate limit) from non-recoverable failures. Capacity errors are retried indefinitely with exponential backoff instead of being dropped after 5 attempts. Non-capacity errors still expire after 5 attempts.
- **Startup requeue**: on plugin load, scan SessionDB for untitled or derived-only sessions and automatically re-enqueue them, so quota outages spanning a restart are self-healing.
- **Background retry daemon**: a lightweight 30-second polling thread retries failed sessions independently of user turns, ensuring quota recovery is not blocked on the next conversation.
- **`custom_instructions` config slot**: optional user-defined text appended verbatim to the title-generation system prompt. Use for personal preferences such as `"Always use English for titles"` or `"Prefix every title with [Project]"`. Empty by default (no effect).
- Fix `titleer` → `titler` typo in init warning message.
- Align all fallback defaults and documentation to `plugin` as the canonical `first_title_mode` default.
- Display `failed_queue` depth in `/autotitler status`.

## 0.2.0-beta.4 — 2026-09-16

- Add opt-in `max_renames_per_session` (default `0` / off). When set to N, a session may receive at most N automatic title replacements in the current plugin process; the counter is checked before the model call. First untitled naming and explicit blind regeneration (`rename-now` / `retitle-all`) do not consume the cap, and a failed write does not.
- Change the new-install evaluation cadence to `every_n_turns: 2`.
- Replace the production title prompt with a concise profile and a valid JSON contract: keep returns `{"action":"keep"}`, rename returns `{"action":"rename","title":"..."}`, and review mode may also return `{"action":"approve"}`. Experimental `minimal` / `detailed` prompt densities remain harness-only.
- Replace synthetic prompt-acceptance cases with a read-only real-session experiment: seeded sampling, chronological prefixes, isolated prompt/input/context/strategy cells, title-evolution traces, and separate judge scores. Failed generations are excluded from quality averages.
- Document the optional rename cap and keep anti-jitter on confirmation rounds plus interval cooldown.

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
- Add `pre_llm_call` lifecycle hook: eagerly triggers title evaluation on the very first turn when user submits their opening message, eliminating the un-titled blank period during long tool-calling loops.
- Eliminate over-engineered pseudo-NLP regex casing heuristics (`_name_hints`, `_canonicalize_name_case`, `_normalize_mixed_script_spacing`), trusting the LLM's system prompt contract for identifier casing.
- Remove the redundant hourly rename sliding-window limiter, keeping anti-jitter strictly focused on confirmation rounds and interval cooldown.
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
