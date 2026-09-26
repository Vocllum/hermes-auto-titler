# AutoTitler 评测契约与执行规范 (Evaluation Contract)

## 1. 核心原则与契约边界
- **盲生成 ≠ 生产演进 (Blind Generation ≠ Production Evolution)**：`generate_title(blind=True)` 从空标题单次生成，没有真实时序、没有累积状态、跳过了冷却与确认门禁。评测必须通过真实 `AutoTitler.evaluate()` 状态机在沙箱中按时间序列回放。
- **最终来源 ≠ 初始来源 (Final Source ≠ Initial Provenance)**：会话行上的 `title_source: user` 仅代表当前/最终状态，不能倒推为初始就是人工命名；初始来源必须有独立证据证明。若初始状态证据不足，归类为 `initial_provenance_unknown`。
- **离线字符 ≠ 实际 token (Offline Characters ≠ Measured Tokens)**：字符估算或未经测量的 token 不能作为计费或模型感知依据。实际消耗必须由 `TraceLLM` 测量并记录实际 `input_tokens` / `output_tokens` / `reasoning_tokens`。若无 usage 则显式标记为 null / unavailable。
- **短白名单未命中 ≠ 局部篡权 (Whitelist Miss ≠ Local Usurpation)**：人工标注的短白名单（`acceptable_titles`）仅作精确等值诊断（`strict_whitelist_hit`），代表人工词表下界，不能等同于全量语义主线覆盖；未命中短白名单绝不能在 `allowed_shift=False` 时直接判定为局部篡权（`local_usurped`）。篡权判定必须具备独立的正向证据（如命中 `secondary_topics` 声明的局部排错/噪声主题）。对于未命中短白名单且无篡权明确证据的标题，标记为未定（`undetermined`），绝不可将未知算作成功，也绝不可将未知算作 100% 严重篡权失败。

## 2. 样本门槛与真值冻结 (Corpus Gating)
- 仅收录顶层会话（无父会话）、初始状态可自动命名、至少 8 条真实人类输入（过滤子代理完成包、系统中断通知、重复重播等机器噪声）。
- 覆盖 8 类典型场景：短对话、单一主线、双主线均衡、主线+局部排错、明确转向、工具/助手噪声、压缩与重复压缩、精确标识符。
- 人类独立盲审标注关键前缀（先看对话历史证据，不看模型候选与旧标题），产出 `durable_subject`、必要次主题、是否允许转向 (`allowed_shift`)、必须保留的标识符集合与置信度。
- 样本指纹保存在 `eval/fixtures/manifest.json`，严禁在公开发布/room 汇报中泄露真实会话正文、敏感路径与凭据。

## 3. 两阶段实验方法 (Two-Phase Experiment)
1. **阶段一：离线输入/架构诊断**
   - 比较真实人类轮次与压缩载体进入 Prompt 的损失率、阶段覆盖（早/中/晚）、载体污染率与 Token 预算分布。
2. **阶段二：在线成对回放实验**
   - 以生产默认作为共同基线锚点（A）。
   - 单因素正交对比：`summary_preview_chars` (1200 vs 2400)、`preview_chars` (120 vs 60)、`opening_turns` (2 vs 3)、`recent_turns` (2 vs 4)、`ignore_model_messages` (false vs true)、`user_message_threshold` (40 vs 20)、`strategy` (conservative vs aggressive)、`title_style` (concise vs complete)、`rename_confirmations` (1 vs 0)、`min_interval_minutes` (5 vs 0)、`every_n_turns` (2 vs 1)。
     *注意：`every_1_turn` 衡量的是代码默认值 `DEFAULTS["every_n_turns"]`（2 → 1）的单因素偏离，而非相对现网本地 config.yaml（现网本地已设置为 1）的偏离。报告必须明确此基线对照口径。*
   - 预注册 3 组机制交叉组合：
     1. 摘要预算 × 助手消息剪裁
     2. opening/recent 视野 × 结构化摘要
     3. conservative/aggressive × 确认门禁
   - 使用统一的时钟、前缀时序、初始标题与沙箱环境。

## 4. 指标体系与决策门槛
- **严格白名单命中率 (`strict_whitelist_hit_rate`) / 保守主线覆盖下界 (`mainline_coverage_rate`)**：
  基于真值标注的全词等值精确匹配。仅作为可验证的保守下界诊断指标，不代表全量语义同义覆盖。
- **确诊局部篡权率 (`local_usurpation_rate`)**：
  标题全词等值命中 `secondary_topics` 所标注的局部次要话题的轮次占比；包含副主题的复合标题仍须人工检查，不凭子串确诊。
- **未定区间率 (`undetermined_rate`)**：
  不在短白名单且未全词等值命中副主题的灰色区间，须人工盲审。保留标识符或允许话题转向都不足以证明主线质量。
- **严格标签未篡权率 (`safe_mainline_rate`，历史字段名)**：
  严格命中 `acceptable_titles` 的轮次占比；不代表语义主线覆盖。
- **标识符保真度 (`identifiers_preserved_rate`)**：
  在声明了 `required_identifiers` 的轮次中，必要实体与标识符在标题中完整保留的比例。
- **阶段遗漏率与合法转向延迟轮数 (`shift_latency_turns` / `failed_shifts`)**：
  合法转向必须写进受认可的有效标题才计入转向延迟，否则计为转向失败。
- **错误改名率 / 百轮（稳定度）与漏改率 (`missed_renames`)**
- **真实调用消耗**：调用数、P50/P95 延时、各阶段 Token 实测、异常分类统计。
- **分母严谨性**：必须包含明确的 `eligible / observed / failed` 分母，不得只展示“看起来最好”的聚合数字。

### 指标演进与破坏性变更修复说明
- **缺陷背景**：原评测逻辑在 `allowed_shift=False` 时，粗暴将所有未命中短白名单的标题均判定为局部篡权（`is_usurped_title=True`）。由于人工标注的短白名单仅含 1-2 条，导致模型产出的合理同义表达（如保留了 `required_identifiers` 但字面微调）被 100% 误判为严重篡权，造成篡权率严重虚高失真。
- **纠正方案**：
  1. 解耦白名单精确匹配与篡权判定：`is_acceptable_title` 保留为严格白名单诊断下界（`strict_whitelist_hit`），不再武断代表全量语义主线覆盖；
  2. 独立证据确诊篡权：仅当标题全词等值命中 `secondary_topics` 时机械标记；其余疑似复合或语义偏离需人工盲审；
  3. 引入三态分类 (`classify_usurpation`) 与未定区间 (`undetermined`)：对无法识别的脱离词表标题标记为未定，不将未知算作成功，也绝不将未知算作 100% 严重失败；
  4. 兼容性保证：`mainline_covered_turns` / `mainline_coverage_rate` 与 `local_usurped_turns` / `local_usurpation_rate` 保持字段名兼容，报告层向下兼容无中断。
