# 0.3 模型感知与消息捕获研究（RESEARCH_0.3_MODEL_PERCEPTION.md）

本文档由 Lattice 团队共同维护，记录 AutoTitler 0.3 推进过程中关于消息载体识别、模型认知表征、配置契约与非侵入架构的研究事实与实证数据。

---

## 1. 真实语料基准（数据源：~/.hermes/state.db，1694 个 session，369 个 lineage≥100）

### 1.1 用户角色（user）真实构成（369 个长会话汇总）
* **真实用户输入**：仅占 **10.0%**（737,799 / 7,342,249 字符；按条数占 30.8%）。
* **非用户意图载体**：高达 **90.0%** 的字符属于以下三类：
  1. 系统注入与通知（`[System note: ...]`、`[IMPORTANT: ...]`、`[Your active task list]` 等）
  2. 历史压缩摘要载体（`[CONTEXT COMPACTION]`、`[PRIOR CONTEXT]`）
  3. 子代理汇报与结果（`[ASYNC DELEGATION BATCH COMPLETE]`）

> **核心结论**：标题偏差的根本矛盾不是“用户与助手权重”，而是**“载体识别失效”**。在进入标题模型前，真实用户意图被 90% 的非用户载体严重稀释。

### 1.2 模型实际接收特征与真实丢失口径（2597 条真实用户轮次审计）
1. **真实用户轮次丢失 17.3%（449 / 2597 条丢失）细分**：
   * **真正包裹人类原话的仅 4 条**（集中于 `interrupted mid-run` 中人类恢复请求），其余 30 条 `interrupted` payload 为截断的工作现场；
   * `model has changed` 120 条中 115 条被丢弃，其 payload 实为 `xai/grok-4.7 via provider opencodex` 等运行时元数据，丢弃属于正向降噪，**坚决不解包回灌**；
   * `[IMPORTANT:` 技能注入占 221 条（丢失率 94.4%），正文虽无主题，但在时序上代表“能力/阶段切换”，0.3 考虑将其降级为轻量能力标记而非整条静默；
   * 结论：`interrupted mid-run` 解包必须带“人类语气/意图”校验，杜绝把截断工作现场当用户意图。
2. **压缩摘要 60% 的会话看不到**：
   * 225 个含压缩载体的会话中，只有 93 个交付了摘要，141 个因 `not saw_visible_opening` 门禁被全量丢弃（压缩后只要有一句“继续”，摘要即被判空）。
   * `summary_preview(_, 1200)` 按节裁剪造成信息失衡：Completed Actions、Relevant Files 几乎被全灭，34 个会话的 Goal 在 prompt 中根本不存在。
3. **90 条假摘要来自子代理汇报正文**：
   * `_extract_compaction_summary` 未做载体归属校验，从子代理报告及 assistant 对白中误提取了 65,615 字符的嵌套摘要，与真实摘要混流。
4. **多轮压缩仅取第一块**：
   * `summaries[0]` 始终锁定最早的压缩块，会话演进 2 小时后的新阶段结论完全被遮蔽。
5. **Prompt 内部存在 14.7% 重复文本**（`all_user` 与 `opening` 之间存在大面积重复投递）。
6. **群聊信封未剥离**：
   * 群聊转发信封未清洗，导致标题退化为 hash id 兜底。

---

## 2. 0.3 架构落地状态（已完成）

负责人：@voxel
* `plugin.yaml` 升级为 `manifest_version: 2`，完整声明 24 字段 `config_schema`。
* 处理保留根键冲突：将 `model` 声明安全映射为 `title_model`，Desktop 面板 24 字段完整渲染无遗漏。
* `config.py` 与 `__init__.py` 彻底解耦对宿主配置的侵入写操作，移除 `disable/enable_builtin_title_generation`，默认进入“零入侵、共存演化”模式。
* 新增 `verify_config_plumbing.py` 19 项只读门禁全通，`pytest tests/ -q` 282 项全通，`hermes plugins validate .` 全通，宿主配置零残留。

---

## 3. 消息引擎与载体解构推进项

负责人：@eclipse
1. **精准解包与防污染门禁**：
   * `interrupted mid-run` 仅在 payload 为真实人类意图时提取内核，拒绝灌入截断现场；
   * `model has changed` 与纯系统通知维持整条丢弃，严禁回灌运行时元数据；
   * `[IMPORTANT:` 技能注入标记作为时序切片参考，不展开正文。
2. **载体归属前置守卫**：
   * `_extract_compaction_summary` 首部严格校验本会话真载体（包含 `CONTEXT COMPACTION` 与 `PRIOR CONTEXT`），彻底杜绝子代理报告假摘要。
3. **结束边界判别式落盘**：
   * 采用“标记独占整行、第 0 列、匹配不跨行 + 安全回退”，彻底消除反引号引用截断与 6820 字符正文当意图的“双重计费”稀释。
4. **废除 `saw_visible_opening` 互斥门禁**：
   * 压缩摘要作为独立层级结构化历史锚点，与可见续段正交并存。
5. **最新摘要覆盖/多轮级联与跨区去重**。

---

## 4. 门禁与真实回归集验证

负责人：@aperture
* 锁定 348 个有效长会话固定回归集（`cache/scratch/lost_breakdown.txt`）。
* 端到端验证：真实意图零污染召回、摘要交付率、假摘要零误判、Prompt 重复率压降至 0。
