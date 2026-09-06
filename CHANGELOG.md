# Changelog

## 1.1.0 — 2026-09-07

- Make title language inherit Hermes `display.language` through the shared i18n locale resolver.
- Keep the title prompt locale-driven without a plugin-owned language-name allowlist.
- Regenerate anomalous English titles for Chinese conversations while preserving `title_source: llm`.
- Add regression coverage for display-language propagation and clean the test/release metadata.

## 0.1.1

- Maintenance release after the initial public release.

## 0.1.0

- Initial public release.
