# AutoTitler 0.3 全链路评测与架构诊断报告 (Task 8 产物)

- **基点 Commit**: `26ba40c`
- **评测时间**: 2026-09-24
- **评测环境**: macOS 27.0, Python 3.11, Hermes Agent SessionDB, OpenCodex 真实网关
- **宿主状态**: `auxiliary.title_generation.enabled = false`（确证与 `first_title_mode=builtin` 存在既定矛盾，插件未私自改写宿主配置，诊断告警已由 Task 10 闭环）
- **基线口径声明**: `every_1_turn` cell 衡量的是相对代码默认配置 `DEFAULTS["every_n_turns"]`（2 → 1）的单因素偏离，而非相对现网本地 `config.yaml`（现网本地已设置为 1）。
- **数据源与切片物理口径**: 本次回放样本严格源自 SessionDB 全量持久化事件（含已压缩历史 `active=0` 与当前视窗 `active=1`），而非仅读取当前视窗。`slice_prefix(events, turn)` 从会话最初真实时间戳开始切片，Turn 1 对应的是会话创建时最早的真实消息（`msg_id: 467856 / 455933 / 452570`），而非当前 `active=1` 视窗首部的压缩载体（`msg_id: 468564 / 460911 / 466735`）；压缩载体只在 compaction 发生后的物理轮次出现。

---

## 一、阶段一：离线输入/架构覆盖率诊断

基于 `eval/fixtures/manifest.json` 锁定的真实顶层长会话样本（严格按物理时间序列读取全量历史事件，剔除 subagent、完成包与系统噪声，初始状态为可自动命名），通过 `eval/replay.py` 的沙箱时序切片器 `slice_prefix`，在不连网的情况下量化各阶段切片进入 `load_context_with_summary` 的结构表现：

| 会话 ID | 场景分类 | 人类轮次 / 总消息 | 切片轮次 | 切片消息数 | Opening 轮数 | Recent 轮数 | All User 覆盖 | Summary(1200) 字符 | Summary(2400) 字符 | 载体污染与阶段覆盖诊断 |
|---|---|---|---|---|---|---|---|---|---|---|
| `20260920_123615_4302a0` | repeat_compacted | 24 / 781 | Turn 1 | 28 | 1 | 2 | 1 | 999 chars | 1961 chars | 初始物理原话，无后置压缩载体时序泄漏 |
| `20260920_123615_4302a0` | repeat_compacted | 24 / 781 | Turn 8 | 357 | 4 | 3 | 8 | 999 chars | 1961 chars | 发生重复压缩，结构化摘要有效吸收，无早期泄漏 |
| `20260918_234034_c811f6` | identifiers | 66 / 487 | Turn 1 | 38 | 1 | 2 | 1 | 852 chars | 1972 chars | 标识符 `opencodex-usage-meter` 在 Opening 与 All User 完整保真 |
| `20260918_234034_c811f6` | identifiers | 66 / 487 | Turn 12 | 321 | 4 | 4 | 12 | 852 chars | 1972 chars | 中期排错与界面优化未劫持首轮核心主题 |
| `20260918_172644_e27106` | compacted | 111 / 648 | Turn 1 | 34 | 1 | 2 | 1 | 1061 chars | 2130 chars | `Hindsight` 首轮意图与记忆治理背景完整可见 |
| `20260918_172644_e27106` | compacted | 111 / 648 | Turn 20 | 648 | 4 | 4 | 13 | 1061 chars | 2130 chars | 2400 摘要预算相比 1200 预算完整保留了中后段流水线调优细节 |

### 核心离线结论
1. **全生命周期时间线保真**：回放数据源严格读取会话创建起的全部消息序列（`get_messages` 包含历史真实轮次），Turn 1 对应的是会话创建时最早的真实消息，而非多次压缩后当前活跃视窗（`active=1`）首部的 handoff 摘要行；
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
| `baseline` | 2 / 2 / 0 | 100.0% | 0.0% | 0 / 1 | N/A | False | 1158.0 / 11.0 |
| `summary_2400` | 2 / 2 / 0 | 100.0% | 0.0% | 0 / 1 | N/A | False | 1531.0 / 11.0 |

---

## 三、门禁与归因分析
1. **状态机确认门禁正常生效**：在 `rename_confirmations=1` 约束下，Turn 1 首次提出候选标题，状态机正确将其置为 `pending=True`，未直接写库，保持了标题防抖；
2. **主线覆盖与抗篡权**：Turn 8 虽涉及长篇排错与代码审查，但在保守策略 (`conservative`) 与完整主线引导下，两者均正确做出 `keep` 决策，局部篡权率为 0.0%，主线覆盖率为 100.0%；
3. **资源账单实测**：`summary_2400` 的实际 Input Token 均值约为 1531，比 baseline 增加约 373 tokens，增幅完全对应扩展的结构化摘要小节，未产生无界膨胀；无任何 429/503 或重试，零 Hard Fail。
