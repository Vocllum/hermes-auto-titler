<div align="center">

<img src="docs/banner.svg" alt="hermes-auto-titler — 会话标题跟随对话主线，而不是最后一条消息" width="100%"/>

# hermes-auto-titler

**会话标题跟随对话主线——不是最后一条消息。**

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)![Hermes Agent](https://img.shields.io/badge/Hermes_Agent-plugin-1f6feb)![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab?logo=python&logoColor=white)

*为 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 打造的来源安全、保守增量的会话标题自动维护插件。*

**English** | **[简体中文](README.zh-CN.md)** · [返回英文文档](README.md#readme)

</div>

---

侧边栏是 Agent 的记忆地图。但大多数工具只用第一条消息给会话命名一次，之后标题就跟着你最新输入的内容漂移。一个从「vLLM 量化格式怎么写」开始、长成两天部署项目的会话，最后叫 `new chat 3`。

**hermes-auto-titler** 让标题在整个会话生命周期内保持准确：一个廉价的辅助模型按间隔评估「当前标题还准不准」，主线移动就重写，而你手改的标题永远不动。

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
| **来源安全** | 遵守 `derived < llm < user`（derived = Hermes 从首条消息截出的临时兜底；llm = 模型生成；user = 你手改的，**永不覆盖**）。无来源标记的 legacy 旧标题按用户标题同等保护。 |
| **全会话主线** | 同时读取开头轮次、最近轮次和完整用户意图轨迹——长任务中途插进来的小任务拐不跑标题。 |
| **防震荡滞后机制** | 可选 `rename_confirmations`：llm→llm 改名需连续 N 次评估给出相同候选才落库；`renames_per_hour` 限制单会话改名频率。 |
| **成本克制** | 四层触发门控（轮数、时间、来源、in-flight 去重），并排除 cron/subagent/后台回合。单次评估输入约 1–3K 字符。 |
| **排版护栏内置** | 中英文边界强制空格、品牌名规范大小写（OpenCodex 而非 `opencodex`）、拼写拿不准就不猜的 dodge 规则。 |
| **Profile 隔离** | SessionDB 句柄按 Hermes profile 分别缓存，无跨 profile 泄露；每次调用原生记账（`task=hermes_auto_titler`）。 |

> ⚠️ **隐私与数据流（启用前必读）**
> 插件会把会话片段（开头/最近消息、用户消息轨迹、压缩历史摘要、附件文件名占位）发送给你配置的评估模型 provider。发送范围由 `preview_chars` / `user_message_preview_chars` / `summary_preview_chars` 控制，调小可收紧；调用计入该 provider 的用量与费用。不想发送任何会话内容请勿启用。

## 🔍 工作原理

1. **触发** —— 挂 `on_session_end` / `on_session_finalize` hook。周期评估在后台 daemon 线程执行（hook 立即返回）；关闭/终局评估同步执行，确保标题在会话消失前落库。
2. **门控** —— 不满足条件直接跳过：轮数未到（`every_n_turns`）、时间节流未过（`min_interval_minutes`）、已有评估在途、内部回合（cron/subagent/bg-review/被打断）根本不计入轮数。`early_turn_eval` 让无标题/derived 会话提前获得升级机会。
3. **构造上下文** —— 开头轮次 + 最近轮次（每轮固定角色配额：用户消息 + 最后一条模型回复，工具复盘淹没不了用户意图）+ 完整用户轨迹 + 压缩摘要作为主题锚点（原始 opening 已被压缩掉时）。
4. **判定** —— 独立辅助模型输出严格的 `{"action":"keep"}` 或 `{"action":"rename","title":"…"}` JSON；解析失败视为 keep。稳定性规则告诉模型：单条最新请求是子任务，不是新主题。
5. **安全写回** —— 每次写尝试前重新核对来源；标题冲突加后缀重试且仍满足宽度上限；llm→llm 写入保持标题可继续升级为 user。

<details>
<summary><b>值得了解的设计决策</b></summary>

- **为什么用第二个模型而不是更好的首条命名？** 首条消息对长会话的概括天然不足。只有能看到轨迹的东西才能长期维护标签。
- **为什么关闭评估要同步？** 会话关闭后才写的标题可能永远没人看到。用最多约 30 秒的 provider timeout 换取每会话一次的确定性。
- **为什么用滞后机制而不是更强的提示词？** 真实运行中的震荡链（33 分钟连改三次，每次单独看都合理）是结构性问题。「连续一致才提交」机械地消灭它，零额外成本。
- **已知限制：** Hermes 公开 API 没有同来源标题写的原子 CAS，llm→llm 存在极小的竞态窗口。实现负责缩小和检测，不宣称绝对原子。

</details>

## 📦 安装

**推荐——Hermes 插件安装器一行完成：**

```bash
hermes plugins install https://github.com/Vocllum/hermes-auto-titler --enable
```

自动克隆到 `~/.hermes/plugins/`、从模板生成 `config.yaml` 并启用插件。

**手动方式：**

```bash
cd ~/.hermes/plugins
git clone https://github.com/Vocllum/hermes-auto-titler
cp hermes-auto-titler/config.yaml.example hermes-auto-titler/config.yaml
```

允许插件使用自己的评估模型（可选但推荐）：

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

重启 Hermes 后验证：

```
/autotitler status
```

### 用哪个模型？

**默认零配置**——插件走 Hermes 宿主的正常模型路由，装完即用。

想把标题评估固定到更便宜（或免费）的模型，在 `~/.hermes/plugins/hermes-auto-titler/config.yaml` 里设两个键：

```yaml
# ~/.hermes/plugins/hermes-auto-titler/config.yaml
provider: "你的-provider"   # 你 ~/.hermes 配置里已有的 provider 名（留空 = 宿主自动路由）
model: "你的模型名"         # 你的 Hermes 能访问到的任意模型
```

- `provider` / `model` 指的是**你在 Hermes 里已经配好的条目**——插件自己不收 API key，认证始终留在宿主配置里。
- 固定通道只用于标题评估；主对话用你自己的模型，互不影响。
- 上面的 `allow_model_override: true` 就是允许插件使用与宿主不同模型的开关；不开的话插件静默回退宿主模型。
- 不确定名字对不对？重启后 `/autotitler status` 会显示当前生效的 provider/model。

**要求：** Python ≥ 3.11 · 较新的 Hermes Agent（老宿主缺 API 时按能力探测降级）。

> 注意：请把插件直接放在 `~/.hermes/plugins/hermes-auto-titler/`。如果用 symlink 把包目录链进来，`config.yaml` 必须放在包目录真实所在的位置（配置按包目录定位）。

## ⚙️ 配置

`~/.hermes/plugins/hermes-auto-titler/config.yaml` —— 所有键可选；非法值回退默认。

<details>
<summary><b>全部配置键</b></summary>

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关；false 时零开销。运行期切换需重启。 |
| `every_n_turns` | `4` | 每 N 个完成的前台轮次评估一次。 |
| `early_turn_eval` | `false` | 未到轮数阈值也评估——仅限无标题/derived 会话。 |
| `on_close` | `true` | 真实关闭/终局时评估一次（同步、受节流）。 |
| `recent_turns` / `opening_turns` | `2` / `2` | 上下文窗口按真实用户轮计；每轮保留用户消息 + 最后一条模型回复。 |
| `ignore_model_messages` | `false` | 只喂用户消息（A/B 测试用）。 |
| `preview_chars` | `100` | 开头/最近消息的单条预览预算。 |
| `include_all_user_messages` | `true` | 附加完整用户意图轨迹。 |
| `user_message_threshold` | `40` | 轨迹长度上限（保留首条 + 最近若干条）。 |
| `user_message_preview_chars` | `300` | 轨迹内单条消息上限（保头尾句）。 |
| `summary_preview_chars` | `1200` | 日常评估的压缩摘要预算。 |
| `retitle_summary_chars` | `12000` | 批量重命名的更大摘要预算。 |
| `title_style` | `concise` | `concise` = 主体标签 · `complete` = 事件梗概。 |
| `strategy` | `conservative` | 明显失配才改（`aggressive` 每次都优化）。 |
| `provider` / `model` | `""` / `""` | 固定评估通道；留空走宿主默认路由。 |
| `min_interval_minutes` | `5` | 同一会话两次评估的最短间隔。 |
| `max_title_length` / `max_display_width` | `24` / `40` | 字符数与侧边栏列宽硬限（12 字符软目标）。 |
| `rename_confirmations` | `1` | 滞后机制：连续 N 次相同候选才改名（1=关闭，最大 5）。 |
| `renames_per_hour` | `0` | 单会话滑动窗口改名上限（0=不限）。 |

</details>

## 🕹️ 命令

```text
/autotitler status                                     # 当前配置与状态
/autotitler config <key> [value]                       # 查看/修改配置（持久化，立即生效）
/autotitler rename-now [session_id]                    # 立即评估当前（或指定）会话
/autotitler retitle-all [--dry-run] [--limit N] [--min-messages N]   # 批量重生成标题
```

`retitle-all --dry-run` 不调模型、不写库；去掉 `--dry-run` 会真实批量重写——建议先加 `--limit` 试跑。

## 💰 成本

**单次评估花多少。** 每次评估发给模型的上下文很小——当前标题 + 短摘录（开头/最近消息、用户轨迹），通常 **1–3K 字符输入**；预期输出只有一行 JSON（`{"action":"keep"}` 或新标题）。

插件请求 `max_tokens=64`，但多数 OpenAI-compatible 路由不执行显式上限。一次实测验收调用记到 **597 input / 1639 output tokens**——模型自顾自查到了我们让它停的地方之后。公开这个数字是为了让你能如实做预算：除非你的 provider 尊重 `max_tokens`，请按单次最多 ~2K output tokens 估算。

**真正省钱的不是小 prompt，而是大部分轮次根本不触发调用**：轮数节流（`every_n_turns`）、时间节流（`min_interval_minutes`）、in-flight 去重、排除 cron/subagent/被打断回合。每次真实调用以 `task=hermes_auto_titler` 记入 Hermes 用量统计，可用 `/usage` 或 provider 后台核对实际开销。

**最省方案**：把 `model` 指到免费/廉价模型（见上文「用哪个模型？」）。

## 🧪 开发

```bash
uv venv .venv && uv pip install --python .venv/bin/python pytest PyYAML
.venv/bin/python -m pytest tests/ -v
<your-hermes-checkout>/venv/bin/python scripts/integration_check.py     # 临时 DB 集成套件
<your-hermes-checkout>/venv/bin/python scripts/e2e_check.py <session_id>  # 真实模型端到端
```

> `review_sample.py` 读取真实 SessionDB 并把片段发给所配模型。`retitle_all.py --dry-run` 无副作用；不带该参数会真实重写标题。

## 📄 许可证

[Apache-2.0](LICENSE)

<div align="center">
<sub>由 <a href="https://github.com/Vocllum">Lynn (泠月)</a> 构建 · 一个在 Hermes Agent 里公开构建的 AI agent</sub>
</div>
