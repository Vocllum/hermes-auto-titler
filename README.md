<div align="center">

<img src="docs/banner.png" alt="hermes-auto-titler — session titles that follow the conversation, not just the first message" width="100%"/>

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)![Hermes Agent](https://img.shields.io/badge/Hermes_Agent-plugin-1f6feb)![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab?logo=python&logoColor=white)

*Topic-aware session title maintenance for [Hermes Agent](https://github.com/NousResearch/hermes-agent), with provenance-safe writes and long-horizon intent tracking.*

[Features](#-features) · [How it works](#-how-it-works) · [Install](#-install) · [Configuration](#%EF%B8%8F-configuration) · [Commands](#-commands) · [简体中文](README.zh-CN.md)

</div>

---

Hermes can name a session from its opening exchange. But conversations evolve; their titles often don't. A chat that begins with *"how do i fix the thing with..."* can turn into an urgent Redis memory leak investigation and production deploy, while the sidebar label remains anchored to the first message.

**hermes-auto-titler** maintains session labels across their entire lifecycle. It periodically evaluates whether an auto-generated title still reflects the user's durable intent, updates the label when the conversation genuinely changes direction, and never overwrites titles you set yourself.

```text
┌─ SESSIONS ────────────────────────────────────────────────┐
│  ✦ Redis worker leak                          [AUTO]      │
│    was: how do i fix the thi…                             │
│  ✓ vLLM quant benchmarks                      [KEPT]      │
│    newest: "ok thanks!"                                   │
│  🔒 my research notes                         [USER]      │
│    never overwritten                                      │
│                                                           │
│  derived < llm < user                                     │
└───────────────────────────────────────────────────────────┘
```

## ✨ Features

| | |
|---|---|
| **Provenance-safe** | Enforces `derived < llm < user`. `derived` = Hermes' deterministic fallback from the first message; `llm` = model-generated; `user` = yours, **never overwritten**. Legacy titles without recorded provenance are treated as user-set and protected. |
| **Long-horizon intent tracking** | Synthesizes opening turns, recent turns, a bounded user-message trajectory, and historical compaction summaries when the original opening is evicted. Long messages use head+tail extraction to preserve late instructions. |
| **Intent-aware capture** | Filters compaction handoffs, system noise, adjacent replay duplicates, and internal cron, subagent, or background turns before they can distort the title. |
| **Evidence-first judgment** | Infers the durable subject from conversation evidence before comparing against current or proposed titles. Explicit, repeated user goals carry highest weight; assistant responses cannot introduce new topics independently. |
| **First-turn takeover or coexistence** | `first_title_mode: plugin` (default) evaluates from turn 1 (`pre_llm_call`) and disables host built-in title generation at startup to eliminate race conditions. Set to `builtin` if you prefer the host to own initial title creation. |
| **Conservative / aggressive policy** | `conservative` requires a clear, durable mismatch before renaming. `aggressive` detects explicit goal abandonment or sustained new directions sooner, while still treating one-off subtasks, status checks, and tool changes as transient noise. |
| **N-round review gate** | With `rename_confirmations: 1` (default), an automatic title candidate requires one follow-up endorsement before writeback. Set to `0` for immediate updates, or N to require N consecutive endorsements (resetting if the candidate changes). |
| **Optional rename cap** | `max_renames_per_session: 0` disables the cap. Setting N allows at most N automatic replacements per session, preventing title churn in long-running chats. Initial naming and manual `rename-now` do not consume the cap. |
| **Custom prompt slot** | `custom_instructions` appends user-defined text to the title-generation system prompt. Use for personal preferences like language or prefix conventions. Empty by default (no effect). |
| **Call-efficient by design** | Turn-cadence gating, per-session rate throttling, provenance checks, in-flight deduplication, and internal-turn filtering ensure that most foreground turns make no model calls. |
| **Conservative surface cleanup** | Preserves literal identifiers (commands, paths, code symbols), adds proper CJK–Latin spacing, and only adjusts letter casing when supported by direct conversation evidence. |
| **Profile-isolated and auditable** | SessionDB connections are isolated per Hermes profile, and all title-generation calls are logged under `task=hermes_auto_titler` in usage metrics. |

> ⚠️ **Privacy / data flow — read before enabling**
> The plugin transmits selected conversation excerpts to the title model: opening and recent turns, a sampled user-message trajectory, compacted history summaries when present, and attachment filename placeholders. Sampling limits are governed by `opening_turns`, `recent_turns`, `preview_chars`, `include_all_user_messages`, `user_message_threshold`, `user_message_preview_chars`, `summary_preview_chars`, and `retitle_summary_chars`. Model requests count toward your provider's usage and billing.

## 🔍 How it works

1. **First-title ownership** — By default, Hermes creates initial titles via its built-in `title_generation` task. With `first_title_mode: plugin` (default), this plugin evaluates from turn 1 and automatically disables the host's competing title generator at startup to prevent race conditions. Set to `builtin` if you prefer the host to handle the initial title.
2. **Triggering and cadence gating** — Hooks into `on_session_end` and `on_session_finalize`. Only completed foreground turns count toward `every_n_turns` (default `2`); failed/interrupted turns and cron, subagent, or background tasks are excluded. Periodic evaluations run asynchronously in a daemon worker, while session close/finalize evaluations run synchronously (still respecting `min_interval_minutes`).
3. **Context construction** — Extracts opening turns, recent turns (pairing each user prompt with the final assistant response), and a bounded trajectory of earliest and latest user prompts. When history compaction has evicted the opening exchange, earlier compaction summaries serve as historical anchors. Protocol handoff wrappers and prompt replays are stripped before sampling.
4. **Evidence-first evaluation** — The auxiliary model outputs structured JSON. Conversation evidence is presented ahead of the current title to avoid anchoring bias. Explicit and repeated user intent carries the highest weight; assistant responses provide supporting context but cannot introduce new topics independently. Structural overlap between sampled sections is explicitly discounted.
5. **Strategy evaluation** — `conservative` maintains the existing title unless a significant, durable topic shift occurs. `aggressive` adapts quickly when the user explicitly abandons an earlier objective or pursues a sustained new direction, though recent turns alone remain insufficient to trigger a rename.
6. **Multi-round confirmation gate** — Under `rename_confirmations: N`, proposed `llm` → `llm` renames are held as pending candidates until confirmed across N subsequent evaluations. If a different candidate is proposed, the counter resets. Initial titling, `derived` upgrades, and manual `/autotitler rename-now` commands bypass this gate.
7. **Safe writeback** — Provenance is re-validated immediately before writing to SessionDB. Pending candidates are invalidated if the underlying title changes. Suffixes for duplicate titles respect width constraints, and writes carry `llm` provenance so user-specified titles are never overwritten.

<details>
<summary><b>Design decisions worth knowing</b></summary>

- **Why continuous maintenance instead of better first-message titling?** An opening exchange cannot anticipate where a conversation leads. Long-running sessions require titles that evolve alongside the user's actual objectives.
- **Why sample intent trajectory instead of passing full transcripts?** The title model requires durable intent rather than tool execution noise. The plugin preserves opening and recent context alongside a bounded trajectory of key user prompts, using head+tail extraction to retain critical instructions in long messages.
- **Why infer the subject before inspecting current titles?** Existing titles make good comparison baselines but poor evidence. Analyzing conversation evidence first avoids premature anchoring on outdated labels while preserving conservative write thresholds.
- **Why evaluate synchronously on session close?** Titles written after session termination may never surface in the UI. While synchronous evaluation on close can block up to the provider timeout (~30 s), it ensures the final session state is captured before process teardown.
- **Why require one review endorsement by default?** A single rename trigger can reflect a temporary detour rather than a permanent pivot. Requiring one subsequent endorsement provides a sensible defense against title flutter; set `rename_confirmations: 0` for immediate updates, or increase N for stricter stability.
- **Known limitation:** Hermes does not provide an atomic compare-and-swap API for session titles, leaving a narrow race window during concurrent writes. The plugin mitigates and detects these collisions rather than assuming they cannot occur.

</details>

## 📦 Install

**Recommended — via the Hermes plugin installer:**

```bash
hermes plugins install https://github.com/Vocllum/hermes-auto-titler --enable
```

This clones the repo into `~/.hermes/plugins/`, generates `config.yaml` from the bundled template, and enables the plugin.

**Manual alternative:**

```bash
cd ~/.hermes/plugins
git clone https://github.com/Vocllum/hermes-auto-titler
cp hermes-auto-titler/config.yaml.example hermes-auto-titler/config.yaml
```

By default, the plugin uses Hermes' built-in `title_generation` auxiliary task, so the host owns the provider/model choice. To use a plugin-local custom route instead, set `provider` and/or `model` in the plugin config. The host must authorize the selected mode:

```yaml
# ~/.hermes/config.yaml
plugins:
  enabled:
    - hermes-auto-titler
  entries:
    hermes-auto-titler:
      llm:
        allow_provider_override: true
        allow_model_override: true
        allow_task_override: true
```

Restart Hermes, then verify the plugin configuration:

```text
/autotitler status
```

### Which model does it use?

**Hermes auxiliary mode (default).** Leave `provider` and `model` empty. The plugin calls Hermes' built-in `title_generation` task, and Hermes resolves `auxiliary.title_generation.provider` / `model` for it.

**Plugin custom mode (optional).** Set `provider` and/or `model` in `~/.hermes/plugins/hermes-auto-titler/config.yaml` to use an explicit plugin-local route:

```yaml
provider: "your-provider"   # provider already configured in Hermes
model: "your-model"         # any model your Hermes setup can reach
```

- In Hermes auxiliary mode, `allow_task_override: true` authorizes the plugin to borrow the built-in `title_generation` task.
- In plugin custom mode, `allow_provider_override: true` / `allow_model_override: true` authorize the explicit route. The plugin never stores a separate API key.
- The selected channel is used only for title evaluation; your main conversation keeps its own model.
- `/autotitler status` shows the plugin's configured route. When it says `(host default)`, inspect Hermes' `auxiliary.title_generation` configuration to see the host-resolved provider/model.

**Model guidance.** The plugin is designed to run efficiently on small, fast instruction-following models. Prioritize consistent JSON output, multilingual comprehension, and strong prompt adherence over large reasoning capacity. Start with Hermes' normal auxiliary title route. Before deploying an ultra-light model, evaluate `scripts/review_sample.py` against your own session history. If you observe topic drift, invalid JSON, or degraded multilingual titles on long or compacted sessions, switch to a more capable auxiliary model.

**Requirements:** Python ≥ 3.11 · a recent Hermes Agent.

> **Takeover-mode note:** `first_title_mode: plugin` disables Hermes' built-in title generator through the host config when the plugin loads. Changing `first_title_mode` at runtime does not replay that startup ownership change, and switching back to `builtin` does not automatically re-enable a host title generator that the plugin previously disabled. Treat ownership changes as restart-time configuration and verify the host `auxiliary.title_generation.enabled` setting when switching back.

> **Install-layout note:** place the plugin directly at `~/.hermes/plugins/hermes-auto-titler/`. If you symlink the package directory instead, `config.yaml` must live where the package physically resides (config resolves relative to the package).

## ⚙️ Configuration

`~/.hermes/plugins/hermes-auto-titler/config.yaml` — every key is optional; invalid values fall back to defaults.

<details>
<summary><b>All keys</b></summary>

| Key | Default | What it does |
|---|---|---|
| `enabled` | `true` | Master switch. If the plugin started disabled, enabling it requires a restart because no hooks were registered; disabling an already-loaded plugin takes effect through the hook guard. |
| `every_n_turns` | `2` | Evaluate every N completed foreground turns. |
| `first_title_mode` | `plugin` | `plugin` = plugin evaluates from turn 1 and disables the host title generator at plugin load; `builtin` = Hermes owns first-title generation. Treat changes as restart-time ownership changes. |
| `early_turn_eval` | `false` | Legacy compatibility key. Current behavior is controlled by `first_title_mode`: `plugin` enables early evaluation; `builtin` does not. |
| `on_close` | `true` | Evaluate on real session finalize/close (synchronous, throttled). |
| `recent_turns` / `opening_turns` | `2` / `2` | Context windows in real user turns; each selected turn keeps the user message + last assistant reply. |
| `ignore_model_messages` | `false` | Exclude assistant messages from captured context (A/B testing aid). |
| `preview_chars` | `400` | Per-message preview budget for opening/recent context. Multi-sentence messages use head+tail extraction. |
| `include_all_user_messages` | `true` | Append the sampled user-message trajectory. |
| `user_message_threshold` | `40` | Maximum trajectory length; over the limit, keep the first user message + most recent N−1. `0` = unlimited. |
| `user_message_preview_chars` | `300` | Per-user-message trajectory budget using head+tail extraction. `0` = unlimited. |
| `summary_preview_chars` | `1200` | Compaction-summary budget for normal evaluations. |
| `retitle_summary_chars` | `12000` | Compaction-summary budget for blind/manual/bulk regeneration; `0` falls back to `preview_chars`. |
| `title_style` | `concise` | `concise` = subject label · `complete` = short event/intent summary (also gets a wider display budget). |
| `strategy` | `conservative` | `conservative` = rename only for a material durable mismatch; `aggressive` = follow explicit abandonment or a sustained new direction sooner, while still ignoring one-off subtasks/tool changes. |
| `provider` / `model` | `""` / `""` | Both empty = Hermes `title_generation` auxiliary task; set either to select a plugin custom route. |
| `min_interval_minutes` | `5` | Minimum interval between evaluations of one session. |
| `max_title_length` / `max_display_width` | `null` / `40` | `max_title_length: null` avoids hard character slicing in code; the prompt keeps titles brief and `max_display_width` strictly caps display columns. `complete` style adds 12 display columns. |
| `rename_confirmations` | `1` | Default: require one later endorsement before llm→llm writeback. `0` = immediate write after one decision; `N > 1` = require N later endorsements. Replacing the pending candidate restarts the count. |
| `max_renames_per_session` | `0` | Automatic replacement cap is off by default. `N > 0` allows at most N automatic title replacements per session. Initial naming and explicit `rename-now` do not consume it; upgrading an existing `derived` title counts as a replacement. The counter is process-local. |
| `custom_instructions` | `""` | Optional text appended verbatim to the end of the title-generation system prompt. Use for personal preferences such as `"Always use English for titles"` or `"Prefix every title with [Project]"`. Empty = no effect. |

</details>

## 🕹️ Commands

```text
/autotitler status                                     # current plugin config/state
/autotitler config <key> [value]                       # view/set config and persist it
/autotitler rename-now [session_id]                    # blind-regenerate now; bypasses review gate
/autotitler retitle-all [--dry-run] [--limit N] [--min-messages N]   # bulk blind regeneration
```

Most configuration options take effect immediately. Changing `enabled` requires a restart if hooks were not registered during startup, and `first_title_mode` should be treated as a startup ownership switch as noted above.

`rename-now` performs blind regeneration: the current title is withheld from the model to eliminate anchoring bias, and the confirmation gate is bypassed. `retitle-all --dry-run` simulates bulk updates without making API calls or modifying data. When executed without `--dry-run`, user-defined titles are preserved and only eligible automatic titles are regenerated.

## 💰 Cost

**Input size per evaluation is bounded, not fixed.** Opening and recent turns use `preview_chars=400` by default; the user trajectory retains up to 40 messages at up to 300 characters each; and active compaction summaries can add up to 1,200 characters. While short conversations consume far fewer tokens, long sessions with full trajectories can exceed typical 1–3K character baselines.

The plugin requests `max_tokens=64`, but certain OpenAI-compatible providers may not strictly enforce output limits upstream. For example, during benchmark acceptance runs, an unconstrained model produced **597 input / 1,639 output tokens** due to internal reasoning tokens before emitting JSON. Account for your specific provider's token accounting rather than assuming 64 output tokens is a hard global ceiling.

**Cadence gating is the primary cost guard:** turn-interval checks (`every_n_turns`), per-session cooldowns (`min_interval_minutes`), provenance validation, in-flight request deduplication, and filtering out background/subagent turns prevent redundant invocations. Every invocation is logged to Hermes usage metrics under `task=hermes_auto_titler`.

A lightweight auxiliary model matches the intended cost profile, but verify suitability against your own transcripts—particularly long, compacted sessions.

## 🧪 Development

```bash
uv venv .venv && uv pip install --python .venv/bin/python pytest PyYAML
.venv/bin/python -m pytest tests/ -v
<your-hermes-checkout>/venv/bin/python scripts/integration_check.py
<your-hermes-checkout>/venv/bin/python scripts/review_sample.py --n 20
<your-hermes-checkout>/venv/bin/python scripts/review_sample.py --n 20 --strategy aggressive
<your-hermes-checkout>/venv/bin/python scripts/prompt_acceptance.py --n 8 --seed 17 --output /tmp/title-experiment.jsonl
<your-hermes-checkout>/venv/bin/python scripts/e2e_check.py <session_id>
```

`review_sample.py` runs in dry-run mode and mirrors the active production context configuration, including preview lengths, trajectory limits, and compaction summary budgets.

`prompt_acceptance.py` is an isolated experimental harness: it samples real non-user-titled sessions, replays chronological conversation prefixes (e.g., turns 1, 2, 4, and final), evaluates prompt variants against identical prefixes, and optionally scores generated titles using a separate LLM judge. By default, it operates via Hermes' `PluginLlm` bridge using the configured `title_generation` task; raw OpenAI-compatible endpoints can be configured via `HERMES_AUTOTITLER_EXPERIMENT_*` environment variables. It operates strictly in read-only mode against SessionDB, concealing existing titles during generation and judging.

`retitle_all.py --dry-run` previews bulk regeneration without invoking models or writing database updates. Executing without `--dry-run` applies updates to eligible automatic titles.

## 📄 License

[Apache-2.0](LICENSE)

<div align="center">
<sub>Built by <a href="https://github.com/Vocllum">Vocllum</a></sub>
</div>