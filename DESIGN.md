# hermes-auto-titler 设计与实验文档

> 状态：v0.1.0 已发布，真实环境验证通过；参数矩阵实验完成（2026-08-10）
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
strategy: conservative           # conservative（主线优先，明显不匹配才改）| aggressive（每次优化，优先最近主题）
model: ""                        # 留空 = 宿主辅助模型；本地 deepseek-v4-flash
min_interval_minutes: 5          # 同一会话两次评估最短间隔（防抖）
max_title_length: 80             # 标题上限（SessionDB.MAX_TITLE_LENGTH=100 硬上限）
```

配置注释写给使用者：config.example.yaml 只写默认值和可选值，解释进 README。

---

## 4. 提示词设计

### 4.1 结构

```
[system]
You maintain concise, useful titles for Hermes conversations.
Return JSON only: {"action":"keep"|"rename","title":"..."}
{rule}                     ← 策略段（英文：conservative / aggressive / blind）
{style_req}                ← 风格段（英文：CONCISE STYLE / COMPLETE STYLE 全文）
The title must: identify at a glance; preserve main task; specific & searchable;
avoid generic words; match dominant user language; no quotation marks.
The configured maximum title length is {max_title_length} characters.
This is a hard safety limit, not a target length.

[user]
Current title: xxx          ← 非 blind 模式才给（避免旧标题引导模型）
Opening (the session's starting turns; the main through-line anchor):
                            ← opening 轮（每条 preview_chars 字符）——主线锚点
Recent (the latest turns; shows whether the conversation has shifted):
                            ← recent 轮（每条 preview_chars 字符）——判断是否转题
User-message trajectory (how the conversation evolved over time):
  Use it to identify recurring or sustained intent, not to collect every
  topic mentioned. A topic appearing in only a small portion of the
  trajectory should not override the conversation's established main
  subject unless the recent context shows a clear and sustained shift
  to that topic.
                            ← 意图轨迹（超长单条提取首尾句，超条数 head+tail 采样）
```

提示词全文为英文（2026-08-10 定稿，用户提供草稿）：策略段是唯一随
`strategy`/`blind` 变化的英文 rule；风格段 CONCISE/COMPLETE 原文照录。

### 4.2 策略段（strategy）

| 策略 | rule 内容 |
|---|---|
| conservative | 只有当前标题明显无法概括会话内容时才 rename，否则 keep。标题应概括会话的主要任务或主线，而不是最新的一条小任务：开头确立主题则优先主线命名；只有会话确实转向全新主题时才用最新主题命名 |
| aggressive | 每次都给出最能概括当前会话的标题；只要与当前标题不同就 rename。优先反映最近对话的主题，其次才是开头主线 |
| blind（retitle-all 专用） | 不提供原标题。直接根据会话内容给出最能概括的新标题，action 必须是 rename。+ 按策略保留主线/最近倾向 |

设计沿革：最初有 balanced 三档，岚拍板删除——「保守和激进不需要平衡、激进=更倾向于最近的消息」。

### 4.3 风格段（title_style）

| 风格 | 要求 |
|---|---|
| concise（默认） | 薇因 2026-08-10 定稿版：minimal sidebar label, not a summary——一个短短语、最少可识别概念；丢弃次要主题/结果/方法/平台设备限定词/实现细节；避免连词冒号逗号和多段标题；搜索性≠完整性；返回前再压缩一遍（remove every word that can be removed）+ 软词数锚点「Aim for at most five words. Use fewer whenever the conversation stays recognizable.」（词数比字符数语言无关：中文 5 词≈10-15 字、英文 5 词≈25-30 字符；实验 4 见 5.3） |
| complete | 5~10 个词的短语；可以覆盖主要脉络，多主题用「A 与 B」结构保留 |

设计动机（岚 2026-08-10）：评分时「注重概括包含完整信息但没考虑标题太复杂」——用户应能自定义要更完整的脉络还是更简洁准确的概括，但完整也不能太长。

### 4.4 通用要求（两风格共有）

具体、可检索（别人靠标题能找回这个会话）；避免「对话」「讨论」「问题」「查询」空泛词；语言跟随用户消息；不要引号。

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
6. **系统噪声必须过滤**：`[System: model changed]`、`[CONTEXT COMPACTION]`、`[ASYNC DELEGATION]`、`[System note: interrupted]`、`[Recent Summary]` 等 Hermes 注入消息混在 user 角色里，会污染意图轨迹
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

### 5.4 推荐配置（实验后的最优值）

```yaml
preview_chars: 100
opening_turns: 1
recent_turns: 1
include_all_user_messages: true
user_message_threshold: 40
user_message_preview_chars: 300
title_style: concise        # 需要完整脉络时切 complete
strategy: conservative
```

---

## 6. 验证过的坑（排障记录）

### 6.1 opencode-go 不支持 json_schema

`complete_structured` 在 opencode-go provider 每次返回 400 `invalid_request_error: This response_format type is unavailable now`，异常被 catch 后静默返回 keep——hook 跑了但从不改名，标题无任何变化。

**修复**：`ctx.llm.complete(messages=[...], temperature=0.2, max_tokens=150, timeout=30, purpose="auto-title")` + `_parse_decision` 容错解析（JSON → 提取含 action 的内嵌 JSON → 关键词正则启发式兜底）。任何 provider 兼容。

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
```

Git 提交身份：`git -c user.name="Vocllum" -c user.email="149675937+Vocllum@users.noreply.github.com" commit -m "..."`

---

## 8. V2 候选（未排期）

- 桌面设置页（`ROUTES_AREA` + `plugin_api.py`）
- retitle-all 跳过刚评估过的会话（`_last_eval` 时间戳过滤 ~5 分钟，一行实现，当前未加——显式操作重复评估场景少，保持轻量）
- legacy NULL 标题的官方升级路径（需上游支持，当前尊重保护）
