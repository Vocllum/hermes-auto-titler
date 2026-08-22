# hermes-auto-titler

Hermes 插件：会话标题自动维护。每次对话结束后按配置的轮数间隔评估当前标题是否还准确，由独立模型判断并写回 Hermes session DB。

> **Privacy / data flow (read before enabling)**: the plugin sends conversation excerpts (opening/recent messages, the user-message trajectory, compacted history summaries, attachment filename placeholders) to the model provider you configure, for title evaluation. Scope is controlled by `preview_chars` / `user_message_preview_chars` / `summary_preview_chars`; calls count against that provider's usage and billing. Do not enable this plugin if you do not want session content sent to a model.

> **隐私与数据流（启用前必读）**：插件会把会话片段（开头/最近消息、用户消息轨迹、压缩历史摘要、附件文件名占位）发送给你配置的评估模型 provider，用于生成标题。发送范围由 `preview_chars` / `user_message_preview_chars` / `summary_preview_chars` 等开关控制，设小可收紧；调用计入该 provider 的用量与费用。不想发送任何会话内容请勿启用本插件。

## 特点

- **来源安全**：遵守标题来源优先级 `derived < llm < user`（derived = Hermes 从首条用户消息截出的临时兜底；llm = 模型自动标题；user = 用户手改，最高优先永不覆盖），无来源标记的 legacy 旧标题按用户标题同等保护
- **普通回合不阻塞**：周期/早期评估进入 daemon worker，hook 立即返回
- **从触发层省调用**：轮数、时间、来源、in-flight 四层门控，并排除 bg-review、cron、subagent、失败与打断回合
- **主线优先**：同时读取 opening、recent 和用户意图轨迹，避免标题被最后一个小任务带偏
- **Profile 与记账完整**：后台线程继承当前 Hermes profile Context，真实调用写入原生 auxiliary usage（Hermes 用量统计的任务维度记录；老宿主无该 API 时静默跳过）
- **标题边界稳健**：JSON fail-safe、专名规范书写、字符/列宽双限制、边界清洗和唯一性冲突后缀

## 工作原理

- 挂 `on_session_end` hook（每轮对话结束触发），按 `every_n_turns` 轮数节流评估；评估在后台 daemon 线程执行，hook 立即返回，同一会话同时最多一个评估在途（in-flight 去重）；worker 携带当前 Hermes profile Context（`tools.thread_context.propagate_context_to_thread`，老宿主退回标准库 contextvars）
- Hermes 内部执行不计入轮数：`bg-review` 后台回顾线程与 `platform=cron|subagent`（定时任务、子代理）的轮次直接忽略；被打断/失败/未完成（completed=False / failed / interrupted，即使标志不一致）的轮次同样不计
- 真实会话关闭/终局走 `on_session_finalize` lifecycle hook（或带非空 reason 的 `on_session_end`），正常节流（force=False），不重复进行中的自动评估；关闭评估刻意保持同步——可能阻塞至 provider timeout（最长约 30 秒），换取关闭前完成标题更新的确定性
- `early_turn_eval=true` 时未达轮数间隔的已完成轮次也评估，但仅限无标题或 `derived` 来源的会话（llm/user/legacy 在早期不额外调用模型；正常 `every_n_turns` 边界不受影响），受 `min_interval_minutes` 节流
- 评估输入：当前标题 + 会话开头/最近消息 +（可选）全部用户消息，只取纯文本，附件仅留占位符。Opening/Recent 按真实用户消息切轮，每个选中轮次只保留该用户消息和最后一条模型文本回复，避免工具复盘或连续 AI 残片挤占主题信号；压缩历史摘要只有在原始 opening 不可见时作为独立主题锚点提供。blind 重生成把它视为历史基线，并保留摘要后的真实用户续段来识别持续的新阶段，但不把该续段冒充 Opening，也不携带续段 assistant/recent 细节
- Hermes/cron/压缩系统注入（包括 `MEMORY_MAINTENANCE_SUMMARY` 与 task-list 续接标记）在进入模型前过滤
- 判断模型独立于主对话（默认宿主模型，可配任意可用模型），输出 `keep` / `rename` JSON；插件请求 `max_tokens=64`，但是否成为 wire 级硬上限取决于 Hermes/provider
- 写回遵守 Hermes 标题来源优先级：每次写尝试前都重新核对来源，正常路径优先保护用户手改标题；`derived` 是首条用户消息的确定性临时兜底，插件评估时始终要求升级为正式模型标题（不只处理超长首句）；自动标题保持可升级。`llm → llm` 因宿主公开 API 没有同级的原子条件写（CAS），只能临时 user 写入再恢复 llm 来源，最后仍有极小的跨步骤竞态窗口，不宣称绝对原子或绝对零覆盖
- 标题冲突（被其他会话占用）自动加后缀重试，后缀保证落在字符与列宽双上限内

## 安装

```bash
cd ~/.hermes/plugins
mkdir -p hermes-auto-titler
ln -s <repo>/plugin_entry/__init__.py hermes-auto-titler/__init__.py   # Hermes 要求插件根目录有 __init__.py
ln -s <repo>/hermes_auto_titler hermes-auto-titler/hermes_auto_titler
ln -s <repo>/plugin.yaml hermes-auto-titler/plugin.yaml
cp <repo>/config.yaml.example hermes-auto-titler/config.yaml             # Hermes 插件安装器也会自动复制 *.example 为真实文件
```

启用插件（修改 ~/.hermes/config.yaml）：

```yaml
plugins:
  enabled:
    - hermes-auto-titler
  entries:
    hermes-auto-titler:
      llm:
        allow_model_override: true   # 允许插件指定非宿主模型时需要
```

重启 Hermes 生效。验证：`/autotitler status`。

## 配置（插件目录 config.yaml）

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关；false 时 hook 不注册，零开销；运行期 false→true 只写配置，hook 注册在插件加载时决定，需重启 Hermes 生效 |
| `every_n_turns` | `4` | 每 N 轮对话结束评估一次 |
| `early_turn_eval` | `false` | true 时未达 `every_n_turns` 的已完成前台轮次也提交评估，但仅限无标题或 `derived` 来源的会话（llm/user/legacy 不提前调用模型；正常轮数边界不受影响）；受 `min_interval_minutes` 节流，快速连发轮次可能被节流；`every_n_turns=1` 时无额外效果 |
| `on_close` | `true` | 会话真实关闭/终局（`on_session_finalize` 或带非空 reason 的 `on_session_end`）时评估一次（正常节流，不重复进行中的自动评估）；普通打断不算关闭 |
| `recent_turns` | `2` | 每次评估携带最近 N 个真实用户轮次；每轮最多保留用户消息 + 最后一条模型文本回复（默认 2 轮：实测 1 轮无成本优势且更容易被结尾子任务带偏） |
| `opening_turns` | `2` | 额外携带会话开头 N 个真实用户轮次；采用同一角色配额，避免开头 AI 残片冒充主线（同上） |
| `ignore_model_messages` | `false` | true 时评估只喂用户消息，过滤模型回复（A/B 对比用） |
| `preview_chars` | `100` | 开头/结尾消息只保留前 N 字符（≈ 前几句话），全文梗概模式；0 = 不截断（实测 100 与 200 质量持平、输入省一半） |
| `include_all_user_messages` | `true` | 附加全部用户消息（不含附件内容） |
| `user_message_threshold` | `40` | 用户消息条数上限（0=不限）；超限保留开头 1 条 + 最近 N-1 条，防超长对话（普通会话用户消息 1~3 条不触发） |
| `user_message_preview_chars` | `300` | 单条用户消息超过该长度时提取首尾句（各限一半预算，保留意图与结论）（普通会话不触发） |
| `title_style` | `concise` | `concise` = LABEL 主体标签（Subject + 最小区分意图，不重述经过）/ `complete` = SUMMARY 简短事件梗概（Subject + 主要意图/事件）。信息类型是第一约束，长度只是护栏（12 目标 / `max_title_length` 硬限） |
| `retitle_summary_chars` | `12000` | 仅 retitle-all 全量重命名（盲改）生效：压缩摘要截断长度（0=沿用 `preview_chars`）。dry-run 不做模型调用，此项不适用。压缩会话以该摘要为历史基线，同时携带摘要后的真实用户续段；只有多条实质请求形成持续转向时，续段才可改写主线，单次收尾/验证/参数调整仍视为细节 |
| `strategy` | `conservative` | `conservative` 明显不匹配才改（优先开头主线）/ `aggressive` 每次优化（开头主线仍优先于最新子任务） |
| `provider` | `""` | 显式固定评估模型通道（如某个自定义 provider）；留空 = 宿主按 auto 路由（可能静默回退到别的模型） |
| `model` | `""` | 留空 = 宿主默认模型；也可指定任意你的 Hermes 配置里可用的模型 |
| `summary_preview_chars` | `1200` | 压缩历史摘要进入日常评估的截断长度（盲改用 `retitle_summary_chars`）；调小可减少发送的会话内容 |
| `min_interval_minutes` | `5` | 同一会话两次评估的最短间隔 |
| `max_title_length` | `24` | 标题最大字符数（12 为软目标；24 的硬上限用于完整保留较长的仓库/包/命令标识符；Hermes 上限 100） |
| `max_display_width` | `40` | 侧边栏显示列宽硬上限（中/全角 2 列，半角 1 列） |
| `rename_confirmations` | `1` | 滞后机制：llm→llm 改名需连续 N 次评估给出**相同候选**才写库（候选变化即重新计数，中途 keep 清空）。`1`=关闭（单次确认即写）；derived/无标题的首次升级与 close/retitle-all 盲改走旁路立即提交；钳制 1~5 |
| `renames_per_hour` | `0` | 单会话每小时实际改名次数上限（滑动 60 分钟窗口；被拦时返回 capped 并保留已确认候选，窗口滑过后直接写入）；`0`=不限 |

## 命令

```
/autotitler status                          # 当前配置与状态
/autotitler config <key> [value]            # 查看/修改配置（写回 config.yaml，立即生效）
/autotitler rename-now [session_id]         # 立即评估当前（或指定）会话
/autotitler retitle-all [--dry-run] [--limit N] [--min-messages N]   # 批量重生成所有标题
```

## 成本

单次评估的输入规模随会话而定（开头/最近轮 + 用户轨迹，各 preview 上限截断；正常会话约 1~3K 字符，压缩摘要盲改可能更长）。插件请求 `max_tokens=64`，但当前 Hermes auxiliary client 会对多数 OpenAI-compatible 路由省略显式上限；一次真实验收调用记到 `597 input / 1639 output`，因此不把 64 宣称为 provider 硬上限或成本收益。主要节省来自触发抑制：轮数节流（every_n_turns）+ 时间节流（min_interval_minutes）+ 同会话 in-flight 去重 + 内部执行排除（bg-review/cron/subagent 与打断轮次不触发调用）。每次真实调用经 `SessionDB.record_auxiliary_usage` 记入用量统计（task=hermes_auto_titler）。

## 要求

- Python ≥ 3.11
- Hermes Agent（插件依赖 `hermes_state` 的 SessionDB 标题来源 API 与 plugin hook 体系；老宿主缺少个别 API 时按能力探测降级，核心功能不受影响）

## 开发

```bash
uv venv .venv && uv pip install --python .venv/bin/python pytest PyYAML
.venv/bin/python -m pytest tests/ -v
<path-to-your-hermes-checkout>/venv/bin/python scripts/integration_check.py   # 临时 SessionDB 集成验证（路径按本机 Hermes 安装调整）
<path-to-your-hermes-checkout>/venv/bin/python scripts/e2e_check.py <session_id>  # 真实模型、写回与 usage 验证
```

> `review_sample.py` 会读取真实 SessionDB，并把会话片段发送给当前配置的模型；只在信任该 provider 且接受调用成本时运行。`retitle_all.py --dry-run` 不调用模型、不写标题，去掉 `--dry-run` 后会真实批量写库。

## 许可证

[Apache-2.0](LICENSE)
