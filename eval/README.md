# AutoTitler 评测契约与执行规范 (Evaluation Contract)

## 1. 核心原则与契约边界
- **盲生成 ≠ 生产演进 (Blind Generation ≠ Production Evolution)**：`generate_title(blind=True)` 从空标题单次生成，没有真实时序、没有累积状态、跳过了冷却与确认门禁。评测必须通过真实 `AutoTitler.evaluate()` 状态机在沙箱中按时间序列回放。
- **最终来源 ≠ 初始来源 (Final Source ≠ Initial Provenance)**：会话行上的 `title_source: user` 仅代表当前/最终状态，不能倒推为初始就是人工命名；初始来源必须有独立证据证明。若初始状态证据不足，归类为 `initial_provenance_unknown`。
- **离线字符 ≠ 实际 token (Offline Characters ≠ Measured Tokens)**：字符估算或未经测量的 token 不能作为计费或模型感知依据。实际消耗必须由 `TraceLLM` 测量并记录实际 `input_tokens` / `output_tokens` / `reasoning_tokens`。若无 usage 则显式标记为 null / unavailable。

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
   - 预注册 3 组机制交叉组合：
     1. 摘要预算 × 助手消息剪裁
     2. opening/recent 视野 × 结构化摘要
     3. conservative/aggressive × 确认门禁
   - 使用统一的时钟、前缀时序、初始标题与沙箱环境。

## 4. 指标体系与决策门槛
- 主线覆盖率（盲审判定）
- 局部篡权率（副主题/报错劫持主线）
- 阶段遗漏率与合法转向延迟轮数
- 错误改名率 / 百轮（稳定度）
- 漏改率（应改未改）
- 真实调用消耗：调用数、P50/P95 延时、各阶段 Token 实测、异常分类统计
- 必须包含明确的 `eligible / observed / failed` 分母，不得只展示“看起来最好”的聚合数字。
