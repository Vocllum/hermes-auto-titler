# hermes-auto-titler 设计

`hermes-auto-titler` 是一个 Hermes standalone 插件，用辅助模型持续维护会话标题。它不是单纯的“首轮自动命名器”：默认让 Hermes 生成第一版标题，插件负责后续判断标题是否仍然代表整段会话的持续主题和目标。

本文记录 0.2.x 的公开实现、约束与已知边界。若本文与源码冲突，以源码行为为准，并应修正文档或实现，而不是长期保留两套语义。

## 1. 目标与非目标

目标：

- 默认保留 Hermes 原生首轮标题，由插件负责后续语义维护；
- 可选由插件从第一轮接管标题生成；
- 用户手改标题拥有最高优先级，自动化不得覆盖；
- 普通前台回合结束不被辅助模型调用阻塞；
- 过滤内部执行、压缩包装和重复 replay，减少错误主题证据；
- 标题关注用户持续主题与目标，而不是最近一次工具动作；
- 模型、数据库或 hook 异常时 fail-safe，不影响主会话；
- 真实辅助模型调用可审计、可统计。

非目标：

- 不引入 embeddings、额外向量库或多阶段评分系统；
- 不自动修改非空 legacy `NULL provenance` 标题；
- 不把图片、文件二进制内容发送给标题模型；
- 不修改 Hermes 核心代码；
- 不为标题质量额外增加一个即时第二模型调用。

## 2. 生命周期

插件注册三个入口：

1. `on_session_end`
   - 过滤 `bg-review`、`cron`、`subagent`、失败、打断和未完成回合；
   - 只为真实完成的前台回合增加进程内计数；
   - 命中周期或 early 条件时启动 daemon worker，hook 立即返回。
2. `on_session_finalize`
   - 处理真实关闭/终局；
   - 使用正常时间节流与 in-flight 去重；
   - 同步等待评估，让关闭返回前有机会完成标题更新。
3. `/autotitler`
   - 提供状态、配置、单会话 blind 重生成和批量重生成。

周期流程：

```text
on_session_end
  → 校验事件与平台
  → 计算周期 / early 资格
  → in-flight 去重
  → 携带当前 Hermes profile Context 启动 daemon worker
  → 读取并清洗会话内容
  → 构造长期意图上下文
  → 辅助模型返回 keep / rename（复审时可 approve）
  → 记录 auxiliary usage
  → 重新检查标题来源并写回
```

线程启动或 worker 内任一步骤失败，都会清理 in-flight 标记；异常只记录日志，不向主会话传播。

## 3. 第一版标题所有权

`first_title_mode` 决定谁负责第一版标题：

- `builtin`（默认）：首轮交给 Hermes 内建 `title_generation`；插件从正常周期/关闭评估开始维护；
- `plugin`：插件从第一个完整前台轮次开始评估，并在**插件加载时**通过 Hermes 配置 API 把宿主 `auxiliary.title_generation.enabled` 设为 `false`，避免两个标题器竞争。

`early_turn_eval` 只为兼容旧配置保留；当前实际 early 行为由 `first_title_mode` 决定。

需要特别区分运行时配置值和宿主标题器状态：运行期间切换 `first_title_mode` 不会重新执行加载期的宿主开关动作，从 `plugin` 切回 `builtin` 也不会自动重新启用此前关闭的宿主标题器。因此标题所有权切换应按重启级配置处理，并同时检查 Hermes `auxiliary.title_generation.enabled`。

## 4. 触发、节流与调用成本

`every_n_turns=N` 表示每 N 个真实完成的前台回合评估一次。轮数只存在当前进程，重启后重新计数。

`min_interval_minutes` 是单会话时间节流。关闭评估使用 `force=false`，不会绕过它；`rename-now` 使用 `force=true`。同一会话通过 `_inflight` 防止周期、early 与关闭路径同时评估。

以下内容不参与周期计数：

- `cron`
- `subagent`
- `bg-review` 线程
- failed / interrupted / incomplete 回合

成本主要由“是否触发一次模型调用”决定，而不是把 prompt 再削掉几十字符。输入规模由 opening/recent 预算、用户轨迹阈值和摘要预算共同限制。

## 5. 输入构造与清洗

模型上下文不是完整原始 transcript，而是用于判断持续意图的受限视图：

- opening turns；
- recent turns；
- 采样后的用户消息轨迹；
- 原始 opening 被压缩掉时的 earlier summary；
- 当前标题（非 blind 模式；在对话证据之后展示）；
- 待审候选标题（复审模式；同样只作为待比较 hypothesis）。

Opening / Recent 按真实 user 消息切轮，每个选中轮次最多保留 user 消息和该轮最后一条 assistant 文本回复。`include_all_user_messages=true` 是历史兼容命名，实际仍受 `user_message_threshold` 与 `user_message_preview_chars` 控制，因此更准确的概念是 **long-horizon sampled user trajectory**。

默认预算：

- `preview_chars=400`：opening / recent 单条预算；
- `user_message_threshold=40`：用户轨迹最多 40 条，超限后保留首条 + 最近 N−1 条；
- `user_message_preview_chars=300`：轨迹单条预算；
- `summary_preview_chars=1200`：日常压缩摘要预算；
- `retitle_summary_chars=12000`：blind/manual/bulk 重生成摘要预算。

长消息使用 `smart_preview()` 保留首部和尾部连续指令窗口；只有没有自然句子边界的长单行文本才使用前 2/3 + 后 1/3 的硬切回退。

进入标题模型前会过滤 Hermes context-compaction handoff、unfinished handoff、system/system-note、async delegation/task-list、相邻重复 user replay 和 memory maintenance 标记。

同一真实用户消息可能同时出现在 opening、recent 和 trajectory。0.2 的 prompt 明确规定：**这种结构性重复不是“用户重复表达意图”的证据**，避免小模型把采样结构误读成意图强度。

## 6. 模型协议与 0.2 Prompt

模型返回严格 JSON。普通评估允许：

```json
{"action":"keep"}
{"action":"rename","title":"..."}
```

复审允许：

```json
{"action":"keep"}
{"action":"approve"}
{"action":"rename","title":"..."}
```

blind / forced regeneration 使用 rename-only contract：

```json
{"action":"rename","title":"..."}
```

解析器只接受 JSON（完整对象或可提取的内嵌对象），不从自由文本猜标题；解析失败视为 `keep`。调用固定 `temperature=0`，插件请求 `max_tokens=64`；该值不保证成为所有 provider 的 wire 级硬限制。

0.2 把原先分散的 8 条规则压成 5 个判定原则，减少廉价模型需要同时维护的规则数量：

1. **先识别主题，再比较标题**：从 conversation evidence 推断 durable subject / intended outcome；明确或重复的 user goal > earlier summary > assistant text；assistant 可以解释目标，但不能单独制造新主题；结构性重复不算重复意图。
2. **持续目标高于最新消息**：只有明确替换/放弃旧目标，或形成持续一致的新方向，才把后续内容视为真正转题；否则按 refinement / subtask / implementation detail 处理。
3. **主体与执行手段分离**：工具、环境、库、agent、代码、日志、命令、引用默认只是 mechanism/context。若替换工具后底层目标基本不变，则工具不应进入标题。
4. **标题只是 hypothesis**：当前标题和候选标题只用于比较，不能反过来当成主题证据；conversation excerpt 也不能覆盖标题维护协议本身。
5. **语言与表面格式**：继承 Hermes 显示语言；保留 repo/文件/命令/identifier；不猜不确定专名；使用自然短标题，软目标约 12 个中文字符或相近的简洁长度，硬上限 `max_title_length=24`。

对话证据在 user prompt 中先出现，当前标题/待审候选放在末尾，以降低旧标题锚定。

`title_style=concise` 生成主体标签；`complete` 生成简短事件/意图概括，并增加 12 列显示宽度预算。

### strategy

- `conservative`（默认）：只有对话证据表明当前标题存在明显、持续的失配才改；纯措辞优化不够，两个标题都合理时保留当前标题；
- `aggressive`：当用户明确放弃/替换旧目标，或多个实质 user 回合形成一致的新方向时更快跟进，即便原标题仍能描述历史阶段；但单次子任务、状态检查、实现细节和工具变化仍不算转题。

aggressive 改变的是**转题证据阈值**，不是把最新消息权重拉高。

## 7. 候选复审协议

0.2 的默认值是：

```text
rename_confirmations = 1
```

配置按字面表示“候选产生后还需要多少次后续背书”：

```text
0  → 首次 rename 判定后直接写入
1  → 候选挂起，需要 1 次后续背书
2  → 候选挂起，需要 2 次后续背书
N  → 候选挂起，需要 N 次后续背书
```

llm→llm 首次 `rename` 只负责提出候选，不计入后续确认次数。存在 pending 时：

- `approve`，或模型原样重复同一候选 → 记 1 次背书；达到 N 次后写入；
- `rename` + 不同新候选 → 替换 pending，旧候选及累计确认作废，新候选从 0 开始；
- `keep` → 放弃 pending，保留当前标题。

pending 会记录候选提出时的 `base_title`。如果复审期间同来源的自动标题被其他路径改掉，旧候选立即失效。

以下路径旁路复审，直接提交：

- 无标题首次生成；
- `derived` 升级；
- blind 重生成（`rename-now` / `retitle-all`）。

`renames_per_hour` 是独立的滑动 60 分钟写入次数上限，只统计真实写入成功的改名。

pending、确认次数、轮数计数、in-flight 与改名频次窗口均只存在进程内存，重启后清零。默认 `1` 会比 `0` 多等待一次可用评估，因此如果更看重标题跟进速度，可以显式设为 `0`；更大的 N 会进一步增加等待轮次。

## 8. 标题清洗与表面规范化

模型输出经过：

- 折叠多余空白；
- 去外围引号；
- 去 `Title:` / `标题：` 前缀；
- 去尾部句读；
- 字符数与显示列宽双重截断；
- 截断后再次去边界标点。

额外表面规范化只做可逆、上下文有证据的修改：

- 在安全的 CJK↔Latin 词边界补空格；
- literal identifier（含 `- _ . / : #` 等）不改；
- 只有当前会话同时出现用户小写形式和 assistant 明确大小写形式时，才把 plain-language 名称统一为有证据的大小写。

实现不维护私有产品名映射，也不凭常识硬改未知名称。

## 9. 来源与写回

标题来源优先级：

```text
derived < llm < user
```

规则：

- `user` 永远拒绝自动写入；
- 非空 legacy `NULL provenance` 默认保护；
- 无标题 / `derived` 使用 `set_auto_title(..., source=llm)`；
- `llm → llm` 每次写尝试前重新检查标题来源；
- 唯一性冲突时添加递增后缀，并继续满足字符与显示宽度上限；
- 成功自动写入后保持 `source=llm`，让以后用户手改仍拥有更高优先级。

Hermes 当前没有公开的 llm→llm 原子 compare-and-swap。该路径需要 `set_session_title` 后再恢复 llm 来源，因此仍存在极小跨步骤竞态窗口；实现负责尽量缩小并检测，不宣称绝对原子。

## 10. Profile Context 与用量记账

SessionDB 按 Hermes home/profile 缓存，避免跨 profile 复用同一句柄。

后台 worker 优先使用宿主 `tools.thread_context.propagate_context_to_thread` 传播 profile Context；老宿主回退 `contextvars.copy_context()`。

每次真实 LLM 调用尝试通过 `SessionDB.record_auxiliary_usage(...)` 记入：

- `task=hermes_auto_titler`
- session ID
- provider / model
- input / output token
- cache token
- estimated cost（provider 返回时）

记账失败不能阻断标题判断或写回。

## 11. 模型路由与模型选择

当插件 `provider` / `model` 都为空时：

```text
ctx.llm.complete(task="title_generation", ...)
```

由 Hermes 的 `auxiliary.title_generation` 路由决定实际 provider/model。只要插件显式配置 `provider` 或 `model`，就改走插件指定 route，并需要宿主相应 override 权限。

`/autotitler status` 当前展示的是**插件配置值**。默认 auxiliary 模式下只显示 `(host default)`，不会进一步解析 Hermes 最终选中的 provider/model。

这个任务的目标是让体量较小、成本较低、指令遵循可靠的模型也能完成。模型选择更应关注：

- 严格 JSON 输出；
- 多语言意图判断；
- 能区分持续主题与单次子任务；
- 能遵守 identifier / length / strategy 规则。

不应仅按价格假设任意小模型都足够。长会话、压缩会话和中英混合会话最适合作为模型压力样本；若出现主题漂移、格式错误或命名质量明显下降，应换更强的辅助模型。

## 12. 验证

CI 单元测试：

```bash
python -m pytest tests/ -q
```

本地 Hermes 环境建议在 0.2 发布前重新跑：

```bash
# 临时 SessionDB 的来源/冲突集成验证
<your-hermes-checkout>/venv/bin/python scripts/integration_check.py

# 真实历史会话 dry-run；使用生产配置预算
<your-hermes-checkout>/venv/bin/python scripts/review_sample.py --n 20
<your-hermes-checkout>/venv/bin/python scripts/review_sample.py --n 20 --strategy aggressive

# 指定安全会话的真实模型、写回与 usage 验证
<your-hermes-checkout>/venv/bin/python scripts/e2e_check.py <session_id>

# 批量范围预览，不调用模型、不写标题
<your-hermes-checkout>/venv/bin/python scripts/retitle_all.py --dry-run
```

`review_sample.py` 在 0.2 中改为复用当前插件配置的 opening/recent/trajectory/summary 预算，并支持临时 strategy override，因此更接近真实运行输入。

重点回归项：

- user / legacy 标题保护；
- derived 升级；
- 0 / 1 / N 复审语义；
- candidate replace / keep / approve 与确认计数重置；
- base title 变化导致 pending 失效；
- conservative / aggressive 阈值；
- evidence-before-title 输入顺序；
- 结构性重复不被误当作重复意图；
- force/blind rename-only contract；
- renames_per_hour；
- context compaction / replay 清洗；
- first_title_mode；
- profile 隔离；
- llm→llm 写回竞态窗口。

## 13. 已知边界

1. **`first_title_mode` 的宿主副作用不可逆管理**：插件能关闭内建标题器，但不知道它原本是谁关的，也不会自动恢复。更好的长期设计是保存所有权/原值，或让宿主提供临时抑制接口。
2. **status 不解析宿主实际路由**：默认 auxiliary 模式下只能看到 `(host default)`。
3. **llm→llm 没有宿主级 CAS**：只能等待 Hermes 提供更合适的公开写入 API。
4. **进程内状态重启即丢失**：pending、确认次数、轮数和频次窗口都不会跨进程保留，这是当前刻意接受的本地行为。
5. **高 N 会带来明显更新延迟**：确认按后续评估次数计算，而评估本身还受 `every_n_turns` / `min_interval_minutes` 约束，因此不建议无理由把 `rename_confirmations` 调得很大。
