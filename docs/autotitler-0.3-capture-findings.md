# 0.3 素材：插件实际收到的内容（真实数据，非推测）

以下全部为只读探针实测，数据源 = `~/.hermes/state.db`（1694 个 session，`get_messages_as_conversation(sid, include_ancestors=True)`），插件代码为当前 `main`（0.2.3.1）。探针脚本在 `cache/scratch/`，可复跑。

## 1. 语料规模

| 口径 | 数量 |
|---|---|
| 全部 session | 1694 |
| 活跃血缘 ≥ 100 条 | 369 |
| 活跃血缘 ≥ 400 条 | 86 |
| 最大血缘 | 1343 条（`20260911_042019_a643bc`，1,339,963 字符） |

## 2. 用户角色里到底是什么（86 个长会话汇总）

user 角色的消息并不都是用户说的话：

| 载体 | 条数 | 字符 | 占 user 角色字符 |
|---|---|---|---|
| `[System: …]` / `[System note: …]` / `[IMPORTANT: …]` / `[Your active task list …]` | 247 | 720,447 | **31.4%** |
| compaction 包装（`[CONTEXT COMPACTION — REFERENCE ONLY]` / `[PRIOR CONTEXT…]`） | 49 | 715,131 | **31.2%** |
| `[ASYNC DELEGATION BATCH COMPLETE — …]` 子代理结果 | 96 | 562,348 | **24.5%** |
| **真实用户输入** | 1205 | 289,860 | **12.7%** |
| `OUT-OF-BAND USER MESSAGE` 回放标记 | 12 | 4,175 | 0.2% |

结论：**用户角色里 87% 的字符不是用户意图**。这不是"权重"问题，是载体识别问题——模型看到的"用户消息"段里，近三分之一是系统噪声、近三分之一是历史摘要、四分之一是子代理审查报告。子代理报告尤其致命：它结构上就是"完整审查报告 + 最终判断"，篇幅和语气都像用户意图，天然会被当成主题。

## 3. 标题评估器实际收到多少（86 个长会话）

| 指标 | 结果 |
|---|---|
| 摘要成功进入 prompt | **27 / 86**（59 个 session 有 compaction 载体但模型看不到） |
| 喂给模型的内容占原始血缘比例 < 0.5% | 261 / 369（lineage≥100） |
| 喂给模型的内容占原始血缘比例 < 1% | 323 / 369 |
| `all_user` ≤ 2 条 | 149 / 369 |
| **完全收不到任何内容**（recent+all_user+opening+summary 全空） | **21 / 369** |

具体样本（lineage / 总字符 / 实际喂入字符 / 占比）：

```
20260911_042019_a643bc   1343   1,339,963   1,717   0.13%
20260906_194354_fb7bdf   1329     332,121   1,055   0.32%
20260905_211119_b665de   1324     325,255   1,013   0.31%
20260914_155705_4cbbb9   1296   1,662,011   2,852   0.17%
20260920_171319_9ea5d4    915   1,160,698  17,384   1.50%
```

模型看到的是原始对话的 0.1%–1.5%。它不是在"总结会话"，是在根据几条残片猜。

## 4. 三条已定位的硬缺陷（含实证）

### 4.1 21 个 session 的标题模型输入为空
全部 21 个都是同一形态：整个血缘只有 1 条 user 消息，且是

```
[System note: Your previous turn was interrupted mid-run — … The interrupted request was:]

qq bot一直想要绑定researcher
```

`is_system_noise()` 只做 `startswith` 前缀匹配，命中 `[system note:` 后**整条丢弃**——连同包装后面的真实用户请求一起丢。`clean_captured_text()` 从 `--- END OF CONTEXT SUMMARY ---` 之后取正文的逻辑不适用于这类"前缀噪声 + 空行 + 真实内容"的载体。语料中这种"interrupted mid-run"形态共 38 条。

### 4.2 23 个 session 的压缩摘要到不了模型
`earlier_summary` 的放行条件是 `if summaries and not saw_visible_opening`。压缩后只要有**任意一条**可见真实用户消息（通常就是一句"继续"），`saw_visible_opening=True`，摘要被整体丢弃。实测这 6 个 session 压缩载体后第一条真实用户消息分别是：

```
20260906_194354_fb7bdf   "完成全部摘要，不保留继续摘要，命名为summary…"
20260908_183604_868db3   "继续"
20260826_171402_f64e95   "creat没事，写入时间就行…"
20260822_203714_caf6dd   "派子代理再多方位审查，然后发布…"
20260901_173530_e08839   "独立只读评估 PR #83314 的上游价值…"
20260919_205447_d1d774   "顺便把整合清理了，我现在看着还有残留"
```

`20260908_183604_868db3` 的可见"opening"就是两个字"继续"——主线全在被丢掉的 21,445 字符摘要里。这正是"模型把局部当主线"的直接机制。

### 4.3 多轮压缩只取 `summaries[0]`
1694 个 session 里有 8 个存在多个 compaction 载体。以 `20260920_194554_afa67c` 为例，载体在 idx 0/327/459，正文分别 4448 / 2397 / 243 字符；代码只把 `summaries[0]` 交给模型，后两次压缩的进展（其中 idx=327 正是"修复了 compaction 解析"的阶段性结论）全部丢弃。
### 4.4 附带发现：merged carrier 的正则误命中
`_HANDOFF_END_RE = re.compile(r"---\s*end of context summary.*?---\s*", DOTALL)`。在 idx=0 这类"摘要正文里反引号引用了一段含 `--- end of context summary ---` 的示例文本"的载体上，该标记出现 3 次（6473 / 11152 / 13239）。正确边界应是 11152（其后接 `Anchor Index`、`Context Recovery` 等正文小节），代码取第一次命中 6473，把 9198 字符的可用摘要砍到 4549 字符——被丢掉的正是 `## Detailed Session Log`、`## Resolved Questions`、`## Relevant Files`、`## Anchor Index` 这些最能定位主题的分节。

修法方向：结束标记匹配应优先取**最后一次** `--- END OF CONTEXT SUMMARY`（带破折号包裹的完整形态），或要求标记行独占一行且后接文末；不能容忍正文内反引号引用的示例文本抢先命中。

### 4.5 子代理结果里的"嵌套压缩摘要"被误当成本会话历史
idx=459 是一条 `[ASYNC DELEGATION BATCH COMPLETE — deleg_1bbee97d]` 载体（`is_system_noise=True`，会被正确丢弃），但它内部含子代理自己上下文里的一份压缩块。`_extract_compaction_summary` 不区分载体归属，把这份**子代理会话**的摘要提取出来，塞进本会话的 `summaries`。同理 idx=327 的委派报告里有 2397 字符被当成压缩历史。

全量扫描（lineage≥100）：没有任何 session 的摘要**只**来自外部载体——即这条不会单独造成错误主题，但会让本会话自己的摘要被外部内容挤占（多载体时 `summaries[0]` 可能正是外部那份）。修法方向：只在载体本身是 compaction 包装时提取摘要（`[CONTEXT COMPACTION` / `[PRIOR CONTEXT` 出现在消息首部），子代理结果、replay、interrupted 等载体一律不作为摘要来源。

### 4.6 首屏样本：`20260920_194554_afa67c` 模型今天实际看到的全部内容
```
earlier_summary (138 chars):
  "## Historical Task Snapshot\nUser asked (…): '问题是这些标题都不是首次命名…现在模型看到的消息是什么样的，\n\n## Blocked\nNone."

opening[0] = [user] '` 且后面无真实用户输入，`clean_captured_text` 将其清洗为空字符串 (`None`)，导致 `is_summary()` 根本未触发，`load_contex'
             ^ 这不是用户说的话。它是 idx=0 压缩载体**正文内部**的一段文字
             （`_HANDOFF_END_RE` 在 6473 处误命中，把 6820 字符的正文后半段当成了"真实用户消息"）

recent[0]  = [user] '@url:`https://chatgpt.com/share/6aaf…`查看这个 … JSON.parse(t):'
recent[2]  = [user] '当前请求 ``` 这种消息不会满足 `startswith("[context compaction")`…'
             ^ 同上，仍是 idx=459 子代理报告经同一误命中留下的正文
recent[8]  = [user] 'Review recent work'      ← 16 条 all_user 里唯一接近真实意图的
```
一个 838 条消息、主线是"修复 AutoTitler 标题偏差"的会话，模型看到的"用户意图"里混着两段子代理审查报告和一条 `@url:` 附件噪声，真实用户输入被稀释到 1/16。这就是"标题被末尾支线带偏"的完整机制。

### 4.7 `summary_preview` 的尾部截断
`summary_preview` 的尾池逻辑是"整节装得下就整节，装不下退回 `smart_preview` 句子边界窗口"。修正 4.4 后的 9198 字符、12 小节摘要经 `summary_preview(_, 1200)` 得到 1174 字符，末节落在 `## Detailed Session Log`，`smart_preview` 的尾窗切在 `` 匹配到 `_HANDOFF_END_RE`（` `` 处——**句子边界被反引号里的中文括号打断，模型收到一个没有结尾的残句**。`_SENT_RE` 不把 `` ` `` 或 `（` 视为边界，这在中文 + 反引号混排的摘要里很常见。修法方向：尾窗找不到干净边界时，宁可整节放弃改取上一节，或在截断处补一个显式省略标记，不要交付看起来完整其实断在半句的文本。

## 5. 由此得出的方向（供研究文档讨论，不预设甜点值）

1. **载体识别优先于权重调节**：先把 user 角色拆成真实输入 / 系统噪声 / 历史摘要 / 子代理结果四类，四类各自的处置策略完全不同（真实输入全量、噪声剥离包装取内核、摘要作为独立锚点、子代理结果默认剔除或仅取结论句）。在此之前讨论 user/assistant 权重没有意义。
2. **"interrupted mid-run" 这类噪声包裹器必须解包**：噪声是前缀，不是整体。
3. **摘要的放行条件应从 `not saw_visible_opening` 改为"压缩载体在血缘中存在即作为独立历史锚点分层喂入"**，而不是和可见 opening 二选一。
4. **多轮压缩摘要需要合并策略**（最新覆盖 / 分区拼接），不能只取第一个。
