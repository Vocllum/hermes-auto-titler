# hermes-auto-titler 设计与实验文档

> 状态：v0.1.0 已发布，真实环境验证通过；参数矩阵实验完成（2026-08-10）；提示词 v13 定稿（四方案对比胜出，2026-08-10）
> 关联：README.md（使用说明）· scripts/（实验与验证脚本）

---

## 1. 项目背景与目标

Hermes 的会话标题体系只有 `derived`（首条消息截断）和手动 `/title`。长会话的 derived 标题是首条消息的截断产物，毫无概括力；而手动命名需要用户自己动手。竞品调研（2026-08-09）：

- Claude Code：仅手动 `/rename`，无自动 rename hook（上游 #29355 仍在请求）
- Codex CLI：标题 = 首条消息截断，不可改名（#15533 请求中）
- ChatGPT / Claude 网页：异步独立小模型生成标题（历史 gpt-3.5-turbo）

**结论：「对话结束后自动评估要不要改名」是空白点**——hermes-auto-titler 填的就是这个空。

核心价值主张：

1. **零上下文占用**：注册 hook 回调（本地 Python 代码），不注入工具 schema，每轮对话上下文零开销
2. **独立模型调用**：旁路小模型（默认 deepseek-v4-flash），不唤醒主 agent loop
3. **provenance 正确**：尊重 Hermes 标题来源体系，用户手改标题永不覆盖
4. **可配置策略**：conservative / aggressive 策略 + concise / complete 标题风格

---

## 2. 架构决策（已定稿，不再变更）

### 2.1 独立插件，不并入 hermes-lcm

| 候选 | 否决原因 |
|---|---|
| 并入 hermes-lcm | LCM 无会话级概念（只注册 subagent_start/subagent_stop/pre_llm_call，无 title 字段）；LCM 是本地 fork 背着补丁包袱（LCM_DISABLED_TOOLS 裁剪 + OpenAICompatibleProvider 补丁），加功能每次更新冲突面扩大 |
| 并入 Hermes 核心 | 核心 SessionDB 自带完整消息，但标题维护是纯旁路职责，不值得进核心 |
| **独立插件 hermes-auto-titler** ✅ | 标题归宿是 Hermes session DB（sessions.title），核心逻辑 ~200 行，与 LCM 更新节奏解耦 |

### 2.2 独立模型调用而非主模型

`on_session_end` hook 是 fire-and-forget：触发时主 agent loop 已退出，用主模型要重新唤醒整个 loop（阻塞且贵）。业界标准做法（ChatGPT 标题、OpenViking query_planner、LCM 摘要）都是旁路独立小模型。单次评估 ~1.5K 输入 + 300 输出 token，成本可忽略。

### 2.3 触发链

```
用户对话 N 轮
  → turn_finalizer.finalize_turn（每轮末尾）触发 on_session_end
  → 计数器 +1，every_n_turns 达标 or 会话关闭(on_close)
  → evaluate(session_id)
      → SessionDB.get_messages_as_conversation 读全文
      → load_context 提取（开头轮 / 最近轮 / 用户意图轨迹）
      → ctx.llm.complete() 独立模型判定 keep/rename
      → 写回（user 权威写 + 恢复 llm 来源）
```

- CLI 退出 / Ctrl+C 有 safety net（cli.py:18062、cli.py:1318、tui_gateway/server.py:716）
- payload 不带消息历史，插件从 SessionDB 读

### 2.4 标题来源体系与写回技巧（关键坑）

Hermes 标题来源（hermes_state.py:5904-6121）：

```
TITLE_SOURCE_DERIVED = "derived"  (rank 0)
TITLE_SOURCE_LLM     = "llm"      (rank 1)
TITLE_SOURCE_USER    = "user"     (rank 2，权威)
```

- `set_auto_title`（llm/derived）只在更高权威或空标题时落盘
- **llm 来源永远无法更新自身**（官方注释 "stops a session renaming itself"）——对持续维护标题致命

**绕过方案（已实现）**：自动标题更新走 `set_session_title`（user 权威写）+ 立即恢复 `set_session_title_source(llm)`。这是 Hermes 自己压缩时同款技巧（conversation_compression.py:3420-3438）。用户手改标题（source=user）永不碰。

**legacy NULL 保护**：provenance 列出现前的老行 `_title_rank(None)` 按 user 权威对待（与当年手动 /title 无法区分），llm 永远写不进去——官方保守设计，插件显式跳过并标注 `skipped`，不报 failed。

**标题冲突**：唯一性检查在来源优先级之后；`set_session_title` 冲突抛 ValueError，插件捕获后自动加后缀 ` (2)` 重试（i 从 2 到 19）。

---

## 3. 配置项设计

```yaml
enabled: true                    # 插件开关；plugins.enabled 移除 = 完全不加载
every_n_turns: 3                 # 每 N 轮评估一次（1 = 每轮）
on_close: true                   # 会话关闭时再评估一次
recent_turns: 2                  # 携带最近 N 轮消息（实验结论见 §5）
opening_turns: 2                 # 携带会话开头 N 轮消息（主线锚点）
ignore_model_messages: false     # true = 过滤 assistant 消息（A/B 实验用）
preview_chars: 200               # 开头/结尾消息只保留前 N 字符（≈前几句话）
include_all_user_messages: true  # 附加全部用户消息（意图轨迹，不截断、不含附件内容）
user_message_threshold: 20       # 用户消息条数上限（0=不限）；超限保留开头 1 条 + 最近 N-1 条
user_message_preview_chars: 200  # 单条用户消息触发线：超过则提取首尾句（各限一半预算）
title_style: concise             # concise（3~5 词一眼看完）| complete（5~10 词完整脉络）
strategy: conservative           # conservative（明显不匹配才改，主线优先）| aggressive（每次优化，但开头主线仍优先于最新子任务）
model: ""                        # 留空 = 宿主辅助模型；本地 deepseek-v4-flash
min_interval_minutes: 5          # 同一会话两次评估最短间隔（防抖）
max_title_length: 16             # 字符硬上限（中/英各算 1 字符；12 为目标、16 为上限，宁保关键实体用满 16）
max_display_width: 40            # 列宽硬上限（全角 2 列/半角 1 列；写回时双重截断）
```

配置注释写给使用者：config.example.yaml 只写默认值和可选值，解释进 README。

---

## 4. 提示词设计（v13 定稿；2026-08-11 语义化重构 LABEL/SUMMARY + 信息层级）

### 4.1 结构

```
[system]
You maintain concise titles for Hermes conversations.
Return JSON only: {"action":"keep"|"rename","title":"..."}
{rule}                     ← 策略段（英文：conservative / aggressive / blind）
{style_req}                ← 风格段（英文：LABEL STYLE / SUMMARY STYLE）
Information hierarchy:
- Subject comes from the session opening (or the earlier history
  summary when the original opening was compacted away); it is the
  main topic the session started about. A device or entity that
  appears only in the final turns is detail, not the subject.
- Main intent comes from the user-message trajectory.
- Recent turns show the latest event, current state, or a genuine
  topic shift; they must never define the subject by themselves.
Rules:
- Length is a guardrail, not the goal: aim for at most 12 characters;
  never exceed {max_title_len} (Chinese and Latin each count as 1 char).
- Keep key product names and identifiers exact and correctly cased;
  expand informal abbreviations from the user's messages instead of
  copying them: ov -> OpenViking, skill -> Skill, Codex, Hermes, DeepSeek.
- When the conversation has two distinct tasks, name both entities
  even if it uses the full length budget.
- No trailing punctuation, no quotes.
- Use the dominant language of the user's messages.
Good: {"action":"rename","title":"Dia密码导入Apple密码"}
Too narrow: {"action":"rename","title":"关闭验证注入"}
Too vague: {"action":"rename","title":"Code changes"}
Reply with JSON only.

[user]
Current title: xxx          ← 非 blind 模式才给（避免旧标题引导模型）
Opening (the session's starting turns; the main through-line anchor;
primary subject source):
                            ← 有真实 opening 时只放开头轮；摘要永远不混入
Earlier history summary (weak hint; may contain stale subtask details.
Use it only to recover the broad earlier topic when the original opening is unavailable):
                            ← 仅当原始 opening 因压缩不可见时提供；否则整段省略
Recent (the latest turns; the current event, state, or a genuine topic
shift — never the subject by itself):
                            ← recent 轮（每条 preview_chars 字符）——判断是否转题
User-message trajectory (how the conversation evolved; the main intent source):
                            ← 意图轨迹（超长单条提取首尾句，超条数 head+tail 采样）
```

设计要点（v13 与 v12 的差异）：

- **全局任务优先**：`{rule}` 与 `{style_req}` 都显式写「opening 确立的主线任务」是基调、「永远不要用最新子任务命名」。这是四方案对比后岚拍板的硬要求——GPT 逆向方案（extract key point）太倾向当前工作（产出「已修复」「已清理禁用」等完成态），v13 用「Name the MAIN TASK, not the latest subtask」对抗
- **few-shot 三例**：Good（`Dia密码导入Apple密码`）、Too narrow（`关闭验证注入`——实际是子任务却被 GPT 方案选中）、Too vague（`Code changes`）。「Too narrow」示例直接来自四方案对比中 GPT 列 #9 的翻车案例
- **实体优先于长度**：规则明确「宁可用满 16 字符也不丢关键产品名/标识符」——v11 曾因过度压缩丢掉 Codex/verify_on_stop/OpenViking；2026-08-11 再强化为「规范大小写 + 展开缩写」（ov → OpenViking、skill → Skill），因为实测模型会照抄用户消息里的小写/缩写写法（#10 会话「skill报错排查与ov清理」）
- **信息层级（2026-08-11 新增）**：Subject 只来自 Opening/history；Intent 来自用户轨迹；Recent 只是事件/转题信号、永远不能单独定义 Subject。这是 #7 会话（YICO 主线跑偏）教训的 prompt 层落地——压缩会话里 Recent 常被结尾 AI 复盘占满，没有层级约束时模型会拿结尾事件当主题
- **清洗**：`_clean_title` 式规范化（去引号、去 `Title:` 前缀、尾部标点 rstrip）+ `_write` 双重硬截断（字符上限 + 列宽上限）

### 4.2 策略段（strategy）

| 策略 | rule 内容 |
|---|---|
| conservative | 只有当前标题无法概括会话整体时才 rename，否则 keep（不再内置长段主线规则——主线约束已收进风格段，避免两处打架） |
| aggressive | 只要你的标题更好就 rename。**开头主线任务永远优先于最新子任务**（v13 变化：不再「优先最近主题」——岚四方案对比后确认全局任务优先） |
| blind（retitle-all 专用） | 不提供原标题。直接根据会话内容给出最能概括的新标题，action 必须是 rename。+ 按策略保留主线倾向 |

设计沿革：最初有 balanced 三档，岚拍板删除——「保守和激进不需要平衡、激进=更倾向于最近的消息」（v10 前）；v13 四方案对比后又把 aggressive 的「优先最近主题」改为「主线优先」，与岚的全局任务偏好对齐。

### 4.3 风格段（title_style）：信息类型是第一约束，长度只是护栏

| 风格 | 语义 | prompt 原文 |
|---|---|---|
| concise（默认） | **LABEL 主体标签**：Subject + 最小区分意图，不重述经过 | `LABEL STYLE: name the conversation. Identify its main subject and only the minimum intent needed to distinguish it. Do not retell what happened.` |
| complete | **SUMMARY 简短事件梗概**：Subject + 主要意图/事件/纠偏 | `SUMMARY STYLE: briefly describe what the conversation is mainly about. Preserve the main subject and the most important intent, event, or correction.` |

设计动机（岚 2026-08-11）：不用「3~5 词 / 5~10 词」这类长度语言定义风格——长度应降级成护栏（12 目标 / `max_title_length` 硬限仍保留在 Rules），信息类型才是第一约束。取用优先级：concise = Subject > Intent >>> Event；complete = Subject + Intent/Event；Recent 永远不能单独定义 Subject。

### 4.4 通用要求（两风格共有）

长度以字符计（中/英各 1 字符）：12 为目标、`max_title_length` 为硬上限（默认 16）；具体、可检索（别人靠标题能找回这个会话）；避免「对话」「讨论」「问题」「查询」空泛词；语言跟随用户消息；不要引号、无尾部标点。

---

## 5. 实验经验（参数矩阵，2026-08-10）

### 5.1 实验一：6 组配置 × 5 长会话（preview/轮数/AI/用户维度）

| 配置 | 平均分（20 分制） |
|---|---|
| C 少轮数（200/1/1/带AI/用户全量） | **8.8** |
| D 多轮数（200/3/3/带AI/用户全量） | 8.6 |
| B 完整消息（0/2/2） | 8.2 |
| A 基线（200/2/2） | 8.0 |
| E 过滤AI（200/2/2） | 7.6 |
| F 无用户全量（200/2/2） | **7.0** |

### 5.2 实验二：7 组配置 × 10 长会话（preview 梯度 + 轮数 + 风格）

| 配置 | 平均分（20 分制） |
|---|---|
| Scomp complete（100/1/1/complete） | **18.1** |
| P100 / P200 / P400（concise） | 17.3 |
| R22（100/2/2） | 17.0 |
| P50（50/1/1） | 16.4 |
| P0 完整消息（0/1/1） | **15.5**（含一次空输出） |

### 5.3 结论（已坐实）

1. **include_all_user_messages 是最高价值维度**——去掉用户轨迹后模型只能靠首尾猜，F 组垫底且出现跑偏（把「模型路由+晨报修复」写成「Codex 多代理验证」）
2. **complete 风格全面胜出**——双主题会话（消息平台+Raft、更新+SSH+sudo）concise 只能选一个主题，complete 用「A 与 B」全保住；最长的标题 27 字符，仍在「一眼看完」范围
3. **preview 100≈200≈400，0（完整）最差**——完整消息有噪声且可能超时空输出；50 信息不足。100 是最优性价比
4. **轮数 1/1 优于 2/2**——2/2 更容易被尾部话题带偏（「消息平台配置」被带成「修复 Raft 侧边栏」）。首尾各 1 轮 + 用户全量轨迹是最稳组合
5. **梗概模式输入规模砍半**（37,680→20,286 字符），判定基本一致
6. **系统噪声必须过滤**：`[System: model changed]`、`[CONTEXT COMPACTION]`、`[ASYNC DELEGATION]`、`[System note: interrupted]`、`MEMORY_MAINTENANCE_SUMMARY` 等 Hermes/cron 注入消息混在 user/assistant 角色里，会污染意图轨迹；`[Recent Summary]`/`[Session Arc Summary]` 属于另一类，按第 12 条单独处理
7. **用户消息上限（防超长对话）**：两个维度独立限制——条数超
   `user_message_threshold`（默认 20）时 head+tail 采样：保留开头 1 条
   （起点锚点）+ 最近 N-1 条（当前意图），中间旧主题（含压缩续接会话
   的祖先内容）对标题价值最低直接丢弃；单条超 `user_message_preview_chars`
   （默认 200）时用 `smart_preview` 提取首句 + 尾句（各限一半预算），无句子
   边界的长串（日志/代码）退化为前 2/3 + 后 1/3 硬切——替代 ChatGPT 的
   2/3+1/3 硬切方案（切断句子破坏语义）。preview_chars 只是触发线，
   提取策略固定为「首尾句」，短消息（≤触发线）永远原样保留
8. **上下文角色必须告诉模型**（2026-08-10 薇因评审）：Opening = 主线锚点、
   trajectory = 判断长期走势（不是关键词合集）、Recent = 判断是否已转题。
   轨迹段附英文说明（recurring/sustained intent；小比例主题不得覆盖已确立
   主线，除非 recent 显示持续转向）。这是「20 组抽样发现少数跑主线」后的
   修正——不是继续加上下文，而是把现有上下文的角色说精确
9. **改版前后 20 组抽样对比**（同 20 会话，SEED=7）：四版对比——
   v1 初版平均 23.55 字符（≤20: 7，≥30: 5）；v2 加角色说明 + 最短句 +
   head+tail 采样后 21.05（≤20: 10，≥30: 2）；v3 换薇因 concise 原版
   （minimal label + compress once more）后 19.65（≤20: 11，≥30: 3）；
   v4 在 v3 上加 5 词软锚点后 **16.6（≤20: 15，≥30: 0，最长 27）**，
   同配置第二轮 v4b 17.0（≤20: 14，≥30: 0）——方差小，稳定。v3 的三
   个反例全部收敛：#4 搜索选型 35→21→8（三服务名不再全堆）、#15
   29→17（括号限定词消失）、#20 31→24→23（平台词压缩）。词数锚点比
   规则描述更能压住模型「保留实体与限定词」的惯性；模型自然收敛到
   2-3 词/8-17 字符，多数在锚点内仍有余量
10. **few-shot 示例（v5，2026-08-10 岚侧边栏实测点名）**：岚看真实侧边栏
   指出仍太长，给出「行/不行」示例（行：Viking 插件功能审查、浏览器
   自动工作流、Windhawk备份恢复搞定；不行：搜索方案对比：Firecrawl、
   Tavily、AnySearch、TencentDB 替代 OpenViking 部署与数据迁移、Polymate
   QQ 机器人权限与限流设置、hermes-auto-titler 自动标题插件开发）。
   → 提示词加 Good/Bad→Better 示例 + 单专有名词规则（多于一个实体名=
   over-listing，只留最能识别的那个）。效果：v4b 17.0 → v5 15.7 平均，
   ≤10 从 3 → 6，≤15 从 6 → 9；「搜索方案对比」6 字符直接命中示例。
   剩余「长」分两类：可压缩的（Polymate 权限与限流设置 16，模型未完全
   跟示例）与实体本身长的（verify_on_stop/OpenCode Go/Desktop SSH——
   压缩即失去可检索性，属合理长度）。**要点：旧侧边栏标题是旧版代码
   生成的，新提示词需插件重载 + 重跑才可见**
11. **主线压缩约束（v6，2026-08-10 岚反馈「精简过头」）**：岚看 v5 结果
   指出「不够概括」——「自动标题 prompt 精简」反映的是当前子任务而非
   大主题；原版虽长但至少概括完整大意。教训：v3-v5 全在教「删」，没教
   「删完仍概括大主题」，模型为最短从 recent 取材。v6 修正：5 词锚点
   改为「recognizable as a whole」+ 新增「title 必须覆盖整个会话的主线，
   不能只是最近子主题；不确定时选更宽的主题」+ compress 段限定「压缩后
   不再覆盖主线就保留长版」。效果：平均 15.7 → 15.05（长度让步于概括
   性），但质量明显回归主线（codex 接入 opencode-go、Hermes symlink
   目录修复、Skill Viking 审查修复、闪白屏MPO修复 7 字符）；点名会话
   Polymate →「Polymate 配置」11 字符命中岚期望。残余：压缩续接会话的原始
   opening 被摘要替换时，不能把压缩后的助手干活消息当作全局主线；这一
   语义问题由第 12 条的独立 weak summary 段修正。
12. **压缩摘要是独立 weak hint（v7 重构，2026-08-10）**：压缩续接会话的原始消息被摘要替换，会话起点可能不存在于库中。摘要类前缀（`[Recent Summary]`/`[Session Arc Summary]`/`[Session Summary]`）先收集，但**永远不进入 Opening、Recent 或用户轨迹**：有真实 opening 时完全不提供摘要；只有摘要先于所有可见真实消息、说明原始 opening 可能已丢失时，才单独增加 `Earlier history summary (weak hint; may contain stale subtask details...)` 段。这样把摘要明确降级为恢复 broad topic 的弱线索，避免模型把摘要中的陈旧子任务当作当前 opening。摘要仍按 `preview_chars` 截断，取最早一条。对应回归测试覆盖「摘要在开头」「真实 opening 在摘要前」「摘要不进三类上下文」三种路径

### 5.4 提示词演进 v8→v13（2026-08-10 岚 0 分否决过度设计）

| 版本 | 做法 | 平均字符 | 结果 |
|---|---|---|---|
| v8 | 精简无示例 | 14.9 | 模型滑向描述句（「根治闪白屏：MPO 冲突」） |
| v9 | 列宽硬限 + 规范名 | 19.1 | 更长：删除示例后模型堆描述；40 列违反 1 次（45 列）；规范名规则造成冗余（「Hermes 记忆维护改为每周日」） |
| v10 | +否定式规则（名词短语/禁动作动词开头/禁和并与） | 14.2 | 长度恢复但副作用：丢关键实体（「闪白屏问题」丢 MPO；「Hermes verify_on_stop 验证注入机制」丢 codex/opencode-go） |
| v11 | **岚 0 分否决 v10**：「我们过度设计了」。砍到 3 句核心 + 12 目标/16 硬限 | 10.8 | 长度全达标（无超 16）但过度压缩：丢 Codex/verify_on_stop/OpenViking；「睡眠日志与分辨率排查」被 recent 带偏 |
| v12 | 长度措辞改「实体优先」：宁用满 16 不丢关键实体 | 13.4 | 语义回归主线（Codex装OpenViking/关闭verify_on_stop），但岚指出「书写不规范 + 一股 AI 味」（「日志实锤」「值得用吗」口语化、中英混排无空格） |
| v13 | 四方案对比定稿（见 5.5） | 6–12（10/10 达标） | 全局任务、名词短语、无 AI 味 |

教训：长度约束从「目标」改「护栏」后质量回升；AI 味的根源是「让模型总结全文」而非「命名意图」。

### 5.5 四方案对比（2026-08-10，同 10 会话 AB_SIDS 固定批次，conservative+blind）

| # | v13 全局任务 | v14 提取管线 | GPT 逆向 | Hermes 原生 |
|---|---|---|---|---|
| 1 | 飞书群聊放行配置 (8) | Mac侧泠月飞书群聊放行 (11) | 补全飞书群聊放行配置 (9) | 给泠月 Mac 飞书群聊放行配置 (13) |
| 2 | 合盖睡眠日志排查 (8) | 插电合盖睡眠排查 (8) | 实锤电池供电与4K (9) ⚠️口语 | 检查插电盒盖睡眠日志 (11) |
| 3 | 闪白屏MPO冲突排查 (10) | MyDockFinder闪白屏 (9) | MPO未关已修复 (7) ⚠️结果态 | 根治 MyDockFinder 闪白屏 MPO 冲突 (16+) |
| 4 | 搜索工具对比 (6) | Firecrawl评估 (8) | 搜索工具对比 (6) | 对比新搜索工具与现有方案 (13) |
| 5 | 记忆维护改每周日0点 (10) | Hermes记忆维护改每周日 (11) | 记忆维护改为周日零点 (10) | MEMORY_MAINTENANCE_SUMMARY… 💥首条是 cron 注入 |
| 6 | Dia密码导入Apple密码 (12) | 校友邦日志调整 (7) 💥幻觉 | Dia 全部密码导入 Apple (12) | 全部密码导入 Apple 密码 (12) |
| 7 | 触摸屏校准误认YICO (11) | Win平板触摸校准 (8) ⚠️丢YICO | YICO 是集线器非触摸屏 (12) | 重新连接线后测试点击 (10) ⚠️只看到首条 |
| 8 | OpenViking双端配置 (10) | Codex装OpenViking (8) | 两边Codex配置OpenViking (11) | 两边 codex 安装配置 open Viking (16+) |
| 9 | Codex切换与验证注入 (11) | Codex切换与注入排查 (10) | 关闭多余验证 Codex 互不相干 ⚠️脑补 | 评估 opencode go 与 cc switch 切换管理 (17+) |
| 10 | skill报错与清理OV (11) | skill排查与清理ov (10) | 插件致错已清理禁用 (9) ⚠️结果态 | 排查 skill 错误并清理 ov 服务器 (15+) |

四方案定义与结论：

- **v13（定稿）**：opening+recent+全量用户轨迹 → 全局任务 prompt。10/10 名词短语、无 AI 味、6–12 字符；#3 是「排查」（对应开头还在问根因）而非「已修复」——全局任务优于当前工作的直接体现
- **v14 提取管线**（岚提议：不喂原文，先提取再命名）：先 LLM 提取 {main_task, opening_subject, key_entities, user_intentions} 再喂标题模型。输入压缩真实（#4 从 15,057 → 751 字符，20 倍），但 **提取幻觉**：#6 提取成「校友邦日志调整」完全跑偏（提取一步错标题全错）；#7 丢 YICO 实体；且每次评估多一次 LLM 调用。结论：不默认启用，记入 V2 候选（超长会话降级选项），正常规模用 preview_chars 截断已足够
- **GPT 逆向**（社区逆向：`---BEGIN Conversation---` 包对话 + `Summarize the conversation in 5 words or fewer` + `Your goal is to extract the key point`）：简洁但**太倾向当前工作**——「已修复」「已清理禁用」都是完成态；「实锤」口语；「互不相干」脑补。确认岚的判断
- **Hermes 原生**（上游 title_generator.py `_TITLE_PROMPT_TEMPLATE` 复刻：只喂首条用户消息 + 3-7 words + `Name what the user wants DONE` + few-shot）：只喂首条消息的脆弱性暴露——#5 首条是 cron 注入（MEMORY_MAINTENANCE_SUMMARY）直接跑飞输出整段；#7 只看到「重新连接线后测试点击」丢全局。我们插件的噪声过滤 + 全量轨迹更有价值

### 5.6 推荐配置（实验后的最优值）

```yaml
preview_chars: 100
opening_turns: 1
recent_turns: 1
include_all_user_messages: true
user_message_threshold: 40
user_message_preview_chars: 300
title_style: concise        # 需要完整脉络时切 complete
strategy: conservative
max_title_length: 16        # v13 定稿：字符硬上限（中/英各 1 字符）
```

---

## 6. 验证过的坑（排障记录）

### 6.1 opencode-go 不支持 json_schema

`complete_structured` 在 opencode-go provider 每次返回 400 `invalid_request_error: This response_format type is unavailable now`，异常被 catch 后静默返回 keep——hook 跑了但从不改名，标题无任何变化。

**修复**：`ctx.llm.complete(messages=[...], temperature=0, max_tokens=150, timeout=30, purpose="auto-title")` + `_parse_decision` 容错解析（JSON → 提取含 action 的内嵌 JSON → 关键词正则启发式兜底）。`temperature=0` 固定标题生成，减少同一输入仅因采样产生的漂移；任何 provider 兼容。

**temperature 对比（2026-08-10）**：固定 10 个真实会话各调用一次 `0.2` 与 `0`，仅 2/10 次输出相同。`0.2` 平均 12.2 字符、最长 18；`0` 平均 12.6 字符、最长 16。`0` 的结果在「AnySearch 值不值得用」「opencode-go 切换与验证」等会话里保留了更明确的任务/结果边界，未观察到质量退化；因此正式调用和比较脚本统一使用 `temperature=0`。这是一组质量抽样，不把一次调用当成 provider 的绝对确定性保证。

### 6.2 目录插件必须根目录有 `__init__.py`

Hermes `_load_directory_module` 要求插件根目录直接有 `__init__.py`，否则 `plugins list` 显示 enabled 但 `register()` 静默不执行（agent.log 报 No __init__.py）。子包结构（hermes_auto_titler/）不会被自动发现。

**修复**：`plugin_entry/__init__.py` 薄入口 re-export 子包 register，symlink 到安装目录。

### 6.3 无热重载

改代码后必须重启 Hermes 后端进程（Desktop Cmd+Q 彻底退出，或杀 serve 进程自动拉起）。曾经「修复落地了但进程还是旧代码」导致白排查一轮。

### 6.4 `hermes config set` 列表键陷阱

`hermes config set plugins.enabled '["a","b"]'` 把列表存成字符串，需 Python + yaml.safe_dump 修正为真 YAML 列表。"not a recognized config key" 提示是正常的（插件自定义键）。

### 6.5 模型 override 门控

`plugins.entries.<id>.llm.allow_model_override` 默认 fail-closed，必须显式 `hermes config set plugins.entries.hermes-auto-titler.llm.allow_model_override true` 才能用 config 里的 model。

### 6.6 derived 长标题强制重生成

`derived` 来源 + 标题 >40 字符 = 首条消息截断产物，模型即使判定 keep 也要强制 rename（`force_rename` 追加「当前标题是自动截断的长文本，不合格」）。

### 6.7 cron 注入消息会伪装成首条用户消息（已修复）

四方案对比 #5 暴露：cron 任务注入的会话会写入 `MEMORY_MAINTENANCE_SUMMARY …` 标记（消息角色可能是 user 或 assistant）。Hermes 原生方案只喂首条消息，直接把它当用户意图，标题跑飞成整段注入文本。

**修复已落地**：`_SYSTEM_NOISE_PREFIXES` 加入大小写不敏感的 `memory_maintenance_summary` 前缀，和 `[system:]`、`[context compaction]`、`[async delegation]`、`[important:]` 一样在进入 recent/opening/用户轨迹前过滤；回归测试覆盖 marker 不出现在三类上下文中。该项不再属于 V2 候选。

### 6.8 压缩会话的「结尾实体」会抢走 Subject（调查：YICO 案例）

2026-08-11 岚问：会话 `20260810_005212_f9e167` 主体明明是「副屏触摸屏校准」，为什么生成「YICO实为sRGB集线器」？取证（59 条消息）：

- 真实用户消息只有 3 条（重连线 / 关机 / 「为什么是 yico」），首条是 8387 字符压缩摘要，assistant 28 条 + tool 27 条——**AI 消息占绝对多数**
- 轮切分按 user 开头：压缩后第一个真实 user 在 #32，前 31 条 assistant 残骸全挤进第一个 opening 轮，无用户提问锚点
- Recent 最后 7 条里 6 条是 assistant 的 YICO 复盘；轨迹最后一条也是用户问 YICO；摘要前 200 字符内 YICO 出现 3 次（活动任务就叫「YICO 触摸屏校准」）——信息分布上 YICO 完全主导
- 温度对比实验（/tmp 临时脚本）没同步 `earlier_summary` 4 元组改动，模型连摘要锚点都没有

结论：不是单一原因，是「压缩后 opening 无 user 锚点 + Recent 被结尾 AI 复盘占满 + YICO 高频」叠加。prompt 层修复 = Information hierarchy（Subject 只来自 Opening/history、Recent 永远不能单独定义 Subject、结尾才出现的设备/实体不是 Subject）；结构层修复 = 摘要作为独立 weak hint 提供（已在前一版落地）。实测：非盲改路径（真实运行）下该会话 keep 现标题「触摸屏校准误认 YICO 集线器」，不再被 YICO 抢跑；盲改（retitle-all）模式下对 YICO 偏好仍不稳定，属模型能力边界，未继续堆 prompt。

### 6.9 模型照抄用户消息的小写/缩写实体（调查：skill/ov 案例）

会话 `20260810_015334_f41e41` 只有 1 条用户消息（原文即小写「skill」「ov」），模型生成「skill报错排查与ov清理」——照抄而非规范。修复 = Rules 直白映射（`ov -> OpenViking, skill -> Skill`）+ 双任务双实体规则。实测：盲改输出「Skill报错排查与清理」、非盲改输出「Skill排错与OV清理」，规范大小写生效；OV 缩写仍在 12 字符护栏内可接受（岚确认）。

---

## 7. 命令速查

```bash
# 单测（项目 .venv）
cd ~/Hermes/Home/Projects/hermes-auto-titler && .venv/bin/python -m pytest tests/ -q

# 真库集成验证（hermes venv，能 import hermes_state）
~/.hermes/hermes-agent/venv/bin/python scripts/integration_check.py

# 真实端到端（PluginLlm + dsv4 + 真实会话，写库）
~/.hermes/hermes-agent/venv/bin/python scripts/e2e_check.py [session_id]

# 批量重命名（dry-run 先看范围；--limit/--min-messages 防手滑）
~/.hermes/hermes-agent/venv/bin/python scripts/retitle_all.py --dry-run

# 抽样审查（--blind 预览盲改模式；默认跳过 user 手改标题）
~/.hermes/hermes-agent/venv/bin/python scripts/review_sample.py [--blind] [--n 10]

# 参数矩阵实验（不写库）
~/.hermes/hermes-agent/venv/bin/python scripts/eval_matrix.py

# A/B 对比（with vs without 模型消息，不写库）
~/.hermes/hermes-agent/venv/bin/python scripts/ab_compare.py

# 四方案对比（v13 / v14 提取管线 / GPT 逆向 / Hermes 原生，不写库）
AB_SIDS="前缀1,前缀2,..." ~/.hermes/hermes-agent/venv/bin/python scripts/compare_titler_schemes.py
```

Git 提交身份：`git -c user.name="Vocllum" -c user.email="149675937+Vocllum@users.noreply.github.com" commit -m "..."`

---

## 8. V2 候选（未排期）

- 桌面设置页（`ROUTES_AREA` + `plugin_api.py`）
- retitle-all 跳过刚评估过的会话（`_last_eval` 时间戳过滤 ~5 分钟，一行实现，当前未加——显式操作重复评估场景少，保持轻量）
- legacy NULL 标题的官方升级路径（需上游支持，当前尊重保护）
- v14 提取管线作超长会话降级选项（输入 20 倍压缩但提取幻觉风险，#6 跑偏案例，见 §5.5；触发条件可定为「输入超阈值才启用」）
- 中英混排空格规范化（`Codex装OpenViking` → `Codex 装 OpenViking`，v12 暴露的书写规范缺口，v13 靠 prompt 示例缓解，未做代码层 normalize）
