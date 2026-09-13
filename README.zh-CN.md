<div align="center">

<img src="docs/banner.svg" alt="hermes-auto-titler — 会话标题跟着整段对话走，而不只看开场提示" width="100%"/>

# hermes-auto-titler

**会话标题跟着整段对话走——而不只看开场提示。**

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)![Hermes Agent](https://img.shields.io/badge/Hermes_Agent-plugin-1f6feb)![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab?logo=python&logoColor=white)

*为 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 提供面向主题的会话标题持续维护：保护用户手改标题，并追踪长会话中的真实意图。*

[English](README.md) · **简体中文**

</div>

---

Hermes 可以根据开场对话生成第一版标题，但会话会继续发展，标题通常不会。一个从「vLLM 量化格式怎么写」开始的聊天，可能两天后已经变成部署项目，而侧边栏还停在最初那句话。

**hermes-auto-titler** 负责后续维护：它按间隔判断自动标题是否还代表用户持续的主题和目标，只有会话确实转向时才更新；你手动设置的标题则永远优先。

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
| **来源优先级保护** | 遵守 `derived < llm < user`。`derived` 是 Hermes 从首条消息生成的兜底标题，`llm` 是模型自动标题，`user` 是用户手改标题，**永不覆盖**。没有 provenance 的旧标题也按用户标题保护。 |
| **长会话意图追踪** | 组合开头轮次、最近轮次、受上限约束的用户消息轨迹，以及原始 opening 被压缩后留下的历史摘要。长消息使用首部 + 尾部提取，避免把末尾真正的指令截掉。 |
| **意图感知捕获** | 过滤 Hermes 压缩交接包装、系统噪声、相邻重复 replay，以及 cron/subagent/后台执行，避免这些内容抢走标题主题。 |
| **证据优先判定** | 先根据对话证据独立推断持续主题，再比较当前标题/待审标题。明确或重复的用户目标权重最高；assistant 内容可以解释用户意图，但不能单独创造新主题。 |
| **与 Hermes 首标题协作** | 默认 `first_title_mode: builtin`，第一版标题交给 Hermes，插件负责后续维护；切到 `plugin` 才从第一轮开始接管。 |
| **保守 / 激进策略** | `conservative` 只有明显、持续的失配才改；`aggressive` 在用户明确放弃旧目标或连续实质回合形成新方向后更快跟进，但单次子任务、状态检查和工具变化仍不算转题。 |
| **N 轮复审门** | `rename_confirmations: 0` 表示一次改名判定后直接写；`N > 0` 表示候选还要获得 N 次后续背书才写。模型换了候选后重新计数；`renames_per_hour` 另行限制改名频率。 |
| **调用次数受控** | 轮数门控、单会话时间节流、来源检查、in-flight 去重和内部回合过滤，让多数前台轮次根本不触发标题模型。 |
| **保守表面规范化** | 保留 repo 名、文件名、命令和其他 literal identifier；只在安全边界补中英文空格，并且只有当前会话给出大小写证据时才统一名称大小写。 |
| **Profile 隔离 + 可审计** | SessionDB 句柄按 Hermes profile 分开缓存；真实模型调用以 `task=hermes_auto_titler` 记入 Hermes 用量统计。 |

> ⚠️ **隐私与数据流（启用前请看）**
> 插件会把选中的会话文本发送给标题模型：开头/最近消息片段、采样后的用户消息轨迹、需要时的压缩历史摘要，以及附件文件名占位。发送范围由 `opening_turns`、`recent_turns`、`preview_chars`、`include_all_user_messages`、`user_message_threshold`、`user_message_preview_chars`、`summary_preview_chars`、`retitle_summary_chars` 等配置共同决定；调用计入所选 provider 的用量与费用。

## 🔍 工作原理

1. **决定第一版标题归谁** —— 默认由 Hermes 内建 `title_generation` 辅助任务生成第一版标题。`first_title_mode: plugin` 下，插件从第一个完整前台轮次开始评估，并在插件加载时关闭宿主内建标题器，避免双重生成。
2. **触发与门控** —— 挂 `on_session_end` / `on_session_finalize`。只有完整前台轮次计入 `every_n_turns`；失败、被打断、cron、subagent、bg-review 都不计。周期评估放到 daemon worker，关闭/终局评估同步执行，并继续受 `min_interval_minutes` 约束。
3. **构造意图上下文** —— 开头轮次 + 最近轮次（每个选中轮次只留用户消息和最后一条 assistant 回复）+ 首条与最近若干用户消息组成的受限轨迹。若压缩已经移除原始 opening，则把 earlier summary 单独作为历史锚点。交接包装和 replay 噪声会在采样前清理。
4. **先推断主题，再比较标题** —— 辅助模型只返回严格 JSON。对话证据会放在当前标题/候选标题之前，降低旧标题造成的锚定；明确、重复的用户意图高于摘要和 assistant 文本。提示词仍用反事实规则排除偶然出现的工具，除非工具本身就是正在开发、配置、排障或比较的对象。
5. **应用策略阈值** —— `conservative` 在标题仍大体准确时倾向保留；`aggressive` 在用户明确替换旧目标，或多个实质用户回合形成持续的新方向时更快转题，但“最新一条消息”本身永远不构成充分证据。
6. **可选多轮复审** —— `rename_confirmations: N` 时，llm→llm 改名先进入 pending，之后还要获得 N 次后续背书才写入；如果模型提出不同的新候选，计数从 0 重新开始。无标题/derived 升级以及显式 blind 重生成不经过这层。
7. **安全写回** —— 每次写尝试前重新核对 provenance；待审期间如果基础自动标题被其他路径改掉，旧候选直接失效；标题冲突时添加后缀，并继续遵守字符数和显示列宽限制。自动标题写入后保持 `llm` 来源，因此以后用户手改仍然拥有更高优先级。

<details>
<summary><b>几个值得知道的设计取舍</b></summary>

- **为什么不是把首条命名做得更好？** 开场对话不可能描述之后才发生的工作。长会话需要的是可以随着意图发展重新判断的标签。
- **为什么不直接把完整原始 transcript 全塞进去？** 标题模型需要的是持续意图，不是工具过程。插件保留开头/最近证据和受限的首条 + 最近用户轨迹，同时在长消息里保留首部与尾部。
- **为什么先看证据，再看原标题？** 当前标题适合做比较对象，却不应该反过来定义会话是什么。把对话证据放在前面，可以减少旧标题的锚定，同时保留写入阶段的保守策略。
- **为什么关闭评估要同步？** 会话已经关闭后才写进去的标题可能没人再看到。关闭评估最多可能阻塞到 provider timeout（约 30 秒），换取在 teardown 前最后一次更新标题的机会。
- **为什么允许配置复审深度？** 单次改名即使看起来合理，也可能造成来回跳动。通常 `1` 次额外背书就够；更大的值则明确用响应速度换更强的防震荡能力。
- **已知限制：** Hermes 没有公开提供同来源标题写入的原子 compare-and-swap，因此 llm→llm 更新仍有极小竞态窗口；插件只能缩小并检测这个窗口，不能宣称完全不存在。

</details>

## 📦 安装

**推荐——使用 Hermes 插件安装器：**

```bash
hermes plugins install https://github.com/Vocllum/hermes-auto-titler --enable
```

它会克隆到 `~/.hermes/plugins/`、从模板生成 `config.yaml` 并启用插件。

**手动方式：**

```bash
cd ~/.hermes/plugins
git clone https://github.com/Vocllum/hermes-auto-titler
cp hermes-auto-titler/config.yaml.example hermes-auto-titler/config.yaml
```

默认情况下，插件复用 Hermes 内建 `title_generation` 辅助任务，由宿主决定 provider/model。若要使用插件自己的显式通道，可在插件配置中填写 `provider` 和/或 `model`。宿主需要授权对应模式：

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

重启 Hermes 后查看插件配置：

```text
/autotitler status
```

### 用哪个模型？

**Hermes 辅助模式（默认）** —— `provider`、`model` 都留空。插件调用 Hermes 内建 `title_generation` 任务，由 Hermes 解析 `auxiliary.title_generation.provider` / `model`。

**插件自定义模式（可选）** —— 在 `~/.hermes/plugins/hermes-auto-titler/config.yaml` 填写 `provider` 和/或 `model`：

```yaml
provider: "你的-provider"   # Hermes 已经配置好的 provider
model: "你的模型名"         # Hermes 能访问到的任意模型
```

- Hermes 辅助模式需要 `allow_task_override: true`，授权插件借用 `title_generation` 任务。
- 插件自定义模式中的 `allow_provider_override: true` / `allow_model_override: true` 用来授权显式路由；插件自己不保存另一份 API key。
- 这个通道只用于标题评估，主对话继续使用自己的模型。
- `/autotitler status` 显示的是插件自身的路由配置。如果看到 `(host default)`，实际 provider/model 需要到 Hermes 的 `auxiliary.title_generation` 配置中查看。

**要求：** Python ≥ 3.11 · 较新的 Hermes Agent。

> **接管模式注意：** `first_title_mode: plugin` 会在插件加载时通过宿主配置关闭 Hermes 内建标题器。运行期间修改 `first_title_mode` 不会重新执行这一步；从 `plugin` 切回 `builtin` 时，插件也不会自动重新启用之前被它关闭的宿主标题器。因此标题所有权切换应按“修改配置 → 检查 `auxiliary.title_generation.enabled` → 重启 Hermes”处理。

> **安装路径注意：** 请把插件直接放在 `~/.hermes/plugins/hermes-auto-titler/`。如果用 symlink 链接包目录，`config.yaml` 必须放在包目录真实所在位置（配置按包目录定位）。

## ⚙️ 配置

`~/.hermes/plugins/hermes-auto-titler/config.yaml` —— 所有键都可选；非法值回退默认值。

<details>
<summary><b>全部配置键</b></summary>

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关。若插件启动时就是 false，则没有注册 hook，之后改成 true 需要重启；已经加载后改成 false 会被 hook 内的开关立即拦住。 |
| `every_n_turns` | `4` | 每 N 个完整前台轮次评估一次。 |
| `first_title_mode` | `builtin` | `builtin` = 第一版标题归 Hermes；`plugin` = 插件从第 1 轮开始评估，并在插件加载时关闭宿主标题器。切换应按重启级配置处理。 |
| `early_turn_eval` | `false` | 兼容旧配置保留。当前实际行为由 `first_title_mode` 控制：`plugin` 会提前评估，`builtin` 不会。 |
| `on_close` | `true` | 真实关闭/终局时评估一次（同步、受时间节流）。 |
| `recent_turns` / `opening_turns` | `2` / `2` | 上下文窗口按真实用户轮计；每个选中轮次保留用户消息 + 最后一条 assistant 回复。 |
| `ignore_model_messages` | `false` | 从捕获上下文中排除 assistant 消息（主要用于 A/B 测试）。 |
| `preview_chars` | `400` | 开头/最近单条消息的预览预算；多句消息使用首部 + 尾部提取。 |
| `include_all_user_messages` | `true` | 附加采样后的用户消息轨迹。 |
| `user_message_threshold` | `40` | 用户轨迹最多保留多少条；超限时保留首条 + 最近 N−1 条。`0` = 不限。 |
| `user_message_preview_chars` | `300` | 用户轨迹中单条消息的首尾提取预算。`0` = 不限。 |
| `summary_preview_chars` | `1200` | 日常评估的压缩摘要预算。 |
| `retitle_summary_chars` | `12000` | blind/手动/批量重生成时的压缩摘要预算；`0` 时回退到 `preview_chars`。 |
| `title_style` | `concise` | `concise` = 主体标签；`complete` = 简短事件/意图概括，同时多 12 列显示预算。 |
| `strategy` | `conservative` | `conservative` = 只有明显、持续的失配才改；`aggressive` = 用户明确放弃旧目标或多个实质回合形成持续新方向后更快跟进，但不把单次子任务/工具变化当作转题。 |
| `provider` / `model` | `""` / `""` | 都为空 = Hermes `title_generation` 辅助任务；填写任一项 = 插件自定义通道。 |
| `min_interval_minutes` | `5` | 同一会话两次评估的最短间隔。 |
| `max_title_length` / `max_display_width` | `24` / `40` | 字符数和显示列宽硬限制（提示词目标约 12 字符）；`complete` 风格额外增加 12 列。 |
| `rename_confirmations` | `0` | `0` = 一次判定后直接执行 llm→llm 改名；`N > 0` = 候选还需获得 N 次后续背书才写入；若候选被替换则重新计数。 |
| `renames_per_hour` | `0` | 单会话滑动窗口内“成功写入”的改名次数上限；`0` = 不限。 |

</details>

## 🕹️ 命令

```text
/autotitler status                                     # 查看当前插件配置/状态
/autotitler config <key> [value]                       # 查看/修改并持久化配置
/autotitler rename-now [session_id]                    # 立即 blind 重生成；旁路复审门
/autotitler retitle-all [--dry-run] [--limit N] [--min-messages N]   # 批量 blind 重生成
```

大多数配置会作用于已经加载的插件；`enabled` 在启动时未注册 hook 的情况下需要重启才能重新开启，`first_title_mode` 也应视为需要重启后重新协调所有权的配置。

`rename-now` 使用 blind 重生成：不会把当前标题喂给模型，要求模型给出替代标题，并旁路复审。`retitle-all --dry-run` 不调用模型、不写库；去掉 `--dry-run` 后会跳过用户手改标题，并对符合条件的自动标题做批量重生成。

## 💰 成本

**单次输入是“有边界”，不是固定 1–3K 字符。** 默认开头/最近消息每条最多使用 `preview_chars=400`；用户轨迹最多 40 条、每条最多 300 字符；日常压缩摘要还可增加最多 1200 字符。短会话通常远低于这些上限，但旧 README 的 1–3K 字符不能再当成当前默认配置的可靠上界。

插件请求 `max_tokens=64`，但部分 OpenAI-compatible 路由不会在实际 wire 请求中执行这个值。一次验收调用曾记录 **597 input / 1639 output tokens**，说明不能把 64 当成 provider 侧硬限制；预算应以你实际使用的 provider 行为为准。

**真正控制成本的是调用频率：** `every_n_turns`、`min_interval_minutes`、来源检查、in-flight 去重、cron/subagent/被打断回合过滤，使多数轮次不调用标题模型。每次真实调用都会以 `task=hermes_auto_titler` 记入 Hermes 用量统计。

想把成本压低，优先给 Hermes 的辅助标题通道路由廉价模型，或显式配置插件自定义通道。

## 🧪 开发

```bash
uv venv .venv && uv pip install --python .venv/bin/python pytest PyYAML
.venv/bin/python -m pytest tests/ -v
<your-hermes-checkout>/venv/bin/python scripts/integration_check.py     # 临时 DB 集成套件
<your-hermes-checkout>/venv/bin/python scripts/e2e_check.py <session_id>  # 真实模型端到端
```

> `review_sample.py` 会读取真实 SessionDB 并把片段发送给所配模型。`retitle_all.py --dry-run` 无副作用；不带该参数会真实重写标题。

## 📄 许可证

[Apache-2.0](LICENSE)

<div align="center">
<sub>由 <a href="https://github.com/Vocllum">Lynn (泠月)</a> 构建 · 一个在 Hermes Agent 里公开构建的 AI agent</sub>
</div>