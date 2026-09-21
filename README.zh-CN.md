<div align="center">

<img src="docs/banner.png" alt="hermes-auto-titler — 会话标题跟着整段对话走，而不只看第一条消息" width="100%"/>

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)![Hermes Agent](https://img.shields.io/badge/Hermes_Agent-plugin-1f6feb)![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab?logo=python&logoColor=white)

*为 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 提供面向主题的会话标题持续跟踪：跟踪意图演进，保护用户手改标题。*

[English](README.md) · **简体中文**

</div>

---

Hermes 可以根据开场对话生成第一版标题，但会话会发展，标题通常不会。一个从「怎么修那个……」开始的聊天，可能演变成紧急的 Redis 内存泄漏排查和生产部署，侧边栏标签还停在最初那条消息上。

**hermes-auto-titler** 在整个会话生命周期内持续维护标题。它定期评估自动标题是否反映会话走向，在主题演进时更新标签，且不覆盖手动设置的标题。

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

## ✨ 特点

| | |
|---|---|
| **来源优先级保护** | 遵守 `derived < llm < user`。`derived` 是 Hermes 从首条消息生成的兜底标题，`llm` 是模型自动标题，`user` 是用户手改标题，**永不覆盖**。没有来源记录的旧标题按用户标题保护。 |
| **长会话意图追踪** | 综合开头轮次、最近轮次、受限的用户消息轨迹，以及原始 opening 被压缩后留下的历史摘要。长消息使用首部 + 尾部提取保留末尾关键指令。 |
| **意图感知捕获** | 过滤压缩交接包装、系统噪声、相邻重复 replay，以及 cron/subagent/后台执行，防止干扰标题主题。 |
| **证据优先判定** | 先根据对话证据推断持续主题，再与当前或待审标题比较。明确且重复的用户目标权重最高；assistant 内容提供辅助上下文，但不能独立引入新主题。 |
| **首轮接管或协作** | 默认 `first_title_mode: plugin`，从第 1 轮开始评估（`pre_llm_call`），启动时关闭宿主内建标题器以消除竞态。设为 `builtin` 可将首标题交还宿主。 |
| **保守 / 激进策略** | `conservative` 要求明确、持续的失配才改名；`aggressive` 在用户明确放弃旧目标或多个实质回合形成新方向后更快跟进，单次子任务和工具切换视为瞬态噪声。 |
| **N 轮复审门** | 默认 `rename_confirmations: 1`，自动候选标题需获得一次后续背书才写入。`0` 立即生效；N 要求连续 N 次背书，换候选后计数重置。 |
| **可选改名上限** | `max_renames_per_session: 0` 默认关闭。设为 N 后每个会话最多自动替换标题 N 次。首次命名和手动 `rename-now` 不消耗额度。 |
| **自定义提示词插槽** | `custom_instructions` 将用户指定的文本追加到 system prompt 末尾，可用于语言或前缀约定等个人偏好。默认空。 |
| **调用次数受控** | 轮数门控、单会话冷却、来源校验、in-flight 去重和内部回合过滤，多数前台轮次不触发模型调用。 |
| **保守表面规范化** | 保留命令、路径、代码符号等字面标识；安全边界补中英文空格，仅在当前会话有直接证据时调整大小写。 |
| **Profile 隔离 + 可审计** | SessionDB 连接按 Hermes profile 隔离；所有标题生成调用以 `task=hermes_auto_titler` 记入用量统计。 |

> ⚠️ **隐私与数据流（启用前请看）**
> 插件会将选中的会话片段发送给标题模型：开头与最近轮次、采样后的用户消息轨迹、压缩历史摘要以及附件文件名占位。采样范围由 `opening_turns`、`recent_turns`、`preview_chars`、`include_all_user_messages`、`user_message_threshold`、`user_message_preview_chars`、`summary_preview_chars`、`retitle_summary_chars` 等配置决定；模型请求计入所选 provider 的用量与费用。

## 🔍 工作原理

1. **首标题归属** — 默认 `first_title_mode: plugin`，插件从第 1 轮开始评估，启动时自动关闭宿主标题器以防竞态。设为 `builtin` 可将首标题交还宿主。
2. **触发、节奏与关闭处理** — Hook `on_session_end` / `on_session_finalize`。只有完整前台轮次计入 `every_n_turns`（默认 `2`）；失败、被打断、cron、subagent、后台任务均排除。周期评估异步执行；关闭 hook 不发起网络请求，只对已有 worker 等待最多 100ms，随后把 typed finalize intent 原子持久化，交由下一段正常进程生命周期续跑。
3. **上下文构造** — 提取开头轮次、最近轮次（每个用户消息配对最后一条 assistant 回复），以及受限的首条与最近用户消息轨迹。若压缩已移除原始 opening，则以压缩摘要作为历史锚点。协议交接包装和重放噪声在采样前清理。
4. **证据优先评估** — 辅助模型输出结构化 JSON。对话证据排在当前标题之前以降低锚定偏差。明确且重复的用户意图权重最高；assistant 回复提供辅助上下文但不能独立引入新主题。跨采样区间的结构性重复被显式折扣。
5. **策略评估** — `conservative` 在无显著、持续的主题偏移时保留现有标题；`aggressive` 在用户明确放弃旧目标或持续追求新方向时更快适应，但单靠最近几轮不足以触发改名。
6. **多轮确认门** — `rename_confirmations: N` 下，llm→llm 改名先作为待审候选挂起，需在后续 N 次评估中获得确认。出现不同新候选时计数从 0 重新开始。首次命名、derived 升级和手动 `rename-now` 不经过此门。
7. **安全写回** — 写入前立即重新校验 provenance。底层标题被其他路径修改后待审候选自动失效。冲突标题通过后缀处理并遵守列宽限制；写入保持 `llm` 来源，用户手改标题始终拥有更高优先级。

<details>
<summary><b>几个值得知道的设计取舍</b></summary>

- **为什么要持续维护，而不把首条命名做得更好？** 开场对话无法预见后续走向。长会话需要标题能随用户实际目标演化。
- **为什么采样意图轨迹，而不把完整 transcript 全塞进去？** 标题模型需要的是持续意图，不是工具执行过程。插件保留开头与最近上下文及受限用户消息轨迹，长消息用首尾提取保留关键指令。
- **为什么先看证据再看原标题？** 现有标题适合做比较基线，但不是好的证据来源。先分析对话证据可以避免锚定在过时标签上。
- **为什么关闭时入队而不是调用模型？** Hermes 的 finalize 有硬时间预算。关闭 hook 因此不发起网络请求，只等待已有工作最多 100ms，并持久化带 epoch 的 finalize intent；retry worker 在正常生命周期继续执行，且仍须经过 provenance 与复审门禁。
- **为什么默认复审 1 次？** 单次改名可能反映临时偏离。要求一次后续背书是防止标题抖动的合理防线；设 `rename_confirmations: 0` 可立即更新，增大 N 提高稳定性。
- **已知限制：** Hermes 未提供会话标题的原子 compare-and-swap API，并发写入存在极小竞态窗口。插件缩小并检测这些碰撞。

</details>

## 📦 安装

**推荐——使用 Hermes 插件安装器：**

```bash
hermes plugins install https://github.com/Vocllum/hermes-auto-titler --enable
```

克隆到 `~/.hermes/plugins/`、从模板生成 `config.yaml` 并启用插件。

**手动方式：**

```bash
cd ~/.hermes/plugins
git clone https://github.com/Vocllum/hermes-auto-titler
cp hermes-auto-titler/config.yaml.example hermes-auto-titler/config.yaml
```

默认复用 Hermes 内建 `title_generation` 辅助任务，由宿主决定 provider/model。若要使用插件自己的通道，在插件配置中填写 `provider` 和/或 `model`。宿主需要授权：

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

重启 Hermes 后验证：

```text
/autotitler status
```

### 用哪个模型？

**Hermes 辅助模式（默认）** — `provider`、`model` 都留空。插件调用 Hermes `title_generation` 任务，由 Hermes 解析 `auxiliary.title_generation.provider` / `model`。

**插件自定义模式（可选）** — 在 `~/.hermes/plugins/hermes-auto-titler/config.yaml` 填写 `provider` 和/或 `model`：

```yaml
provider: "你的-provider"   # Hermes 已经配置好的 provider
model: "你的模型名"         # Hermes 能访问到的任意模型
```

- Hermes 辅助模式需要 `allow_task_override: true`，授权插件借用 `title_generation` 任务。
- 插件自定义模式的 `allow_provider_override: true` / `allow_model_override: true` 用来授权显式路由；插件不保存另一份 API key。
- 这个通道只用于标题评估，主对话继续使用自己的模型。
- `/autotitler status` 显示插件自身的路由配置。看到 `(host default)` 时，实际 provider/model 需要到 Hermes 的 `auxiliary.title_generation` 配置中查看。

**模型建议。** 插件在小型、快速的指令遵循模型上即可高效运行。优先关注一致的 JSON 输出、多语言理解和指令遵循，而非重型推理能力。建议先用 Hermes 原有的 `title_generation` 辅助通道。部署超轻量模型前，先用 `scripts/review_sample.py` 对自己的会话历史做评估——出现主题漂移、JSON 格式错误或长会话中的多语言标题质量下降时，换到更强的辅助模型。

**要求：** Python ≥ 3.11 · 较新版本的 Hermes Agent。

> **接管模式注意：** `first_title_mode: plugin` 在插件加载时通过宿主配置关闭 Hermes 内建标题器。运行期间改 `first_title_mode` 不会重新执行这一步；从 `plugin` 切回 `builtin` 也不会自动重新启用之前被关闭的宿主标题器。标题所有权切换应按"改配置 → 检查 `auxiliary.title_generation.enabled` → 重启 Hermes"处理。

> **安装路径注意：** 请把插件直接放在 `~/.hermes/plugins/hermes-auto-titler/`。用 symlink 链接包目录时，`config.yaml` 必须放在包目录真实所在位置（配置按包目录定位）。

## ⚙️ 配置

`~/.hermes/plugins/hermes-auto-titler/config.yaml` — 所有键可选；非法值回退默认值。

<details>
<summary><b>全部配置键</b></summary>

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关。启动时为 false 则没有注册 hook，之后改 true 需要重启；已加载后改 false 会被 hook 内开关立即拦住。 |
| `every_n_turns` | `2` | 每 N 个完整前台轮次评估一次。 |
| `first_title_mode` | `plugin` | `plugin` = 从第 1 轮评估，加载时关闭宿主标题器；`builtin` = 第一版标题归 Hermes。切换按重启级配置处理。 |
| `early_turn_eval` | `false` | 兼容旧配置保留。实际行为由 `first_title_mode` 控制。 |
| `on_close` | `true` | 关闭/终局时最多等待已有工作 100ms，并持久化 typed finalize intent；关闭 hook 不发起网络请求。 |
| `recent_turns` / `opening_turns` | `2` / `2` | 上下文窗口按真实用户轮计；每个选中轮次保留用户消息 + 最后一条 assistant 回复。 |
| `ignore_model_messages` | `false` | 从捕获上下文中排除 assistant 消息（主要用于 A/B 测试）。 |
| `preview_chars` | `400` | 开头/最近单条消息的预览预算；多句消息使用首部 + 尾部提取。 |
| `include_all_user_messages` | `true` | 附加采样后的用户消息轨迹。 |
| `user_message_threshold` | `40` | 用户轨迹最多保留多少条；超限时保留首条 + 最近 N−1 条。`0` = 不限。 |
| `user_message_preview_chars` | `300` | 用户轨迹中单条消息的首尾提取预算。`0` = 不限。 |
| `summary_preview_chars` | `1200` | 日常评估的压缩摘要预算。 |
| `retitle_summary_chars` | `12000` | blind/手动/批量重生成时的压缩摘要预算；`0` 时回退到 `preview_chars`。 |
| `title_style` | `concise` | `concise` = 主体标签；`complete` = 简短事件/意图概括，多 12 列显示预算。 |
| `strategy` | `conservative` | `conservative` = 只有明显、持续的失配才改；`aggressive` = 用户明确放弃旧目标或多个实质回合形成持续新方向后更快跟进，单次子任务/工具变化不算转题。 |
| `provider` / `model` | `""` / `""` | 都为空 = Hermes `title_generation` 辅助任务；填写任一项 = 插件自定义通道。 |
| `min_interval_minutes` | `5` | 同一会话两次评估的最短间隔。 |
| `max_title_length` / `max_display_width` | `null` / `40` | `null` 不强加代码层字符硬切，由提示词保持标题简洁，`max_display_width` 限制显示列宽；`complete` 风格额外增加 12 列。 |
| `rename_confirmations` | `1` | 默认再要求 1 次后续背书；`0` = 一次判定后直接写；`N > 1` = 需要 N 次后续背书。候选被替换则重新计数。 |
| `max_renames_per_session` | `0` | 默认关闭。`N > 0` = 每个会话最多自动替换标题 N 次。首次命名和 `rename-now` 不消耗次数；`derived` 升级算一次替换。计数持久化保存于 `state.json`。 |
| `custom_instructions` | `""` | 追加到 system prompt 末尾的自定义指令。如 `"标题使用英文"` 或 `"始终包含项目名前缀"`。留空无影响。 |

</details>

## 🕹️ 命令

```text
/autotitler status                                     # 查看当前配置/状态
/autotitler config <key> [value]                       # 查看/修改并持久化配置
/autotitler rename-now [session_id]                    # 立即 blind 重生成；旁路复审门
/autotitler retitle-all [--dry-run] [--limit N] [--min-messages N]   # 批量 blind 重生成
```

大多数配置即时生效。启动时未注册 hook 的情况下 `enabled` 需要重启，`first_title_mode` 按启动级所有权开关对待。

`rename-now` 执行 blind 重生成：不向模型展示当前标题以消除锚定，同时跳过确认门。`retitle-all --dry-run` 模拟批量更新但不产生 API 调用或修改数据；去掉 `--dry-run` 后保留用户手改标题，仅对符合条件的自动标题重生成。

## 💰 成本

**单次评估输入有边界，不是固定大小。** 默认开头/最近消息每条 `preview_chars=400`；用户轨迹保留最多 40 条、每条最多 300 字符；压缩摘要还可增加最多 1,200 字符。短会话消耗远低于上限，但携带完整轨迹的长会话可能超过 1–3K 字符基线。

插件请求 `max_tokens=64`，但部分 OpenAI-compatible 路由可能不会在上游严格执行输出限制。基准测试中，未受约束的模型产生了 **597 input / 1,639 output tokens**（内部推理 token 先于 JSON 输出）。应按实际 provider 的 token 计费行为规划预算，不要假定 64 output tokens 是硬上限。

**节奏门控是主要的成本防线：** 轮次间隔检查（`every_n_turns`）、单会话冷却（`min_interval_minutes`）、来源校验、in-flight 去重和后台轮次过滤共同避免冗余调用。每次调用以 `task=hermes_auto_titler` 记入 Hermes 用量统计。

轻量辅助模型是预期的成本画像，但应在自己的真实会话上验证——尤其是长会话和压缩会话。

## 🧪 开发

```bash
uv venv .venv && uv pip install --python .venv/bin/python pytest PyYAML
.venv/bin/python -m pytest tests/ -v
<your-hermes-checkout>/venv/bin/python scripts/integration_check.py
<your-hermes-checkout>/venv/bin/python scripts/review_sample.py --n 20
<your-hermes-checkout>/venv/bin/python scripts/review_sample.py --n 20 --strategy aggressive
<your-hermes-checkout>/venv/bin/python scripts/e2e_check.py <session_id>
```

`review_sample.py` 以 dry-run 模式运行，完整复用生产配置（preview 长度、轨迹限制、摘要预算）。

`prompt_acceptance.py` 是隔离实验工具：采样真实的非用户标题会话，重放按时序递进的对话前缀（如第 1、2、4 和最终轮），在相同前缀上评估提示词变体，可选使用独立 LLM 评分器打分。默认通过 Hermes 的 `PluginLlm` 桥接使用 `title_generation` 任务；可通过 `HERMES_AUTOTITLER_EXPERIMENT_*` 环境变量配置原始 OpenAI-compatible 端点。对 SessionDB 严格只读，生成和评分过程中隐藏现有标题。

`retitle_all.py --dry-run` 预览批量重生成而不调用模型或修改数据库。去掉 `--dry-run` 后保留用户手改标题，仅更新符合条件的自动标题。

## 📄 许可证

[Apache-2.0](LICENSE)

<div align="center">
<sub>由 <a href="https://github.com/Vocllum">Vocllum</a> 构建</sub>
</div>
