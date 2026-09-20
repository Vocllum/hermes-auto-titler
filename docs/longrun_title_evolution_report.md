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
