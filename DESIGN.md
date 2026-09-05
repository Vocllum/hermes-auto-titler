# hermes-auto-titler 设计

`hermes-auto-titler` 是一个 Hermes standalone 插件，在会话主线发生变化或当前标题不足以概括整体任务时，用独立辅助模型维护标题。

本文只记录公开实现所需的架构、约束和验收方法。历史提示词实验、私人会话样本与表格结果不属于仓库内容。

## 1. 目标

- 保留 Hermes 原生首轮确定性标题，由插件负责后续语义升级。
- 优先保护用户手改标题，自动化不得降低标题来源优先级。
- 普通回合结束不能被辅助模型调用阻塞。
- 减少重复、内部和无意义调用，成本优化优先发生在触发层。
- 模型、数据库或 hook 异常时 fail-safe，不影响主会话。
- 标题短、稳定、可检索，并满足字符数和显示列宽限制。

非目标：

- 不接管 Hermes 首轮标题生成。
- 不引入 embeddings、标题缓存数据库或多阶段评分管线。
- 不自动升级旧的非空 `NULL provenance` 标题。
- 不修改 Hermes 核心代码或新增桌面设置页。

## 2. 生命周期

插件注册三个入口：

1. `on_session_end`
   - 过滤 `bg-review`、`cron`、`subagent`、失败、打断和未完成回合。
   - 只为真实完成的前台回合增加进程内计数。
   - 命中周期或 early 条件时启动 daemon worker，hook 立即返回。
2. `on_session_finalize`
   - 处理真实关闭/终局。
   - 使用正常时间节流与 in-flight 去重。
   - 刻意同步等待评估，以换取关闭返回前标题已完成写回。
3. `/autotitler`
   - 提供状态、配置、单会话立即评估和批量重命名命令。

普通周期流程：

```text
on_session_end
  → 校验事件与平台
  → 计算周期 / early 资格
  → in-flight 去重
  → 携带当前 Hermes profile Context 启动 daemon worker
  → 读取会话内容
  → 辅助模型返回 keep/rename JSON
  → 记录辅助模型 usage
  → 重新检查标题来源并写回
```

线程启动或 worker 内任一步骤失败，都会清理 in-flight 标记；异常只记录日志，不向主会话传播。

## 3. 触发策略

### 周期评估

`every_n_turns=N` 表示每 N 个真实完成的前台回合评估一次。计数仅存在于当前进程，重启后从零开始；它不是业务状态，不需要持久化。

### Early 评估（一键开关 `first_title_mode`）

`first_title_mode=plugin` 时插件第 1 轮就接管全上下文命名（等价旧
`early_turn_eval=true`）；`builtin`（默认）时首轮归内建 `title_generation`，
插件 early 强制失效，只从正常周期/关闭评估开始维护：

- `user`、`llm` 和非空 legacy `NULL provenance` 不触发 early 调用；
- 正常周期边界仍按 `every_n_turns` 工作；
- 仍受 `min_interval_minutes` 和 in-flight 去重约束；
- `every_n_turns=1` 时没有额外效果。

默认 `builtin`，避免首轮内建（~150 token，只看首条消息）与插件全上下文
（~1500 token）重复调用；旧 `early_turn_eval` 保留兼容，`plugin` 模式下
等价 true，`builtin` 模式下强制失效。

### 关闭评估

`on_close=true` 时，真实 finalize 或带关闭原因的 end 事件可以评估。普通中断不等于关闭。关闭评估使用 `force=false`，因此不会绕过时间节流，也不会在周期 worker 正在运行时重复调用。

`rename-now` 是唯一明确使用 `force=true` 的人工命令；批量 `retitle-all` 仍遵守正常节流。

## 4. 输入构造

模型输入由四部分组成：

- 当前标题及来源；
- 会话 opening turns；
- recent turns；
- 可选的全部用户意图轨迹。

Opening 和 Recent 按真实用户消息切轮。每个选中的轮次只保留该用户消息与轮内最后一条模型文本回复；首条用户消息之前的 assistant 残片不属于任何轮次，直接排除。这个固定角色配额防止工具过程、连续模型复盘或压缩残片按文本体量压过用户意图，同时不增加配置维度。

只提取文本；图片、文件等附件保留占位信息，不把二进制内容送入模型。系统维护注入、cron 包装、task-list 压缩续接标记和 `MEMORY_MAINTENANCE_SUMMARY` 等噪声在进入模型前过滤。

当原始 opening 已不可见时，压缩摘要必须作为独立主题锚点，不能伪装成原始用户消息；摘要后的可见 Opening 只是局部续段。`retitle-all` 的盲改模式没有当前标题锚点，因此把更长的摘要作为历史基线，同时保留摘要后的真实用户续段，但清空该续段的伪 Opening、assistant/recent 细节。模型只有在多条实质请求形成持续转向时才允许续段取代或扩展旧主线；单次收尾验证、当前 blocker 或参数微调不能抢走标题。

超长用户轨迹使用“首条 + 最近若干条”采样；单条长消息保留首尾，以兼顾最初意图和最终约束。

## 5. 模型协议

模型只允许返回：

```json
{"action":"keep"}
```

```json
{"action":"approve"}
```

或：

```json
{"action":"rename","title":"..."}
```

`approve` 只在评审模式（存在待审候选，见 §7）的提示词中出现并生效；解析器始终接受它，非评审模式下 `approve` 防御性视为 `keep`。解析器先尝试完整 JSON，再尝试提取包含 `action` 的内嵌 JSON 对象。不从自由文本猜标题；解析失败直接 `keep`。

调用固定 `temperature=0`，插件请求 `max_tokens=64`。这个值是 PluginLlm API 参数，不保证成为 provider 的 wire 级硬上限：Hermes 当前会为部分 OpenAI-compatible 路由省略显式 cap。因此成本结论不能建立在“输出最多 64 token”上。

## 6. 标题规范

标题清洗包含：

- 提示模型在最终输出前规范 plain-language 专名的大小写与标准空格，明确的仓库、包、文件和命令标识符保持原拼写；标题使用会话主导语言，`ingest` / `deploy` / `config` 等通用技术词在主导语言非英语时用该语言表达；
- 12 字符是软目标，不得为凑短而缩写、截残词组；必要时使用完整硬上限；
- 去除代码围栏、引号、`Title:` / `标题：` 前缀和尾部标点；
- 同时限制 `max_title_length` 字符数与 `max_display_width` 显示列宽；
- 截断后再次清理边界标点；
- 候选标题与当前标题相同则视为 `keep`；
- 唯一性冲突时添加递增后缀，并重新满足字符与列宽双上限。

`title_style=concise` 生成主体标签；`complete` 生成简短事件梗概。两者都以会话开头建立的主线为基调，recent 只能补充当前状态，不能单独替换 Subject。

## 7. 评审协议（防标题震荡）

单次 LLM 判断天然偏向最近消息；真实运行中出现过同一会话 33 分钟内被连改三次的震荡链。此外，「连续 N 次逐字节相同候选才写库」的旧滞后设计在真实模型下几乎无法满足——候选措辞的微小差异让 pending 永远不达标，标题长期不变化。

现行机制是**候选评审协议**：

- `rename_confirmations=1`（默认）：单次评估直接改名，协议关闭。
- `rename_confirmations=N (N>1)`：llm→llm 的 rename 先成为**待审候选**（不写库）；下一次评估把候选以 `Proposed title:` 与当前标题并列给模型看，模型三选一裁决：
  - `approve`（或原样重复候选）→ 背书，候选落库；
  - `rename` + 更好的新标题 → 替换待审候选，旧候选作废；
  - `keep` → 放弃候选，保留当前标题。
- 提示词评审规则要求「把准确的当前标题换成更窄/更不完整的提案不算改进」，与保守取向一致。
- 旁路：derived/无标题升级是补漏、blind 是 retitle-all 终局评估（全貌已知），均立即提交，并忽略进程内待审候选。
- `renames_per_hour`（默认 0 = 不限）：滑动 60 分钟窗口内的实际改名硬顶。触顶返回 `capped` 并**保留待审候选**，窗口滑过后模型背书即可写入。
- pending/频次状态仅存于进程内存：重启清零，与轮数计数同级，不持久化。
- 用户权威在评审期间落地时清空 pending；候选与当前标题相同视为 stale，直接放弃。

配套提示词规范（日常评估注入，盲改不注入）：

- 主题只随「多条后续用户请求证实的持续转向」改变，单条最新消息只是子任务；
- 多主体各有实质覆盖时应合并概括，把准确宽标题改成更窄标题视为漂移；
- 当前标题中的标识符仍是主题一部分时必须保留；
- 两个选项都站得住时倾向 keep。

## 8. 来源与并发安全

标题来源优先级保持为：

```text
derived < llm < user
```

写回规则：

- 无标题或 `derived` 使用 Hermes 原生 `set_auto_title(..., source=llm)`；`derived` 是首句确定性兜底，无论长短都要求本次模型生成正式标题；
- `user` 永远拒绝自动写入；
- 非空 legacy `NULL provenance` 默认保护；
- `llm → llm` 在每次写尝试前重新读取标题和来源；
- 写入后只有候选标题仍然匹配时才恢复 `source=llm`。

宿主公开 API 目前没有“同级来源 + 旧标题”的原子 CAS。`llm → llm` 因此仍有一个极小的跨步骤竞态窗口；实现负责缩小和检测该窗口，但不宣称绝对原子或绝对零覆盖。

同一进程内使用锁保护的 session in-flight 集合，防止周期、early 和关闭路径重复评估。SessionDB 的 lazy 初始化同样在锁内完成，避免并发首次访问创建多个实例。

## 9. Profile Context 与用量记账

后台 worker 必须继承当前 Hermes profile Context，否则可能读写错误 profile 的 SessionDB。优先使用宿主 `tools.thread_context.propagate_context_to_thread`；老宿主回退标准库 `contextvars.copy_context()`。

每次真实 LLM 调用通过 `SessionDB.record_auxiliary_usage(...)` 记入：

- `task=hermes_auto_titler`
- session ID
- provider / model
- input / output / total tokens
- cache、reasoning 和 estimated cost（provider 返回时）

记账失败不能阻断标题写回；旧宿主或测试 fake 没有该 API 时安全跳过。

## 10. 配置边界

配置加载对布尔、整数、有限浮点、枚举和字符串分别做严格归一化：

- `true/false` 不接受任意 truthy 数值；
- 整数项拒绝小数、NaN、Infinity 和错误容器类型；
- `recent_turns`、`opening_turns` 至少为 1；
- 长度与阈值项不允许负数；
- `strategy`、`title_style` 只接受已声明枚举；
- 无效 YAML 或无效字段回退默认值。

`enabled=false` 时插件加载阶段不注册 hook，做到零运行开销。运行中从 false 改回 true 需要重启 Hermes 才能重新注册。

## 11. 成本原则

成本主要由是否调用模型决定，不由 prompt 中某个小参数决定。触发层依次使用：

- 前台完成回合过滤；
- 周期节流；
- 时间节流；
- 来源门控；
- 同会话 in-flight 去重；
- bg-review / cron / subagent 排除。

输入侧再用 opening/recent preview、用户轨迹阈值和摘要预算限制规模。项目不把 provider 未兑现的 `max_tokens` 请求当作成本收益。

## 12. 验证

```bash
# 单元与边界测试
.venv/bin/python -m pytest tests/ -q

# 临时 SessionDB 的真实来源/冲突集成验证
~/.hermes/hermes-agent/venv/bin/python scripts/integration_check.py

# 指定安全会话的真实模型、写回与 usage 验证
~/.hermes/hermes-agent/venv/bin/python scripts/e2e_check.py <session_id>

# 批量范围预览，不调用模型、不写标题
~/.hermes/hermes-agent/venv/bin/python scripts/retitle_all.py --dry-run
```

验收时除测试结果外，还要核对：插件可被 Hermes 发现、部署 symlink 指向当前仓库、真实 usage 行存在、临时验证数据已清理、运行进程启动时间晚于最终源码修改时间。

## 13. 已知边界

- `llm → llm` 没有宿主级原子 CAS。
- 关闭评估可能同步等待到 provider timeout；普通轮次不受影响。
- `max_tokens=64` 对部分 provider 只是请求提示。
- 进程内轮数、in-flight、滞后 pending 与频次窗口状态在重启后清零。
- legacy 非空 `NULL provenance` 标题默认不自动升级。
