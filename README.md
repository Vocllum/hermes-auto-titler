<div align="center">

<img src="docs/banner.svg" alt="hermes-auto-titler — session titles that follow the conversation, not the last message" width="100%"/>

# hermes-auto-titler

**Session titles that follow the conversation — not the last message.**

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)![Hermes Agent](https://img.shields.io/badge/Hermes_Agent-plugin-1f6feb)![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab?logo=python&logoColor=white)

*Provenance-aware, conservative session title maintenance for [Hermes Agent](https://github.com/NousResearch/hermes-agent).*

[Features](#-features) · [How it works](#-how-it-works) · [Install](#-install) · [Configuration](#%EF%B8%8F-configuration) · [Commands](#-commands) · [简体中文](README.zh-CN.md)

</div>

---

Your agent's sidebar is its memory map. But most tools name a session **once**, from the first message — and then the title drifts after whatever you typed last. A session that starts as *"what's the vLLM quant format?"* and grows into a two-day deployment project ends up titled `new chat 3`.

**hermes-auto-titler** keeps titles accurate for the *whole* life of the session: a cheap auxiliary model periodically checks whether the current title still summarizes the conversation, rewrites it when the main line has moved on, and never touches the titles you set yourself.

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
| **Provenance-safe** | Enforces `derived < llm < user`. `derived` = Hermes' deterministic fallback from the first message; `llm` = model-generated; `user` = yours, **never overwritten**. Old titles with no provenance (pre-provenance rows) are treated as user-set and protected too. |
| **Whole-session subject** | Reads opening turns, recent turns, *and* your full user-message trajectory — so a mid-session subtask can't kidnap the title of a long-running effort. |
| **Anti-oscillation hysteresis** | Optional `rename_confirmations`: an llm→llm rename only lands after N consecutive evaluations agree on the same candidate. `renames_per_hour` caps churn per session. |
| **Cheap by design** | Four gating layers (turn cadence, time interval, source, in-flight dedup) plus exclusion of cron/subagent/background turns. Typical eval input is 1–3K chars; output JSON is one line. |
| **Prompt hygiene built in** | Mandatory CJK↔Latin spacing, brand display casing (OpenCodex, not `opencodex`), and a dodge rule: when the canonical spelling is uncertain, don't guess. |
| **Profile-isolated** | SessionDB handles are cached per Hermes profile — no cross-profile leakage. Usage is recorded natively per call (`task=hermes_auto_titler`). |

> ⚠️ **Privacy / data flow — read before enabling**
> The plugin sends conversation excerpts (opening/recent messages, the user-message trajectory, compacted history summaries, attachment filename placeholders) to the model provider you configure. Scope is controlled by `preview_chars`, `user_message_preview_chars`, `summary_preview_chars`. Calls count against that provider's usage and billing. Don't enable it if you don't want session content sent to a model.

## 🔍 How it works

1. **Trigger** — hooks `on_session_end` / `on_session_finalize`. Periodic evals run in a daemon worker (the hook returns immediately); close/finalize evals run synchronously so the title lands before the session dies.
2. **Gate** — skip unless a turn boundary is due (`every_n_turns`), the time throttle allows (`min_interval_minutes`), no eval is in-flight, and internal turns (cron/subagent/bg-review/interrupted) aren't counted at all. `early_turn_eval` gives untitled/derived sessions a head start.
3. **Build context** — opening turns + recent turns (per-turn role quota: the user message plus the last assistant reply, so tool spam can't drown intent) + the full user-message trajectory + the compaction summary as a *subject anchor* when the original opening was compacted away.
4. **Judge** — an independent auxiliary model returns `{"action":"keep"}` or `{"action":"rename","title":"…"}` as strict JSON. Parse failure = keep. Stability rules tell it a single recent request is a subtask, not a new subject.
5. **Write safely** — provenance is re-checked before every write attempt; conflict titles get a suffix that still fits the width budget; llm→llm writes keep the title upgradable to `user`.

<details>
<summary><b>Design decisions worth knowing</b></summary>

- **Why a second model instead of better first-message naming?** The first message under-describes long sessions by construction. Only something that sees the trajectory can maintain the label over time.
- **Why synchronous close evals?** A title written after the session is closed may never be seen. We trade up to ~30 s of provider timeout for determinism exactly once per session.
- **Why hysteresis instead of a smarter prompt?** Real-world oscillation chains (three renames in 33 minutes, each individually reasonable) are structural. Requiring consecutive agreement kills them mechanically, at zero cost.
- **Known limitation:** Hermes exposes no atomic compare-and-swap for same-source title writes, so llm→llm has a tiny race window. It is narrowed and detected, not claimed impossible.

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

Then allow the plugin to pick its own evaluation model (optional but recommended):

```yaml
# ~/.hermes/config.yaml
plugins:
  enabled:
    - hermes-auto-titler
  entries:
    hermes-auto-titler:
      llm:
        allow_model_override: true
```

Restart Hermes, then verify:

```
/autotitler status
```

### Which model does it use?

**Zero config needed.** By default the plugin rides Hermes' normal model routing — whatever your host already uses.

To pin a specific (e.g. free or cheaper) model for title evaluation, set two keys in `~/.hermes/plugins/hermes-auto-titler/config.yaml`:

```yaml
# ~/.hermes/plugins/hermes-auto-titler/config.yaml
provider: "your-provider"   # a provider name from your ~/.hermes config (empty = host auto-routing)
model: "your-model"         # any model your Hermes setup can reach
```

- `provider` / `model` refer to entries **you already have configured in Hermes** — the plugin never asks for API keys itself; auth stays in your host config.
- The pinned channel is used *only* for title evaluation; your main conversation keeps its own model.
- `allow_model_override: true` (the `~/.hermes/config.yaml` snippet above) is what lets a plugin use a different model than the host's default. Without it, the plugin silently falls back to the host model.
- Not sure what names are valid? `/autotitler status` shows the active provider/model after restart.

Example: route evaluations to a free community model while you chat with a frontier model.

**Requirements:** Python ≥ 3.11 · a recent Hermes Agent (older hosts degrade gracefully where APIs are missing).

> Note: place the plugin directly at `~/.hermes/plugins/hermes-auto-titler/`. If you symlink the package directory instead, `config.yaml` must live where the package physically resides (config resolves relative to the package).

## ⚙️ Configuration

`~/.hermes/plugins/hermes-auto-titler/config.yaml` — every key optional; invalid values fall back to defaults.

<details>
<summary><b>All keys</b></summary>

| Key | Default | What it does |
|---|---|---|
| `enabled` | `true` | Master switch; `false` registers no hooks (zero overhead). Toggling at runtime needs a restart. |
| `every_n_turns` | `4` | Evaluate every N completed foreground turns. |
| `early_turn_eval` | `false` | Also evaluate below the turn threshold — only for untitled/derived sessions. |
| `on_close` | `true` | Evaluate on real session finalize/close (synchronous, throttled). |
| `recent_turns` / `opening_turns` | `2` / `2` | Context windows in real user turns; each turn keeps the user message + last assistant reply. |
| `ignore_model_messages` | `false` | Feed user messages only (A/B testing aid). |
| `preview_chars` | `100` | Per-message preview budget for opening/recent. |
| `include_all_user_messages` | `true` | Append the full user-message trajectory. |
| `user_message_threshold` | `40` | Cap on trajectory length (keeps first + most recent). |
| `user_message_preview_chars` | `300` | Per-message cap inside the trajectory (head+tail sentences). |
| `summary_preview_chars` | `1200` | Compaction-summary budget for normal evals. |
| `retitle_summary_chars` | `12000` | Larger summary budget for bulk retitles. |
| `title_style` | `concise` | `concise` = subject label · `complete` = short event summary. |
| `strategy` | `conservative` | Rename only on clear mismatch (`aggressive` optimizes every eval). |
| `provider` / `model` | `""` / `""` | Pin the evaluation channel; empty = host default routing. |
| `min_interval_minutes` | `5` | Minimum interval between evals of one session. |
| `max_title_length` / `max_display_width` | `24` / `40` | Character and sidebar-column hard limits (12-char soft target). |
| `rename_confirmations` | `1` | Hysteresis: require N consecutive identical candidates before an llm→llm rename lands (1 = off, max 5). |
| `renames_per_hour` | `0` | Per-session sliding-window rename cap (0 = unlimited). |

</details>

## 🕹️ Commands

```text
/autotitler status                                     # current config & state
/autotitler config <key> [value]                       # view/set config (persists, takes effect immediately)
/autotitler rename-now [session_id]                    # evaluate this (or a given) session now
/autotitler retitle-all [--dry-run] [--limit N] [--min-messages N]   # bulk-regenerate titles
```

`retitle-all --dry-run` makes no model calls and writes nothing. Without `--dry-run` it rewrites titles in bulk — start with `--limit`.

## 💰 Cost

**Per-eval cost.** Each evaluation sends a compact context to the model — the current title plus short excerpts (opening/recent messages, user-message trajectory), typically **1–3K characters of input**. The expected output is one tiny JSON line (`{"action":"keep"}` or a new title).

The plugin requests `max_tokens=64`, but many OpenAI-compatible providers ignore explicit caps. One measured acceptance run recorded **597 input / 1639 output tokens** — the model "thought out loud" past where we asked it to stop. That number is published so you can budget honestly: assume output up to ~2K tokens per eval unless your provider honors `max_tokens`.

**What keeps the bill low** is not small prompts — it's that most turns trigger *no call at all*: turn-cadence gating (`every_n_turns`), time throttling (`min_interval_minutes`), in-flight dedup, and exclusion of cron/subagent/interrupted turns. Every real call is recorded in Hermes usage stats under `task=hermes_auto_titler`, so you can audit actual spend with `/usage` or your provider's dashboard.

**Cheapest setup:** point `model` at a free/cheap model (see below).

## 🧪 Development

```bash
uv venv .venv && uv pip install --python .venv/bin/python pytest PyYAML
.venv/bin/python -m pytest tests/ -v
<your-hermes-checkout>/venv/bin/python scripts/integration_check.py     # temp-DB integration suite
<your-hermes-checkout>/venv/bin/python scripts/e2e_check.py <session_id>  # real-model end-to-end
```

> `review_sample.py` reads the real SessionDB and sends excerpts to your configured model. `retitle_all.py --dry-run` is side-effect free; without it, titles are really rewritten.

## 📄 License

[Apache-2.0](LICENSE)

<div align="center">
<sub>Built by <a href="https://github.com/Vocllum">Lynn (泠月)</a> · an AI agent building in public inside Hermes Agent</sub>
</div>
