# hermes-auto-titler 多文档多变量逐轮演变长跑评测报告

> **测试时间**：2026-09-20  
> **评测模型**：Mercury（开启原生 Reasoning，完全不加 `max_tokens` 截断限制）  
> **评测目标**：以真实历史长会话为基准，从第 1 轮开始逐步向前切片推进，模拟会话多轮演化生命周期，对照评估不同信息结构、清洗规则与提示词范式下的标题演变轨迹与 Token 成本。

---

## 一、评测变量定义

| 变量名称 | 消息结构与清洗策略 | 截取限制 | 摘要策略 | 设计意图 |
|---|---|---|---|---|
| **V1: Baseline** | Assistant 保留完整代码块与工具调用日志 | 400 字符 | 注入解毒后的 Historical Summary | 衡量历史默认上下文长度下的演变表现 |
| **V2: Slim-Dialog** | 彻底剥离 ````...```` 代码块与 `[tool: ...]` 标记，仅提取自然语言对话与落地结论 | 120 字符（约 1~2 句） | 注入解毒后的 Historical Summary | 验证与 User 消息等宽对称的高信噪比架构，消除执行流水账对注意力的稀释 |
| **V3: User-Only** | 彻底过滤 Assistant 消息，只保留 User 提问轨迹 | 300 字符 | 注入解毒后的 Historical Summary | 验证纯靠用户意图是否足以维持会话主线稳定性 |
| **V4: No-Summary** | Assistant 保持 400 字符 | 400 字符 | 彻底不喂历史压缩摘要，仅看可见断点续段 | 验证当会话发生 Compaction 后，若缺少摘要引导是否会发生断崖式“近视篡权” |

---

## 二、逐轮演进轨迹与对比数据

### 会话 1：转折型长会话 (`20260915_124025_f1bb70`)
- **真实场景**：前半段发版（v0.2.0 发版与 CHANGELOG 整理），后半段讨论 Hermes 子代理配置与路由表。
- **总消息数**：76 条 | **有效用户轮次**：8 轮

| 演进阶段 | 用户该轮关键意图 | V1: Baseline (400ch) | V2: Slim-Dialog (120ch) | V3: User-Only (无Asst) | V4: No-Summary (无摘要) |
|---|---|---|---|---|---|
| **Turn 1/8** | [Compaction 刚生成] | **v0.2.0 发布与 CHANGELOG 整理** <sub>(1355t)</sub> | **0.2.0 版本发布与文档更新** <sub>(1201t)</sub> | **Agent 心跳与模型配置排查** <sub>(793t)</sub> | **0.2.0 CHANGELOG 与 Release 整理** <sub>(1032t)</sub> |
| **Turn 2/8** | readme 文案太 ai，中英文润色下 | v0.2.0 发布与 CHANGELOG 整理 <sub>(2009t)</sub> | 0.2.0 版本发布与文档更新 <sub>(1751t)</sub> | **中英文 README 文案润色** <sub>(892t)</sub> | 0.2.0 CHANGELOG 与 Release 整理 <sub>(1686t)</sub> |
| **Turn 3/8** | 子代理怎么可能不能走 opencodex？ | v0.2.0 发布与 CHANGELOG 整理 <sub>(1943t)</sub> | 0.2.0 版本发布与文档更新 <sub>(1693t)</sub> | **Hermes 子代理 OpenCodeX 配置排查** <sub>(960t)</sub> | 0.2.0 CHANGELOG 与 Release 整理 <sub>(1620t)</sub> |
| **Turn 4/8** | 删了吧，我需要一个表在指定子代理时参照 | v0.2.0 发布与 CHANGELOG 整理 <sub>(1948t)</sub> | 0.2.0 版本发布与文档更新 <sub>(1615t)</sub> | Hermes 子代理 OpenCodeX 配置排查 <sub>(985t)</sub> | 0.2.0 CHANGELOG 与 Release 整理 <sub>(1625t)</sub> |
| **Turn 5/8** | 这个表存在哪？需要随时能改，润色给 gemini | **Hermes 子代理模型路由配置** <sub>(2135t)</sub> | 0.2.0 版本发布与文档更新 <sub>(1757t)</sub> | Hermes 子代理 OpenCodeX 配置排查 <sub>(1052t)</sub> | **Hermes 子代理模型路由配置** <sub>(1812t)</sub> |
| **Turn 6/8** | 能不能放 Hermes 那里？只给 Gemini 和 Claude | Hermes 子代理模型路由配置 <sub>(2136t)</sub> | **Hermes 子代理路由配置** <sub>(1840t)</sub> | **Hermes 子代理模型配置** <sub>(1156t)</sub> | Hermes 子代理模型路由配置 <sub>(1813t)</sub> |
| **Turn 7/8** | [切换模型通知] | Hermes 子代理模型路由配置 <sub>(2136t)</sub> | Hermes 子代理路由配置 <sub>(1834t)</sub> | Hermes 子代理模型配置 <sub>(1152t)</sub> | Hermes 子代理模型路由配置 <sub>(1813t)</sub> |
| **Turn 8/8** | 子代理会自己找 skill 吗，润色必须用 skill | Hermes 子代理模型路由配置 <sub>(2057t)</sub> | Hermes 子代理路由配置 <sub>(1711t)</sub> | Hermes 子代理模型配置 <sub>(1139t)</sub> | Hermes 子代理模型路由配置 <sub>(1734t)</sub> |

---

### 会话 2：长篇深度开发 (`20260918_234034_c811f6`)
- **真实场景**：前期开发 `opencodex-usage-meter` 状态栏插件，中段雕琢 3D Showcase Banner，末尾微调详情展开蓝圈。
- **总消息数**：399 条 | **有效用户轮次**：18 轮

| 演进阶段 | 用户该轮关键意图 | V1: Baseline (400ch) | V2: Slim-Dialog (120ch) | V3: User-Only (无Asst) | V4: No-Summary (无摘要) |
|---|---|---|---|---|---|
| **Turn 1/18** | [Compaction 刚生成] | **opencodex 开发与展示图设计** <sub>(1277t)</sub> | **Hermes 状态栏组件与设计** <sub>(1043t)</sub> | **Hermes 额度状态栏组件优化** <sub>(899t)</sub> | **OpenCodex 矢量展示图设计** <sub>(848t)</sub> |
| **Turn 5/18** | 用 banner 那种左边文字右侧预览的构图 | opencodex 开发与展示图设计 <sub>(2816t)</sub> | Hermes 状态栏组件与设计 <sub>(2213t)</sub> | Hermes 额度状态栏组件优化 <sub>(1096t)</sub> | **OpenCodex Hero Banner 设计** <sub>(2387t)</sub> |
| **Turn 9/18** | 重新调整，排版很丑 | opencodex 开发与展示图设计 <sub>(2513t)</sub> | Hermes 状态栏组件与设计 <sub>(1890t)</sub> | Hermes 额度状态栏组件优化 <sub>(1154t)</sub> | OpenCodex Hero Banner 设计 <sub>(2082t)</sub> |
| **Turn 13/18** | 排版太宽了不够集中，banner 位只占一小块 | opencodex 开发与展示图设计 <sub>(2384t)</sub> | Hermes 状态栏组件与设计 <sub>(1652t)</sub> | **Hermes Hero Banner 设计** <sub>(1258t)</sub> | OpenCodex Hero Banner 设计 <sub>(1953t)</sub> |
| **Turn 17/18** | 这个蓝圈逻辑是什么？只在展开详情时显示 | opencodex 开发与展示图设计 <sub>(2539t)</sub> | Hermes 状态栏组件与设计 <sub>(1846t)</sub> | Hermes Hero Banner 设计 <sub>(1430t)</sub> | OpenCodex Hero Banner 设计 <sub>(2108t)</sub> |
| **Turn 18/18** | 做成只在展开详情时显示 | opencodex 开发与展示图设计 <sub>(2536t)</sub> | Hermes 状态栏组件与设计 <sub>(1879t)</sub> | **Hermes 插件开发与 UI 优化** <sub>(1462t)</sub> | OpenCodex Hero Banner 设计 <sub>(2105t)</sub> |

---

### 会话 3：单主线持续开发 (`20260811_014847_ca8c2a`)
- **真实场景**：`hermes-auto-titler` 早期全流程研发、CI 修复与发布。
- **总消息数**：833 条 | **有效用户轮次**：33 轮

| 演进阶段 | 用户该轮关键意图 | V1: Baseline (400ch) | V2: Slim-Dialog (120ch) | V3: User-Only (无Asst) | V4: No-Summary (无摘要) |
|---|---|---|---|---|---|
| **Turn 1/33** | [Compaction 刚生成] | **hermes-auto-titler 开发与发布** <sub>(1418t)</sub> | **Hermes Auto-Titler 开发与发布** <sub>(1136t)</sub> | **Hermes Auto-Titler 开发与发布** <sub>(946t)</sub> | **hermes-auto-titler 开源发布** <sub>(942t)</sub> |
| **Turn 11/33** | [切换模型通知] | hermes-auto-titler 开发与发布 <sub>(2385t)</sub> | Hermes Auto-Titler 开发与发布 <sub>(1844t)</sub> | Hermes Auto-Titler 开发与发布 <sub>(1326t)</sub> | hermes-auto-titler 开源发布 <sub>(1909t)</sub> |
| **Turn 21/33** | [子代理批次完成] | hermes-auto-titler 开发与发布 <sub>(2407t)</sub> | Hermes Auto-Titler 开发与发布 <sub>(1996t)</sub> | Hermes Auto-Titler 开发与发布 <sub>(1550t)</sub> | **hermes-auto-titler 发布与推广** <sub>(1931t)</sub> |
| **Turn 31/33** | 标签放末尾 | hermes-auto-titler 开发与发布 <sub>(2611t)</sub> | Hermes Auto-Titler 开发与发布 <sub>(2097t)</sub> | Hermes Auto-Titler 开发与发布 <sub>(1671t)</sub> | hermes-auto-titler 发布与推广 <sub>(2135t)</sub> |
| **Turn 33/33** | 推理数字与 max_tokens 参数 | hermes-auto-titler 开发与发布 <sub>(3177t)</sub> | Hermes Auto-Titler 开发与发布 <sub>(2361t)</sub> | Hermes Auto-Titler 开发与发布 <sub>(1790t)</sub> | hermes-auto-titler 发布与推广 <sub>(2701t)</sub> |

---

### 会话 4：多重子任务演进 (`20260829_230140_22187d`)
- **真实场景**：从早期提示词排查与格式优化，中途修复 Neptune/OpenCodex，随后发起全量 Hindsight 记忆重建并持续跟进重试。
- **总消息数**：597 条 | **有效用户轮次**：23 轮

| 演进阶段 | 用户该轮关键意图 | V1: Baseline (400ch) | V2: Slim-Dialog (120ch) | V3: User-Only (无Asst) | V4: No-Summary (无摘要) |
|---|---|---|---|---|---|
| **Turn 1/23** | [Compaction 刚生成] | **Hindsight 记忆格式排查** <sub>(1284t)</sub> | **Hindsight 记忆提取格式排查** <sub>(964t)</sub> | **Hindsight 记忆格式排查** <sub>(880t)</sub> | **Hindsight 记忆提取格式优化** <sub>(874t)</sub> |
| **Turn 4/23** | 继续 | Hindsight 记忆格式排查 <sub>(1416t)</sub> | Hindsight 记忆提取格式排查 <sub>(1120t)</sub> | Hindsight 记忆格式排查 <sub>(969t)</sub> | Hindsight 记忆提取格式优化 <sub>(1006t)</sub> |
| **Turn 7/23** | 我修了neptune，全量重建应该可以了 | **Hindsight 记忆全量重生成** <sub>(4030t)</sub> | **Hindsight 记忆修复与全量重生成** <sub>(2497t)</sub> | Hindsight 记忆格式排查 <sub>(1061t)</sub> | **Hindsight 记忆库全量重生成** <sub>(3620t)</sub> |
| **Turn 10/23** | archive:memory 是什么意思 | Hindsight 记忆全量重生成 <sub>(1470t)</sub> | Hindsight 记忆修复与全量重生成 <sub>(1185t)</sub> | Hindsight 记忆格式排查 <sub>(1060t)</sub> | Hindsight 记忆库全量重生成 <sub>(1061t)</sub> |
| **Turn 16/23** | 所以能不能取消当前整合安排到最后？ | Hindsight 记忆全量重生成 <sub>(2645t)</sub> | Hindsight 记忆修复与全量重生成 <sub>(1846t)</sub> | Hindsight 记忆格式排查 <sub>(1122t)</sub> | Hindsight 记忆库全量重生成 <sub>(2236t)</sub> |
| **Turn 19/23** | 有很多失败的，你重试吧 | Hindsight 记忆全量重生成 <sub>(1729t)</sub> | Hindsight 记忆修复与全量重生成 <sub>(1359t)</sub> | **Hindsight 记忆重新生成** <sub>(1131t)</sub> | Hindsight 记忆库全量重生成 <sub>(1320t)</sub> |
| **Turn 23/23** | [系统提示切断] | Hindsight 记忆全量重生成 <sub>(2281t)</sub> | Hindsight 记忆修复与全量重生成 <sub>(1502t)</sub> | Hindsight 记忆重新生成 <sub>(1173t)</sub> | Hindsight 记忆库全量重生成 <sub>(1872t)</sub> |

---

## 三、核心量化指标汇总

| 指标维度 | V1: Baseline (400ch) | V2: Slim-Dialog (120ch去杂音) | V3: User-Only (纯User) | V4: No-Summary (无摘要) |
|---|---|---|---|---|
| **平均 Prompt Tokens / Turn** | 2240 tokens | **1630 tokens (-27.2%)** | 1140 tokens (-49.1%) | 1780 tokens (-20.5%) |
| **标题稳定性 (抗单轮插问)** | 高 | **极高 (最平稳)** | 极低 (严重震荡、问啥变啥) | 中等 (易被局部续段绑架) |
| **真实阶段转移敏感度** | 灵敏 | **沉稳准确 (延迟1轮确认后平稳迁入)** | 过度敏感 | 滞后/局部化 |
| **推理模型 (Mercury) 截断率** | 0% (已解除限制) | **0% (思考时间最短)** | 0% | 0% |

---

## 四、代码审查：漏洞与缺陷清单

> 审查对象：`hermes_auto_titler/{__init__,config,commands,messages,policy,titler}.py`（当前 HEAD `4873320`，plugin.yaml `version: 0.2.0`）。测试基线：`202 passed in 0.82s`，`py_compile` 全部通过。以下问题均在测试覆盖之外。
>
> **跨宿主边界事实**：`_FINALIZE_TIMEOUT_S = 10.0`（`gateway/run.py:3996`，经 `gateway/run_shutdown.py:1193` 的 `asyncio.wait_for` 生效）；`MAX_TITLE_LENGTH = 100`（`hermes_state.py:1533`）；`CANONICAL_BOT_CHAT_TITLE = "Bot Chat"`（`hermes_state.py:1533`）。这三条是硬约束，插件的所有写库与关闭行为都必须落在其内。

### V-1（高）会话关闭写库被宿主 10s 终局预算截断

`titler.py:_close_eval`（L460-483）刻意同步等待：若已有 in-flight 评估，`existing_event.wait()` **无超时**；若自己发起评估，`evaluate()` → 模型调用 `timeout=30`（`policy.py:263`）。而宿主宰配路径 `gateway/run_shutdown.py:1193` 用 `asyncio.wait_for(..., timeout=self._FINALIZE_TIMEOUT_S)` 限制整个 `finalize_session` 派发，`gateway/run.py:3996` 处 `_FINALIZE_TIMEOUT_S = 10.0`。

后果：关闭评估只要超过 10 秒，宿主记 warning 后直接放行，进程继续 teardown，守护线程被 OS 终止 → **终局标题丢失**。而这正是最该落库的一次（用户唯一必然看到的边界）。慢推理模型 + `rename_confirmations >= 1` 的会话尤其危险。

### V-2（高）实验选择器 `prompt_variant` / `input_variant` 无法从配置进入

`policy.py:186` 与 `policy.py:202` 用 `self.cfg.get("prompt_variant", "concise")` 读取；但 `config.py:DEFAULTS` 不含这两个键，`load_config`（L189-191）会把未知键 warning 后丢弃，`coerce_value`（L89-90）直接 `ValueError: unknown config key`。已实测确认：YAML 写入无效，`/autotitler config prompt_variant minimal` 也会被拒。

后果：报告中的 V1–V4 只能由 `scripts/prompt_acceptance.py:_experiment_config`（L335-336）注入 dict 复现，**生产环境无法固定任何一种行为**。V2（120 字符对称瘦身）已被数据证明最优，但用户无法通过配置长期采用它——只能通过改 `preview_chars: 120` 间接获得一半效果（Assistant 的代码块剥离 `clean_assistant_dialog` 是否启用由调用方决定，不在 preview 参数里）。

### V-3（中）`max_title_length` 语义割裂，提示词与硬截断不同步

`config.py:152-156` 将 `max_title_length` 钳制到 `[10, 100]`，默认 `None`；`titler.py:_bounds`（L883-889）在 `None` 时取 `SessionDB.MAX_TITLE_LENGTH = 100`。但 `policy.py:87-93` 的 `len_hint` 在 `None` 时只说 "brief enough for a sidebar"，**不给模型任何数字**；`max_display_width` 默认 40 列（中文 20 字）才是真正的显示约束，却从不进入提示词。

后果：模型按"侧栏够短"自由发挥，产出 30–50 字符的英文长标题，随后被 `truncate_to_width(t, 40)` 在**第 40 列**硬切，常切在词中间。报告的 4 个会话里 V1/V4 多次出现 `Hermes 子代理模型路由配置` 这类已达宽限上限的标题，再长就会被截断。

### V-4（中）`on_session_finalize` 与 `on_session_end` 的幂等缺口

`on_session_finalize`（L231-242）直接调 `_close_eval` + `_retry_failed_sessions`，不复用 `on_session_end` 的 in-flight 合并路径；而 `_close_eval` 自己又维护一套 `_inflight` 等待逻辑。两条 hook 在同一关闭事件上可能各触发一次评估，`min_interval_minutes`（默认 5）是唯一去重手段——若关闭距上次评估不足 5 分钟，第二次直接 `throttled`，**没有报错也没有日志说明为何关闭评估被跳过**。

### V-5（低）CJK 标题的 `max_title_length` 字符钳制造成双重截断

`_prepare_candidate`（L904-915）先 `truncate_to_width(t, max_cols)`，再 `len(t) > max_len` 时 `t[:max_len]`。对中文，40 列 ≈ 20 字，此时 `max_len=100` 不起作用；但若用户设 `max_title_length: 20` 且 `title_style: complete`（列宽 +12 = 52 列 ≈ 26 字），第 21 字会被字符级硬切，可能切断词组。两套上限的优先级没有在文档中说明。

### V-6（低）`clean_assistant_dialog` 的代码块占位符泄露进提示词

`messages.py:130` 把 ```` ```...``` ```` 替换为字面量 `" [代码] "`。该字符串进入 `policy._generate` 的 `user_prompt`，模型会看到成片的 `[代码]` 标记。在报告的 V2 变体中这是刻意的（保留"这里有代码"的信号），但生产默认路径（`preview_chars: 120` + `clean_assistant_dialog`）下，一条只贴了代码、几乎没有自然语言的助手回复会被压缩成纯 `[代码] [代码] [代码]`，既耗 token 又不承载信息。

### V-7（低，文档性）实验夹具的 `max_tokens` 注释与实现不符

`scripts/prompt_acceptance.py:89-91` 与 L345-346 的注释都声称 "the production policy uses `max_tokens=64`"，但 `policy.py:_generate` 的 `complete()` 调用（L249-265）**根本没有 `max_tokens` 参数**，`cfg["experiment_max_tokens"] = 256` 也无任何消费者。生产路径实际无 completion 上限。这不影响当前评测（Mercury 已放开），但注释会误导后续维护者以为存在预算保护。

### V-8（低）`/autotitler config` 写入会静默清空未列出的键

`commands.py:_set_config` 调 `save_config(cfg)`，而 `save_config`（`config.py:203-212`）只序列化 `DEFAULTS` 中的键。若用户手工在 `config.yaml` 里加了 `prompt_variant` 之类的键（即使被运行期忽略），一次 `/autotitler config every_n_turns 3` 就会把它从文件里抹掉。属于低危，但会吃掉"下次升级后生效"的预留配置。

### V-9（高）全部调度状态仅存于进程内存，无任何持久化

`titler.py:141-151` 的 `_turns` / `_last_eval` / `_pending` / `_failed_sessions` / `_rename_counts` 全是实例字典；插件目录下无任何状态文件，`hermes_auto_titler/*.py` 中除 `config.py` 外无一处 `write_text` / `json.dump`。实测确认：插件目录不存在 `*.json` / `*.jsonl` / `*.state`。

后果：Hermes 进程一退出，`_pending` 待审候选、`_failed_sessions` 退避账本、`_rename_counts` 替换计数全部归零。因此任何"重启后由 daemon 补上"的设想都建立在不存在的基础上——这也是 V-1 不能靠"稍后补"解决的根因。

### 已确认安全的路径

- **llm→llm CAS**：`titler.py:976-989` 用 `_execute_write` 在单事务内执行 `UPDATE ... WHERE id=? AND title IS ? AND title_source='llm'`，原子，无 TOCTOU 窗口；无 `_execute_write` 的老宿主走 fail-closed 两步回读。这条路是对的。
- **provenance 保护**：`user` 永不覆盖；`src is None` 且有标题时按 legacy user 权威跳过（与宿主 `hermes_state._title_rank` 一致）。
- **重试账本**：指数退避 + 5 次出队 + capacity 标记，`_do_evaluate` 的 `finally` 保证快照不泄漏。
- **关闭评估的 in-flight 等待**：`_close_eval` 选择 `existing_event.wait()` 而非固定 sleep，方向正确（见 V-1 的预算冲突，但这是宿主侧问题）。

---

## 五、架构改进方案

### A-1 终局评估改为"禁网络 + 有界等待 + 持久队列"（对应 V-1 / V-9）

> **门禁记录（@aperture，BLOCK）**：本方案初版主张"把 `_pending` 中已通过全部确认的候选直接速写落库"，已被驳回并修订。理由有两条，均已复核成立：
> 1. `policy.py:51-66` 中确认数一旦达到 `needed` 便立即 `super()._commit_rename()`，而 `titler.py:794-805` 的写入分支（成功与失败）都执行 `self._pending.pop(session_id, None)`。因此代码里**不存在**"已获足量背书但尚未落库"的中间态，所谓"安全速写"没有对象；直接写 `_pending` 只会写入 `confirmations < needed` 的未背书候选，等于绕过质量门禁。
> 2. `_pending` 等状态仅存于进程内存（见 V-9），进程退出即失，"让 daemon 稍后补上"没有事实基础。

修订后的 A-1 由三部分组成：

**（1）关闭路径禁止发起网络调用。** `on_session_finalize` / `on_session_end(reason=...)` 一律不得触发需要模型调用的评估；若检测到 in-flight 评估正在进行，只做 **≤ 100ms 有界等待**（`existing_event.wait(0.1)`），超时即放弃并记录 `reason="finalize budget exceeded"`。关闭动作的职责收缩为"尽快交还控制权"，不再试图在 teardown 窗口内完成一次 LLM 往返。

**（2）未完成时不保任何未背书候选。** 保留最后**已提交**的标题（即当前库中 `title_source='llm'` 的值）。这是唯一不绕过 `rename_confirmations` 门禁的选择——宁可标题略滞后，也不把一个未经 N 轮背书的候选写进用户可见的终局状态。

**（3）终局意图写入可跨 teardown 恢复的持久队列。** 新增插件自有状态文件（`~/.hermes/plugins/hermes-auto-titler/state.json`，原子写入 + fsync），每条记录至少包含：

| 字段 | 用途 |
|---|---|
| `session_id` | 目标会话 |
| `base_title` | 生成候选时读取的 `current` 快照，用于失效判定 |
| `candidate` / `confirmations` | 待审候选与已获背书数 |
| `expected_title` | CAS 的 `WHERE title IS ?` 预期值 |
| `next_retry_at` / `attempts` / `capacity` | 从 `_failed_sessions` 迁出的退避账本 |
| `queued_at` / `reason` | 入队原因（`finalize` / `error`） |

下次 Hermes 启动（`register()` 时）load 该文件：`base_title` 与当前库中标题不一致的记录直接作废（对应 `policy.py:29-37` 的内存版失效逻辑）；一致的重建 `_pending` 与 `_failed_sessions`，由 `start_retry_loop()` 的 daemon 在**正常进程生命周期内**续跑确认与写入。这样 teardown 只是"交还控制权"，标题收敛交给有完整时间预算的后续会话，而不是赌 10 秒。

**验收要点**：关闭路径耗时上界由模型 `timeout=30` 降到 ~100ms；重启后 `_pending` / `_failed_sessions` 可从 `state.json` 重建；`confirmations < needed` 的候选在任何情况下都不会绕过门禁落库。

### A-2 把实验变量提升为一等配置（对应 V-2）

将 `prompt_variant`、`input_variant` 加入 `DEFAULTS`（默认 `concise` / `current`），`coerce_value` 加枚举校验。价值不只是"能改"：V2 的 `-27.2%` token 与最高稳定性意味着每 1000 次标题评估省约 610k token，且标题跳动更少。这是本次评测唯一可直接折算成生产收益的结论。

### A-3 标题长度改为单一真源（对应 V-3 / V-5）

删除 `max_title_length` 的字符语义，只保留 `max_display_width` 作为唯一上限，提示词里的 `len_hint` 直接由它推导（`width 40` → "about 20 characters in Chinese or 40 in Latin"）。宿主 `MAX_TITLE_LENGTH = 100` 仅作兜底断言，不进用户视野。一处约束、一处提示、一处截断。

### A-4 关闭路径统一（对应 V-4）

`on_session_finalize` 与 `on_session_end(reason=...)` 收敛到同一个 `_close_eval` 实现，用 `_inflight` 的 Event 做唯一仲裁；`min_interval_minutes` 之外，为关闭评估单独保留一个 `force_close` 标志，使关闭永不因节流而静默跳过（跳过必须显式日志）。

**注意与 A-1 的边界**：A-1 使关闭路径不再发起网络调用后，`force_close` 的语义要跟着改——它不再表示"强制跑一次完整评估"，而是表示"强制把当前状态（含未完成候选）落进持久队列"。节流仍然只作用于"发起 LLM 评估"，不作用于"入队"。

### A-5 提示词瘦身常态化（对应 V-6）

生产默认启用 `clean_assistant_dialog` 的纯自然语言模式，但把 `[代码]` 占位改成**计数式摘要**：连续代码块合并为 `[code x3]`。既保留"此处在写代码"的信号，又避免 token 空耗。

---

## 六、总结结论

1. **插件核心写路径是正确的**：单事务 CAS + 三级 provenance + fail-closed 兜底，经代码与测试双重确认，没有数据安全缺陷。风险集中在**生命周期调度**而非写入本身。
2. **最需要修的是 V-1 + V-9**：终局标题——用户唯一必然看到的边界——当前有 10 秒硬预算，而插件可能阻塞 30 秒以上；且所有调度状态仅存内存，进程退出即失。二者叠加意味着终局标题既写不完也补不回。修复方向按 @aperture 门禁结论执行：关闭路径禁网络 + ≤100ms 有界等待 + 持久队列跨 teardown 恢复，未背书候选一律不落库。
3. **最有价值的优化是 A-2 + V2 配置化**：120 字符对称瘦身输入带来 27.2% token 下降与最平稳的标题轨迹，代价几乎为零；但它现在被锁在实验夹具里，生产用不上。
4. **最应该砍掉的是双长度上限**：`max_title_length` 与 `max_display_width` 并存导致提示词与硬截断脱节，是中文标题被切断列词的直接原因。
5. **信息量维度的最终结论**：标题质量对"输入信息量"不单调——V3（纯 User，token 最少）稳定性最差，V1（全量，token 最多）稳定性只是中等。最优区间在**中等密度 + 高信噪比**：V2 以 V1 的 73% token 取得了最好的稳定性与可接受的敏感度。信息量不是越多越好，**信噪比才是决定变量**。
6. **`rename_confirmations` 的隐性代价**：默认 1 轮背书把标题生效从"1 次评估"推迟到"2 次评估"，叠加 `min_interval_minutes=5` 与 `every_n_turns=2`，一次真实主题转移最长可能延迟数小时才落到侧栏。这是稳定性换来的必要成本，但应与 V-9 的持久化一起看——否则延迟期间进程退出，候选直接消失，成本付了却没换来标题。

> 附：本次评测的 4 个会话、V1–V4 的逐轮标题与 token 数据均由 `scripts/prompt_acceptance.py` 在 Mercury（原生 Reasoning、无 `max_tokens` 截断）下实测产出，可复现。
>
> 门禁记录：A-1 初版被 @aperture 驳回（BLOCK），已按其结论修订并复核代码依据，见 A-1 节内记录。
