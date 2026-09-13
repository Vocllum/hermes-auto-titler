# hermes-auto-titler 设计

`hermes-auto-titler` 是一个 Hermes standalone 插件，用辅助模型持续维护会话标题。它不是单纯的“首轮自动命名器”：默认让 Hermes 生成第一版标题，插件负责后续判断标题是否仍然代表整段会话的持续主题和目标。

本文记录 v0.1.3 当前公开实现、约束与已知边界。若本文与源码冲突，以源码行为为准，并应修正文档或实现，而不是长期保留两套语义。

## 1. 目标

- 默认保留 Hermes 原生首轮标题，由插件负责后续语义维护。
- 可选由插件从第一轮接管标题生成。
- 用户手改标题拥有最高优先级，自动化不得覆盖。
- 普通前台回合结束不被辅助模型调用阻塞。
- 过滤内部执行、压缩包装和重复 replay，减少错误主题证据。
- 标题关注用户持续主题与目标，而不是最近一次工具动作。
- 模型、数据库或 hook 异常时 fail-safe，不影响主会话。
- 对真实辅助模型调用记录可审计 usage。

非目标：

- 不引入 embeddings、额外向量库或多阶段评分系统。
- 不自动修改非空 legacy `NULL provenance` 标题。
- 不把图片、文件二进制内容发送给标题模型。
- 不修改 Hermes 核心代码。

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
  → 辅助模型返回 keep / rename（评审时可 approve）
  → 记录 auxiliary usage
  → 重新检查标题来源并写回
```

线程启动或 worker 内任一步骤失败，都会清理 in-flight 标记；异常只记录日志，不向主会话传播。

## 3. 第一版标题所有权

`first_title_mode` 决定谁负责第一版标题：

- `builtin`（默认）：首轮交给 Hermes 内建 `title_generation`；插件从正常周期/关闭评估开始维护。
- `plugin`：插件从第一个完整前台轮次开始评估，并在**插件加载时**通过 Hermes 配置 API 把宿主 `auxiliary.title_generation.enabled` 设为 `false`，避免两个标题器竞争。

`early_turn_eval` 只为兼容旧配置保留；当前实际 early 行为由 `first_title_mode` 决定。

需要特别区分“运行时配置值”和“宿主标题器状态”：

- 运行期间把 `builtin` 改成 `plugin`，会改变插件自己的 early 判定，但不会重新执行加载期的宿主关闭动作；
- 从 `plugin` 改回 `builtin`，插件也不会自动重新开启此前关闭的宿主标题器；
- 因此标题所有权切换应按重启级配置处理，并同时检查 Hermes `auxiliary.title_generation.enabled`。

这是当前实现的显式边界，不应在文档中描述成完全可热切换。

## 4. 触发与节流

`every_n_turns=N` 表示每 N 个真实完成的前台回合评估一次。轮数只存在当前进程，重启后重新计数。

`min_interval_minutes` 是单会话时间节流。关闭评估使用 `force=false`，不会绕过它；`rename-now` 使用 `force=true`。

同一会话通过 `_inflight` 防止周期、early 与关闭路径同时评估。

以下内容不参与周期计数：

- `cron`
- `subagent`
- `bg-review` 线程
- failed / interrupted / incomplete 回合

## 5. 输入构造与清洗

模型上下文不是完整原始 transcript，而是用于判断持续意图的受限视图：

- opening turns；
- recent turns；
- 采样后的用户消息轨迹；
- 原始 opening 被压缩掉时的 earlier summary；
- 当前标题（非 blind 模式）。

### 5.1 轮次采样

Opening / Recent 按真实 user 消息切轮。每个选中轮次最多保留：

- user 消息；
- 该轮最后一条 assistant 文本回复。

这样可以避免工具过程或连续模型复盘按文本体量淹没用户意图。

### 5.2 用户轨迹

`include_all_user_messages=true` 的名字是历史兼容命名，实际仍受采样参数控制：

- `user_message_threshold=40`：超限后保留首条 + 最近 N−1 条；`0` 表示不限制条数；
- `user_message_preview_chars=300`：单条长消息使用首部 + 尾部提取；`0` 表示不限制单条预览。

因此更准确的概念是 **long-horizon sampled user trajectory**，而不是完整原文轨迹。

### 5.3 长消息

`preview_chars=400` 是 opening / recent 的默认单条预算。

多句长消息通过 `smart_preview()` 保留：

- 第一部分，用于实体与起始语境；
- 尾部连续指令窗口，用于保留用户最终约束。

没有自然句子边界的长单行文本才使用前 2/3 + 后 1/3 的硬切回退。

### 5.4 压缩与系统噪声

进入标题模型前会过滤：

- Hermes context-compaction handoff 包装；
- unfinished handoff；
- system/system-note 等系统注入；
- async delegation / task-list 等维护文本；
- 相邻重复 user replay；
- memory maintenance 标记。

如果原始 opening 已不可见，压缩摘要不会伪装成真实 user 消息，而是作为独立历史锚点：

- 日常评估默认 `summary_preview_chars=1200`；
- blind/manual/bulk 重生成默认 `retitle_summary_chars=12000`。

blind 且存在 earlier summary 时，会保留摘要后的真实用户轨迹，同时清空该续段的伪 opening 与 assistant/recent 细节，减少收尾执行内容抢主题。

## 6. 模型协议与标题目标

模型只允许返回：

```json
{"action":"keep"}
```

评审模式还允许：

```json
{"action":"approve"}
```

或：

```json
{"action":"rename","title":"..."}
```

解析器只接受 JSON（完整对象或可提取的内嵌对象），不从自由文本猜标题；解析失败视为 `keep`。

调用固定 `temperature=0`，插件请求 `max_tokens=64`。这个值是 PluginLlm API 请求参数，不保证成为所有 provider 的 wire 级硬限制。

当前提示词核心规则：

1. **Objective over Recency**：优先持续主题和目标，不因最新一条消息自动改题；
2. **Instrument vs Subject**：工具、环境、库、agent 默认只是执行手段，只有工具本身是开发/配置/排障/比较对象时才进入标题；
3. **Context vs Intent**：代码、日志、命令、引用默认是上下文，标题关注用户最终想完成什么；
4. **Language**：继承 Hermes 标题/显示语言；
5. **Formatting**：literal identifier 保持原样，不猜不确定专名。

`title_style=concise` 生成主体标签；`complete` 生成简短事件/意图概括，并增加 12 列显示宽度预算。

`strategy` 仍保留 `conservative/aggressive` 配置枚举，但 v0.1.3 当前 `_generate()` 不读取它；两个值走同一套上述判定。它目前属于兼容配置，而不是有效行为开关。

## 7. 候选复审协议

v0.1.3 的配置语义已经从旧版本迁移为：

- `rename_confirmations=0`（默认）：llm→llm 的 rename 在一次判定后直接写入；
- `rename_confirmations=1`：首次 rename 只产生 pending 候选；下一次评估把它与当前标题一起交给模型复审：
  - `approve`，或原样重复同一候选 → 写入；
  - `rename` + 不同新候选 → 用新候选替换 pending；
  - `keep` → 放弃 pending，保留当前标题。

以下路径旁路复审，直接提交：

- 无标题首次生成；
- `derived` 升级；
- blind 重生成（`rename-now` / `retitle-all`）。

`renames_per_hour` 是独立的滑动 60 分钟写入次数上限，只统计真实写入成功的改名。触顶返回 `capped`；当前 pending 候选会保留，窗口过去后可再次尝试。

### 当前多轮计数边界

配置层目前接受任意非负整数 `rename_confirmations`，但 `evaluate()` 只判断是否 `>=1`，pending 也没有保存累计确认次数。因此：

```text
rename_confirmations = 0    → 无复审
rename_confirmations >= 1   → 当前都只要求 1 次额外复审
```

也就是说，`2`、`3` 等值目前**不会**要求两轮、三轮连续背书。这是 v0.1.3 的实现边界；如果未来要让数值表达“额外确认轮数”，必须给 pending 增加确认计数并补对应测试，而不是只改配置说明。

pending、轮数计数、in-flight 与改名频次窗口均只存在进程内存，重启后清零。

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

## 11. 模型路由

当插件 `provider` / `model` 都为空时：

```text
ctx.llm.complete(task="title_generation", ...)
```

由 Hermes 的 `auxiliary.title_generation` 路由决定实际 provider/model。

只要插件显式配置 `provider` 或 `model`，就改走插件指定 route，并需要宿主相应 override 权限。

`/autotitler status` 当前展示的是**插件配置值**。默认辅助模式下它只会显示 `(host default)`，不会进一步解析 Hermes 最终选中的 provider/model。

## 12. 成本原则

成本主要由“是否触发一次模型调用”决定，而不是把 prompt 再削掉几十字符。

触发层依次使用：

- 完整前台回合过滤；
- 周期门控；
- 时间节流；
- 来源保护；
- 同会话 in-flight 去重；
- cron / subagent / bg-review 排除。

输入规模由 opening/recent 预算、用户轨迹阈值和摘要预算共同限制。由于默认用户轨迹最多可包含 40 条、每条最多 300 字符，旧文档中的“通常 1–3K 字符”不能当成可靠上界。

## 13. 验证

```bash
# 单元与边界测试
.venv/bin/python -m pytest tests/ -q

# 临时 SessionDB 的来源/冲突集成验证
<your-hermes-checkout>/venv/bin/python scripts/integration_check.py

# 指定安全会话的真实模型、写回与 usage 验证
<your-hermes-checkout>/venv/bin/python scripts/e2e_check.py <session_id>

# 批量范围预览，不调用模型、不写标题
<your-hermes-checkout>/venv/bin/python scripts/retitle_all.py --dry-run
```

重点回归项：

- user / legacy 标题保护；
- derived 升级；
- 0/1 复审语义；
- candidate replace / keep / approve；
- renames_per_hour；
- context compaction / replay 清洗；
- first_title_mode；
- profile 隔离；
- llm→llm 写回竞态窗口。

## 14. 已知边界与后续建议

当前最值得继续处理的源码问题：

1. **`rename_confirmations > 1` 没有累计计数**：配置看起来支持 N 轮，但实现只有“开/关复审门”。
2. **`strategy` 是兼容死配置**：要么恢复明确的 aggressive 行为并测试，要么在后续版本正式移除。
3. **`first_title_mode` 的宿主副作用不可逆管理**：插件能关闭内建标题器，但不知道它原本是谁关的，也不会自动恢复。更好的设计是保存所有权/原值，或让宿主提供临时抑制接口，而不是无标记地改持久配置。
4. **status 不解析宿主实际路由**：默认 auxiliary 模式下只能看到 `(host default)`。
5. **llm→llm 没有宿主级 CAS**：只能等待 Hermes 提供更合适的公开写入 API。
6. **进程内状态重启即丢失**：pending、轮数和频次窗口都不会跨进程保留，这是当前刻意接受的本地行为。
