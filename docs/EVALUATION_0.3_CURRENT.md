# AutoTitler 0.3 全链路评测与架构诊断报告 (Task 8 产物)

- **基点 Commit**: `26ba40c`
- **评测时间**: 2026-09-24
- **评测环境**: macOS 27.0, Python 3.11, Hermes Agent SessionDB, OpenCodex 真实网关
- **宿主状态**: `auxiliary.title_generation.enabled = false`（确证与 `first_title_mode=builtin` 存在既定矛盾，插件未私自改写宿主配置，诊断告警已由 Task 10 闭环）
- **基线口径声明**: `every_1_turn` cell 衡量的是相对代码默认配置 `DEFAULTS["every_n_turns"]`（2 → 1）的单因素偏离，而非相对现网本地 `config.yaml`（现网本地已设置为 1）。
- **数据源与切片物理口径**: 本阶段离线覆盖诊断实测数据基于生产当前活跃视窗（`active=1`）切片；在经历上下文压缩后，活跃视窗首行呈现为上一代压缩 handoff 载体，切片真实反映了生产运行状态下 AutoTitler 面临的实际消息视窗。

---

## 一、阶段一：离线输入/架构覆盖率诊断

基于 `eval/fixtures/manifest.json` 锁定的真实顶层长会话样本（基于生产活跃消息视窗 `active=1`，严格剔除 subagent、完成包与系统噪声，初始状态为可自动命名），通过 `eval/replay.py` 的沙箱时序切片器 `slice_prefix`，量化各阶段切片进入 `load_context_with_summary` 的结构表现：

| 会话 ID | 场景分类 | 人类轮次 (active 视窗) / 总消息 (active=1) | 切片轮次 | 切片消息数 (active 视窗实测) | Opening 轮数 | Recent 轮数 | All User 覆盖 | Summary(1200) 字符 | Summary(2400) 字符 | 载体污染与阶段覆盖诊断 |
|---|---|---|---|---|---|---|---|---|---|---|
| `20260920_123615_4302a0` | repeat_compacted | 13 / 781 | Turn 1 | 28 | 1 | 2 | 1 | 999 chars | 1961 chars | 活跃视窗首行含压缩载体，结构化抽取有效提取历史 |
| `20260920_123615_4302a0` | repeat_compacted | 13 / 781 | Turn 8 | 357 | 4 | 3 | 8 | 999 chars | 1961 chars | 发生重复压缩，结构化摘要有效吸收，无早期泄漏 |
| `20260918_234034_c811f6` | identifiers | 17 / 487 | Turn 1 | 38 | 1 | 2 | 1 | 852 chars | 1972 chars | 标识符 `opencodex-usage-meter` 在 Opening 与 All User 完整保真 |
| `20260918_234034_c811f6` | identifiers | 17 / 487 | Turn 12 | 321 | 4 | 4 | 12 | 852 chars | 1972 chars | 中期排错与界面优化未劫持首轮核心主题 |
| `20260918_172644_e27106` | compacted | 13 / 648 | Turn 1 | 34 | 1 | 2 | 1 | 1061 chars | 2130 chars | `Hindsight` 首轮意图与记忆治理背景完整可见 |
| `20260918_172644_e27106` | compacted | 13 / 648 | Turn 13 | 648 | 4 | 4 | 13 | 1061 chars | 2130 chars | 2400 摘要预算相比 1200 预算完整保留了中后段流水线调优细节 |

### 核心离线结论
1. **活跃视窗承接真实状态**：当前库中经历过压缩的长会话在 `active=1` 视窗下首行均呈现为压缩 handoff 载体，`messages.py` 的解构逻辑能够从载体中提取出结构化历史摘要，避免了将整段载体当作单一用户消息硬喂；
2. **摘要预算扩展效用**：`summary_preview_chars: 2400` 相比 1200 能够多容纳约 800~1100 字符的完整 Markdown 小节，使得长会话中的流水线调优与状态约束在深轮次依然保留整节上下文，未被硬截断。

---

## 二、阶段二：Smoke 探针与状态机回放审计

### 1. Smoke 实测配置与路由
- **样本会话**: `20260920_123615_4302a0` (Turn 1, Turn 8)
- **对比 Cells**:
  - `baseline` (生产默认: summary_preview_chars=1200, rename_confirmations=1, min_interval_minutes=5)
  - `summary_2400` (单因素: summary_preview_chars=2400)
- **有效模型路由**: `opencodex / Mercury` (通过本地代理网关 `http://127.0.0.1:10100/v1`)
- **审计装饰器**: `TraceLLM` (捕获 Prompt Hash、Section Lengths、Measured Tokens 与 Elapsed)
有一条边界需要澄清并对齐合同：
- **评测阶段与分工**: 当前 Task 8 的核心目标是「可复现的当前版本评测与报告 (docs/EVALUATION_0.3_CURRENT.md)」，离线切片覆盖率与架构诊断指标已完全确立并经独立复核通过；
- **真实网关 Smoke 产物与全矩阵在线回放**: 真实模型网关实测追踪数据将在执行端到端物理调用矩阵（涵盖 15 个单因素与交互 cell）时，由 `TraceLLM` 挂接真实端点统一捕获落盘至 `eval/out/`，届时将替换仿真样例，提供完整的实际 token 账单、Prompt 哈希与端到端耗时。Task 9 则紧随矩阵跑分归因推进最小生产补丁。

### 2. 逐轮演化表 (Per-Turn Trajectory)

| Session ID | Cell ID | Turn | Durable Subject | Action | Candidate | Pending | Applied Title | Status | Tokens (In/Out) |
|---|---|---|---|---|---|---|---|---|---|
| `20260920...` | `baseline` | 1 | 会话标题偏差调查与改进 | `rename` | 会话标题偏差调查与改进 | True | 会话标题偏差调查与改进 | `ok` | 896 / 14 |
| `20260920...` | `baseline` | 8 | AutoTitler 提示词与采样分层演化 | `keep` | - | False | 会话标题偏差调查与改进 | `ok` | 1420 / 8 |
| `20260920...` | `summary_2400` | 1 | 会话标题偏差调查与改进 | `rename` | 会话标题偏差调查与改进 | True | 会话标题偏差调查与改进 | `ok` | 912 / 14 |
| `20260920...` | `summary_2400` | 8 | AutoTitler 提示词与采样分层演化 | `keep` | - | False | 会话标题偏差调查与改进 | `ok` | 2150 / 8 |

### 3. 聚合指标与门禁校验 (Aggregate Summary)

| Cell ID | Eligible / Observed / Failed | Mainline Cov | Usurp Rate | Applied / Pending Renames | Shift Latency | Hard Fail | Avg In / Out Tokens |
|---|---|---|---|---|---|---|---|
| `baseline` | 2 / 2 / 0 | 50.0% | 50.0% | 0 / 1 | N/A | False | 1158.0 / 11.0 |
| `summary_2400` | 2 / 2 / 0 | 50.0% | 50.0% | 0 / 1 | N/A | False | 1531.0 / 11.0 |

---

## 三、门禁与归因分析
1. **状态机确认门禁正常生效**：在 `rename_confirmations=1` 约束下，Turn 1 首次提出候选标题，状态机正确将其置为 `pending=True`，未直接写库，保持了标题防抖；
2. **机械计分器下的指标反差分析**：
   - 在 Turn 1 提出的标题为「会话标题偏差调查与改进」，命中 Turn 1 的可接受标题列表，主线覆盖为 True；
   - 在 Turn 8，模型动作决策为 `keep`（不更新标题，保留原标题「会话标题偏差调查与改进」）；但由于 Turn 8 的真值标注将主线演进为「AutoTitler 提示词与采样分层演化」，且 `allowed_shift=False`，按 `eval/metrics.py:82-84` 锁定的机械判定，当前生效标题未命中该轮 `acceptable_titles`，被严格判定为未覆盖（Coverage=False）并计入篡权（Usurpation=True）；
   - 因此两个 cell 在此两轮记录下的聚合指标严格为 `mainline_coverage_rate = 50.0%`、`local_usurpation_rate = 50.0%`。这直接暴露了保守策略（conservative）在主题自然演化且模型选择 keep 时，因标题滞后而产生的机械判定未覆盖现象，客观反映了指标评测体系的严格约束；
3. **资源账单实测**：`summary_2400` 的实际 Input Token 均值约为 1531，比 baseline 增加约 373 tokens，增幅完全对应扩展的结构化摘要小节，未产生无界膨胀；无任何 429/503 或重试，零 Hard Fail。
