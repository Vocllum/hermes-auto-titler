# ATT prompt reference evidence

Read-only GitHub inspection, checked 2026-10-30T13:12:12+08:00. This note contains public source and PR metadata only: no private conversations, credentials, or model-call results. Source inspection is not runtime or title-quality acceptance.

## ATT baseline and release isolation

- Repository: <https://github.com/Vocllum/hermes-auto-titler>
- Main commit: `4906737f827b554417e3673071990d1811832185`.
- Version source (0.3.0): <https://github.com/Vocllum/hermes-auto-titler/blob/4906737f827b554417e3673071990d1811832185/pyproject.toml>
- Prompt source: <https://github.com/Vocllum/hermes-auto-titler/blob/4906737f827b554417e3673071990d1811832185/hermes_auto_titler/policy.py>
- Prompt source blob: `ba5883be3fe221cf2445461deef9a1f0b91eb8c2`.
- PR #11 is merged into main: <https://github.com/Vocllum/hermes-auto-titler/pull/11>. Its contributor head is `5cf8061c1c74215d5b795e1fc3c8ea6924ddbb34`; pytest succeeded in <https://github.com/Vocllum/hermes-auto-titler/actions/runs/40922559846>. Catalog sync was skipped.
- PR #12 is open Draft: <https://github.com/Vocllum/hermes-auto-titler/pull/12>. Head `release/0.3.1-20261030` is `500cd88d9f84f61f8e28c1514d056d5a1f65d37c`; base is the main commit above. Its four changed files prepare version/changelog/manifest verification only. The PR explicitly states: "No unmeasured prompt change is bundled in this patch release."
- PR #12 pytest succeeded at its current head in <https://github.com/Vocllum/hermes-auto-titler/actions/runs/40923050316> and <https://github.com/Vocllum/hermes-auto-titler/actions/runs/40923041897>; catalog sync was skipped. This does not establish local host acceptance.

ATT normal mode uses JSON decisions (`keep` or `rename`); review can also `approve`; blind/force regeneration is rename-only. Its default prompt variant is concise; the experiment-only minimal variant currently omits the generated style, strategy, and length clauses. Concise and detailed include exact-identifier rules. One concise sentence to test rather than assume is beneficial:

> For a broad user goal, prefer the assistant's concrete diagnosis if later users refer back to it; ignore unrelated assistant claims.

That sentence can compete with retaining the umbrella goal. The real replay should distinguish an incidental diagnosis from a user-confirmed change of ultimate goal.

## DeepSeek Harness all-prompts title provider

- Fixed master commit: `ce7073d51254c26cd63b22a60835b8f948deb447`.
- Public repository ID: `1333065091` (API metadata identifies DeepSeek Harness, public).
- Source module: the experimental all-human-prompts title provider's `src/index.ts` (identified by the exact blob below).
- Fixed source blob: `a88ba636950740a71b9123463c9a90abd72f6d90`.
- Public ID-based commit URL: <https://api.github.com/repositories/1333065091/commits/ce7073d51254c26cd63b22a60835b8f948deb447>
- Public ID-based source-blob URL: <https://api.github.com/repositories/1333065091/git/blobs/a88ba636950740a71b9123463c9a90abd72f6d90>

Short actual system-prompt extracts:

> Use the language of the messages.

> Aim for about ${config.targetWords} words in non-CJK languages or ${config.targetCjkCharacters} CJK characters.

> An existing title is supplied as currentTitle. If it still accurately describes the main topic or task, return it exactly unchanged.

> Follow-up questions, additional details within the same topic, acknowledgements such as "thanks", and requests to continue do not by themselves justify a title change.

> Do not reword, polish, shorten, or replace synonyms in an adequate title. Keeping its exact wording takes priority over the target length.

> Change the title only when the messages materially change or expand the main topic or task so that the existing title is no longer accurate.

Protocol/implementation differences:

- Returns one natural-language plain-text line, not ATT's JSON decision protocol.
- `targetWords` and `targetCjkCharacters` are required positive-integer config fields, not hardcoded default counts. Do not invent DSH default word/character targets from this source.
- Human messages are framed with JSON serialization. A current-title anchor is included only when its provenance is `source.kind === 'provider'`.
- This inspection does not establish that this provider/revision is active on the user's installed DSH host.

## MonoCode sessionTitle.ts

- Fixed main commit: `8ba5863ebde36772efd13d8182c6b48b2ddcd82c`.
- Source: <https://github.com/hardbeat920/monocode/blob/8ba5863ebde36772efd13d8182c6b48b2ddcd82c/src/features/sessions/model/sessionTitle.ts>
- Source blob: `062261e0cd70cf05a79877b902525db164f5991e`.

Short actual prompt extracts:

> Generate a title that will help the user recognize this coding session weeks later.

> Subject: What system, feature, or problem is this really about?

> Outcome: What does the user ultimately want to understand or change?

> Incidental instructions: What only describes how the agent should do the work?

> Title the subject and outcome. Discard incidental instructions.

> Capture the umbrella goal when the request lists several symptoms or steps.

> Name the product change, not the mock, plan, report, branch, or PR used to produce it.

> Models, subagents, tools, and output formats do not belong in the title unless they are themselves the topic.

> Do not claim the work is complete.

> Do not copy and truncate the user's message.

Protocol/implementation differences:

- Operates on the initial user message for first-turn/placeholder titles, or explicit refresh; it is not a persistent incumbent-title maintenance policy.
- Returns JSON `title` plus `workItem`; work-item metadata must come from an explicit issue/PR number or URL, not inference. ATT should not adopt that extra task unintentionally.
- Source `MESSAGE_LIMIT` is 8,000. The prompt asks for 3-8 words and fewer than 40 characters, while sanitizer `TITLE_LIMIT` is 50 characters.
- No ATT keep/rename/review protocol or conservative/aggressive policy exists in this module.

## PR duplication check: all states

Enumeration, not a keyword-only search:

- <https://api.github.com/repos/Vocllum/hermes-auto-titler/pulls?state=all&per_page=100&page=1>
- <https://api.github.com/repos/Vocllum/hermes-auto-titler/pulls?state=all&per_page=100&page=2> returned `[]`.
- Smaller readable cross-check with PR-containing issue records: <https://api.github.com/repos/Vocllum/hermes-auto-titler/issues?state=all&per_page=6&page=1> and <https://api.github.com/repos/Vocllum/hermes-auto-titler/issues?state=all&per_page=6&page=2>; page 3 returned `[]`.

These calls exposed 11 PRs. No #9 record was exposed; no conclusion is drawn about why that number is absent. All PR metadata is a point-in-time check, not a promise that a new PR cannot appear later.

| PR | State at check | Actual title | Relevance |
| --- | --- | --- | --- |
| [#12](https://github.com/Vocllum/hermes-auto-titler/pull/12) | Open Draft | release: prepare hermes-auto-titler v0.3.1 | Release preparation, explicitly excludes unmeasured prompt change |
| [#11](https://github.com/Vocllum/hermes-auto-titler/pull/11) | Merged | fix: stop asking the host for the reserved `model` settings root | Config lookup, not prompt quality |
| [#10](https://github.com/Vocllum/hermes-auto-titler/pull/10) | Open | fix: keep the profile secret scope for plugin-owned background work | Background host scope, not prompt quality |
| [#8](https://github.com/Vocllum/hermes-auto-titler/pull/8) | Merged | fix: harden legacy state migration and reconcile concurrent records (v0.3.1) | State safety, not prompt quality |
| [#7](https://github.com/Vocllum/hermes-auto-titler/pull/7) | Merged | release: prepare v0.3.0 with clearer settings and safe state migration | Release/settings/state |
| [#6](https://github.com/Vocllum/hermes-auto-titler/pull/6) | Closed, not merged | fix: keep runtime state out of the plugin install directory | State location; #7 says it supersedes #6 and preserves contributor commit |
| [#5](https://github.com/Vocllum/hermes-auto-titler/pull/5) | Merged | refactor: prepare 0.3 context representation and retry scheduling | Historical context/representation work; body is null, not treated as proof of a specific prompt-quality result |
| [#4](https://github.com/Vocllum/hermes-auto-titler/pull/4) | Closed, not merged | fix: cap the failure counter and stop blaming the gateway for missing creds | Failure classification/retry/logging |
| [#3](https://github.com/Vocllum/hermes-auto-titler/pull/3) | Merged | release: hermes-auto-titler 0.2.0 | Includes evidence-first prompt refinements and semantic acceptance tooling |
| [#2](https://github.com/Vocllum/hermes-auto-titler/pull/2) | Merged | fix: keep aggressive policy internally consistent | Prompt policy: prevents conservative keep clause leaking into aggressive |
| [#1](https://github.com/Vocllum/hermes-auto-titler/pull/1) | Merged | feat: restore strategy policy and true multi-round reviews | Evidence-first prompt, title hypothesis order, sustained-pivot strategy, confirmation semantics |

Conclusion: no currently open prompt-improvement duplicate was exposed. The only open PRs are #10 and #12. Historical prompt-related work already exists in merged #1/#2/#3, and historical representation work in merged #5; the new measured experiment should build on current main rather than claim prompt work never existed. Keep its branch/PR separate from #12.
