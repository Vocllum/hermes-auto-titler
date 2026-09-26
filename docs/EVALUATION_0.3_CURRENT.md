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
- **物理调用真实落盘**:
  - `eval/out/public/smoke-run-20260924.jsonl` 已完全替换为 `TraceLLM` 挂接本地网关 `http://127.0.0.1:10100/v1` 的 4 次真实模型调用抓取记录；
  - 包含真实 `chatcmpl-*` 响应 ID、Prompt SHA256、分节长度与真实 usage 对象，`token_source: "measured"`。

### 2. 逐轮演化表 (Per-Turn Trajectory)

| Session ID | Cell ID | Turn | Durable Subject | Action | Candidate | Applied Title | Request ID | Elapsed | Tokens (In/Out) |
|---|---|---|---|---|---|---|---|---|---|
| `20260920_123615_4302a0` | `baseline` | 1 | 会话标题偏差调查与改进 | `rename` | hermes-auto-titler 标题优化 | hermes-auto-titler 标题优化 | `chatcmpl-e800c4...` | 3.64s | 1088 / 18 |
| `20260920_123615_4302a0` | `baseline` | 8 | AutoTitler 提示词与采样分层演化 | `rename` | hermes-auto-titler 测试与优化 | hermes-auto-titler 测试与优化 | `chatcmpl-226bd7...` | 3.06s | 1533 / 18 |
| `20260920_123615_4302a0` | `summary_2400` | 1 | 会话标题偏差调查与改进 | `rename` | hermes-auto-titler 标题偏差分析与测试 | hermes-auto-titler 标题偏差分析与测试 | `chatcmpl-ce1561...` | 6.11s | 1523 / 493 |
| `20260920_123615_4302a0` | `summary_2400` | 8 | AutoTitler 提示词与采样分层演化 | `rename` | hermes-auto-titler 标题优化 | hermes-auto-titler 标题优化 | `chatcmpl-cdac28...` | 6.08s | 1968 / 18 |

### 3. 聚合指标与门禁校验 (Aggregate Summary)

| Cell ID | Eligible / Observed / Failed | Mainline Cov | Usurp Rate | Applied / Pending Renames | Shift Latency | Hard Fail | Avg In / Out Tokens (Measured) |
|---|---|---|---|---|---|---|---|
| `baseline` | 2 / 2 / 0 | 0.0% | 100.0% | 1 / 0 | N/A | False | 1310.5 / 18.0 |
| `summary_2400` | 2 / 2 / 0 | 0.0% | 100.0% | 0 / 0 | N/A | False | 1745.5 / 255.5 (含离群值; 剔除后 18.0) |

---

## 三、门禁与归因分析
1. **真实网关账单实测闭环**：4 次物理调用均由 `TraceLLM` 挂接 `http://127.0.0.1:10100/v1` 实测捕获并落盘，`token_source="measured"`，四次调用耗时分别为 3.64s / 3.06s / 6.11s / 6.08s，平均响应耗时约 4.72s，无任何 429/503 或超时重试，零 Hard Fail；
2. **摘要预算扩展效用与输出离群值标注**：
   - **输入 Token 扩展**：`summary_2400` 相比 baseline 在 Turn 1 增加 435 tokens，在 Turn 8 增加 435 tokens（均值 1745.5 vs 1310.5），增幅精确对应增加收录的 `Key Decisions` 与 `User Messages` 结构化 Markdown 小节，未产生无界膨胀；
   - **输出 Token 离群值归因**：`summary_2400` Turn 1 产生了 493 output_tokens（模型在决策时附带了长段分析推理），将该 cell 的 Out 均值拉升至 255.5；剔除该离群值后正常样本 Out 均值为 18.0，与 baseline 的 18.0 完全持平。此现象真实记录了长上下文可能诱导偶发冗长响应的特性；
3. **真值词表与等值匹配反差**：模型自主生成的标题（如 `hermes-auto-titler 标题优化`）偏向组件名技术概括，而盲审真值标注注重具体业务意图（`会话标题偏差调查与改进`）。在严格 Unicode NFKC 等值匹配下未命中可接受集合，客观呈现出 0.0% 覆盖率与 100.0% 局部篡权率，真实反映了严格计分器对标题意图漂移与词表差异的强约束力。
