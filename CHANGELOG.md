# Changelog

## Unreleased

## 0.2.3 — 2026-09-21

- **First-turn end override**: Once the first turn completes with the assistant's response, immediately evaluate and override the initial eager preview title from the full turn context without cadence or debounce delays.
- **Strict user authority & error backoff**: Unconditionally preserve manual user titles, and safely route provider/network errors to the exponential retry ledger.

## 0.2.2 — 2026-09-21

- **Graceful shutdown protection**: close and finalize hooks never start a network call; in-flight work is waited on for at most 100 ms and the full path stays well under the host's 10 s teardown budget, so terminal titles are no longer lost to shutdown timeouts.
- **Durable typed scheduler state**: retry, finalize, pending-review and rename-counter ledgers persist separately with per-record isolation, so a single corrupt record can no longer abort plugin registration.
- **Finalize intent with its own budget**: terminal intents survive the ordinary 5-failure cap and are only cleared by a worker that provably covered the final close epoch.
- **Closing fence bound to intent lifetime**: a closing fence is released by the same code path that settles the intent, so a resolved session can never be permanently barred from foreground titling.
- **Two-phase unknown-anchor consumption**: a close whose DB read failed re-validates its anchor before consumption, instead of being "covered" tautologically by a later turn.
- **Catalog card at official 2:1**: adds a 1600x800 card for the plugin browser; the original 2400x640 banner moves to the detail page, where it is no longer clipped.
- **English defaults for `/autotitler`**: slash-command replies now default to English, matching the README and prompt language.

## 0.2.1 — 2026-09-21

- **Long-session title tracking**: evaluate opening intent, topic shifts, and late instructions to track genuine conversation progress, preventing titles from drifting due to transient follow-ups or tool logs.
- **Compaction support and noise filtering**: correctly handle Hermes compaction summaries while removing code blocks and tool logs from title evaluation context.
- **State persistence**: persist pending candidate titles and retry queues to disk across process restarts; protect user-edited titles.
- **Shutdown timeout protection**: replace external network calls during session exit with bounded local finalization, preventing title loss from host termination timeouts.
- **Lifecycle fixes**: resolve first-turn concurrency races, merge duplicate shutdown events, and fix omitted retries for untitled sessions.

## 0.2.0 — 2026-09-19

- **Default `first_title_mode` to `plugin`**: the plugin now owns first-title generation from turn 1 out of the box, and disables the host's built-in `auxiliary.title_generation` at plugin load to eliminate race conditions. Explicit `builtin` remains available for users who prefer Hermes to own the opening title.
- **Capacity-aware retry queue**: distinguish capacity/quota errors (429, 503, overloaded, quota, rate limit) from non-recoverable failures. Capacity errors are retried indefinitely with exponential backoff instead of being dropped after 5 attempts. Non-capacity errors still expire after 5 attempts.
- **Startup requeue**: on plugin load, scan SessionDB for untitled or derived-only sessions and automatically re-enqueue them, so quota outages spanning a restart are self-healing.
- **Background retry daemon**: a lightweight 30-second polling thread retries failed sessions independently of user turns, ensuring quota recovery is not blocked on the next conversation.
- **`custom_instructions` config slot**: optional user-defined text appended verbatim to the title-generation system prompt. Use for personal preferences such as `"Always use English for titles"` or `"Prefix every title with [Project]"`. Empty by default (no effect).
- **`max_renames_per_session`**: opt-in per-session cap on automatic title replacements (default `0` / off). First untitled naming and explicit blind regeneration do not consume the cap.
- **`pre_llm_call` lifecycle hook**: eagerly triggers title evaluation on the very first turn when user submits their opening message, eliminating the un-titled blank period during long tool-calling loops.
- **N-round review protocol**: `rename_confirmations: N` requires N follow-up endorsements before an llm→llm title write; replacing the candidate resets the count, and a pending candidate is discarded if its base automatic title changes between review rounds. Default: `1`.
- **Five evidence-first decision principles**: conversation evidence is evaluated before title hypotheses, assistant text is lower-confidence evidence, and structural duplication across opening/recent/trajectory sections is not counted as repeated user intent.
- **Long-horizon Session Identity**: a topic shift alone is never sufficient reason to erase a historically substantial main thread; late work is incorporated as an extension, secondary topic, or phase evolution unless earlier work was explicitly abandoned or minor.
- **Bounded failed-session retry ledger**: track un-titled sessions after model errors, apply exponential backoff (30s–600s), global backpressure (max 2 claims per sweep), thread-safe retry state locks, fail-closed provenance CAS, and synchronous `base_title` snapshot.
- Unify `conservative` and `aggressive` strategies around overall session identity.
- Shift title length control to prompt guidance (`max_title_length: null` by default); title compactness is guided softly by prompt (~12 CJK characters) and display-width column constraints.
- Replace the production title prompt with a concise profile and a valid JSON contract.
- Replace synthetic prompt-acceptance cases with a read-only real-session experiment harness.
- Keep literal identifiers and uncertain names conservative while retaining language inheritance and mixed-script formatting rules.
- Eliminate over-engineered pseudo-NLP regex casing heuristics, trusting the LLM's system prompt contract for identifier casing.
- Remove the redundant hourly rename sliding-window limiter, keeping anti-jitter strictly focused on confirmation rounds and interval cooldown.
- Clean up dead code in base AutoTitler by delegating generation completely to policy.
- Change the new-install evaluation cadence to `every_n_turns: 2`.
- Display `failed_queue` depth in `/autotitler status`.
- Fix `titleer` → `titler` typo in init warning message.
- ASCII art banner and polished bilingual documentation.

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
