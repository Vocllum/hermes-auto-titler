# AutoTitler 当前状态与模型感知快照（2026-09-24）

## 口径与发布状态

- 本地工作树 `main` 当前基点 `8f3ea62`，相对 `origin/main` 超前 11 个提交；插件 manifest 显示 `0.3.0`，但最高本地 tag 是 `v0.2.3.1`。这里的 0.3.0 是本地准备版本，不能称为已发布版本。
- 插件已经启用，安装目录与项目仓库为同一目录；`hermes plugins validate .` 通过。测试基线为本次实跑的 `298 passed`（设置重排后为 299，见末尾）。
- 插件本地 `config.yaml` 只有 `every_n_turns: 1`，其余依 `DEFAULTS` 回退：`first_title_mode=builtin`、`strategy=conservative`、`title_style=concise`、`opening_turns=2`、`recent_turns=2`、`preview_chars=120`、`summary_preview_chars=1200`、`rename_confirmations=1`、`min_interval_minutes=5`。每轮检查的配置依然受五分钟冷却约束。
- **首轮配置冲突**：当前宿主 `auxiliary.title_generation.enabled=false`，但插件默认首轮交给宿主。0.3 插件不会自动改这个宿主开关。该组合下首轮原生标题无保障；可能出现 derived 临时标题，再由插件补评估。这里只读核实，未擅改宿主配置或模型路由。
- 现有 `docs/longrun_title_evolution_report.md` 标注测试日期 2026-09-20，审查对象为 HEAD `4873320`、manifest `0.2.0`。其中 V1–V4 同时改变了不同因素，不能拿来当 0.3 当前的独立变量量化胜负。`docs/RESEARCH_0.3_MODEL_PERCEPTION.md` 是输入覆盖与字符量研究，表中的字符数也不等于标题正确率。

## 当前模型实际收到什么（只读取证）

使用现有 `load_config`、`load_context_with_summary` 和生产 `AutoTitler._generate`，拦截 `ctx.llm.complete(messages=...)` 来看最终的 system/user 消息；无写库操作。对历史会话 `20260918_234034_c811f6`（原题 `opencodex-usage-meter 开发与优化`，source=`llm`）：

| 仅改变一项配置 | opening / recent / 意图轨迹（消息条数） | 历史摘要字符 | system / user prompt 字符 |
|---|---:|---:|---:|
| 当前配置 | 4 / 4 / 17 | 852 | 1426 / 2884 |
| `summary_preview_chars=2400` | 4 / 4 / 17 | 1972 | 1426 / 4004 |
| `ignore_model_messages=true` | 2 / 2 / 17 | 852 | 1426 / 2440 |
| `recent_turns=4` | 4 / 8 / 17 | 852 | 1426 / 3165 |

当前 user prompt 的结构依次为 `Visible continuation`（压缩后的局部开头）→ `Earlier-history summary` → `Recent context` → `Sampled user-intent trajectory` → `Current title`。其中历史摘要的 Goal 明确提到为 `opencodex-usage-meter` 设计 SVG showcase；最近两轮则是蓝圈展开逻辑与桌面重启后 Pin 丢失。它不是只收到了最后一条，但收到的**原始开头已被压缩**：开头栏承载的是压缩后的视觉设计阶段，早期原话仅靠摘要与用户轨迹间接恢复。`1200` 预算在这例还附带了 `Context Recovery` 操作说明；`2400` 版本又带入 `Active State` 文件清单与 `[SKILL_PRUNED]` 行，增加长度未必增加主线证据密度。真实历史摘要及用户原话含私人路径和情绪化内容，完整 payload 留在本地只读审计，不复制进报告。

## 当前代码对真实模型的只读生成调用（非回放状态机）

调用当前生产 `_generate`，保持 temperature=0、原 prompt/JSON parser 与辅助任务路由；只改摘要预算。**没有调用 `evaluate()`，未写标题或推进 pending/确认计数**。模型调用成功，但这只能评估单个完整会话快照的候选，不能推出多轮标题稳定率。

| 会话（原标题；来源） | 1200 字符预算 | 2400 字符预算 | 判读限制 |
|---|---|---|---|
| `20260918_234034_c811f6`（`opencodex-usage-meter 开发与优化`；llm） | keep | keep | 两者相同；整段已偏视觉制作，现题够宽泛 |
| `20260920_123615_4302a0`（`会话标题偏差调查与改进`；llm） | keep | rename → `hermes-auto-titler标题偏差优化` | 扩大摘要后更具体，仍需核对人类整体意图及后续确认 |
| `20260918_172644_e27106`（`Hindsight记忆治理与Hermes适配`；llm） | keep | keep | 两者相同 |
| `20260915_124025_f1bb70`（`Hermes 插件发布与子代理配置`；llm） | keep | keep | 两者相同 |
| `20260811_014847_ca8c2a`（`hermes-auto-titler 开发`；**user**） | rename → `hermes-auto-titler 发布与推广` | keep | **不计入可落库比较**：生产 `evaluate()` 会在模型调用前跳过 user 来源；此行仅作对照，暴露“单独调用生成器”不能代表最终行为 |

这次只有 4 个来源为 `llm` 的完整末端快照、每格一次模型调用，尚无独立人工真值、真实前缀时间序列、action/pending/approve/DB 写回轨迹与 token 使用账单。观察到 1/4 来源合法的样本因摘要预算不同而判定不同；**不能据此选 2400 为新默认**。旧报告的平均 2240/1630 tokens 也不是这一轮的测量。

## 接下来可验收的评测合同

1. 固定合格样本指纹：顶层、无父会话、至少 8 次真实人类输入、开始时标题来源允许自动更新；含压缩、多话题、主线+插问、明确转向。保存样本 id、仓库 HEAD、有效配置和实际生成模型路由。
2. 对每个样本从真实第一轮按时间增量构建沙箱 SessionDB，复用生产 `evaluate()`、JSON 解析、冷却与确认状态机；每组仅改一个变量。不要把最终全量消息误切成虚构轮次。
3. 每轮保存输入区块及字符/token、生成 action、候选、pending/approve、可见标题、失败状态；逐条展示原题→演变链→终题，并人工标注是否篡权、漏主线、延迟几轮、标识符是否准确。分开报告路由失败与质量失分。
4. 只有在同样本、同模型路由和完整状态机的对照得出有效结果后，才考虑改默认摘要预算或 prompt。绝不碰用户手改标题，也不发布 0.3 tag/Release。

## 设置页改动与验证

本次只调整插件 `plugin.yaml` 的 `config_schema` 字段顺序、描述和 `max_title_length` 的显示默认值：**不移除兼容字段，不改运行时默认值与模型路由**。重要项（启用、首轮归属、策略、标题样式、评估周期、关闭、确认、模型/服务商）移至最前；证据采样与长度参数后置；历史兼容项移到最后。宿主的 `plugin_settings_fields()` 按声明顺序输出 24 项，当前前九项顺序已核实；底层 Desktop 通用设置组件仍以双列平铺所有 24 项，尚无分组/折叠能力。要实现真正的“基础/高级”折叠，需要额外 UI 接入，不能把这次的排序说成已完成视觉重构。

测试：`299 passed`、`hermes plugins validate .`、schema 实际枚举顺序 24 项（见本次运行输出）。插件 manifest 的运行时重载是否已经到 Desktop，需要宿主重新扫描插件并实际在设置页核验；本报告不称其已在当前窗口显示。
