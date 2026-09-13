# Changelog

## Unreleased

- Implement true N-round review semantics: `rename_confirmations: N` now requires N follow-up endorsements before an llm→llm title write; replacing the candidate resets the count.
- Restore meaningful `conservative` / `aggressive` strategies. Aggressive mode follows explicit or sustained topic pivots sooner without treating one-off subtasks or tool changes as new subjects.
- Make title judgment evidence-first: conversation evidence is evaluated before the existing/proposed title, assistant text is lower-confidence evidence, and conversation excerpts cannot override the title-maintenance protocol.

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
