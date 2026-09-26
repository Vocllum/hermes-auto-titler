# 0.3 载体识别调研：第二轮（口径修正 + 新增缺陷）

第一轮结论写入 `autotitler-0.3-capture-findings.md` 后，我用同一探针把口径逐条复核，**修正了三处此前偏大的数字，并定位到四个此前没看到的缺陷**。以下全部为实测，可复跑（`cache/scratch/audit*.txt`）。

## 0. 口径修正（重要：此前汇报的数字要按这个收回）

| 指标 | 此前说法 | 复核后 | 原因 |
|---|---|---|---|
| user 角色非用户意图占比 | 87% | **90%**（按字符） | 按字符算是 90.0%；按条数是 30.8% |
| 完全收不到内容的会话 | 21 / 369 | **1 个交互会话**（+20 个 cron） | 21 里 20 个是 `cron_*`，它们本来就只有一条 `[IMPORTANT: You are running as a scheduled cron job…]`，没有人类主题可言 |
| 喂入量不到 1% 的会话 | 261 / 369 | **352 / 369**（口径：占全血缘含 tool） | 之前混用了"占 user+assistant"（3.1%）和"占全血缘"（0.23%）两个分母，必须分开说 |

正确的两个分母：

- **全血缘（含 tool）171,649,408 字符**，实际喂入 399,249 字符 = **0.23%**
- **user+assistant 12,887,788 字符**（tool 占 92.5%），实际喂入 = **3.10%**
- 逐会话中位数 **0.21%**，p10 **0.06%**

## 1. 真实用户意图的丢失率：18.0%

lineage ≥ 100 的 369 个会话里，去掉了 compaction / delegation / OOB 之后，**真实用户轮次 2620 条，只有 2149 条进了 prompt，471 条（18.0%）彻底消失**。30 个会话丢失过半。

丢失不是随机的，全部落在**前缀型系统包装**后面：

| 载体 | 全库条数 | 其中包裹着人类 payload |
|---|---|---|
| `[System note: …interrupted mid-run]` | 44 | **36** |
| `[System: The active model for this chat has changed to …]` | 186 | **18** |
| `[IMPORTANT: …]` | 238 | 0（payload 全是 skill 正文 / 后台进程输出） |
| `[Your active task list was preserved …]` | 89 | 0 |

`is_system_noise()` 是 `startswith` 前缀匹配，命中即**整条丢弃**，不区分"包装"和"包装里的东西"。实测被丢掉的人类原话：

```
[System: The active model for this chat has changed to …]
  → "你拿得出鸡巴？你自己视觉验证一下，妈的，文字读不读得清啊？…"

[System note: Your previous turn was interrupted mid-run …]
  → "给 aside 装上浏览器拓展，现在没有，然后给它换个 dia 的图标"
  → "qq bot一直想要绑定researcher"
  → "我想让你每天自己运营，然后账号知名度做大后接单之类…"
```

**关键判别**：`interrupted mid-run` 与 `model changed` 这两类包装，payload 100% 是人类原话（36/36、18/18 经人工核验）；`[IMPORTANT:` 与 `[Your active task list` 的 payload 是 skill 正文和任务清单，丢掉是对的。所以**不能一刀切"全解包"或"全丢弃"，要按包装类型分流**。

## 2. 压缩摘要：60% 的会话看不到，可用的只交付 2.0%

369 个会话中 225 个含压缩载体，摘要成功进入 prompt 的只有 **93** 个，**141 个（60%）被 `not saw_visible_opening` 门禁整体丢弃**——只要压缩后出现过任意一条可见用户消息（常常就是"继续"两个字），摘要就没了。

交付率同样难看：

| 项 | 数值 |
|---|---|
| 可用摘要正文总量 | 4,577,463 字符 |
| 实际交付 | 93,502 字符（**2.0%**） |
| 单份摘要交付比例 | 中位 5.94%，最差 1.68% |

`summary_preview(_, 1200)` 的按节取舍还**系统性丢掉了最能定位主题的小节**：

| 小节 | 可用 | 存活 |
|---|---|---|
| Historical Task Snapshot | 88 | 84 |
| Goal | 88 | 59 |
| **Completed Actions** | 88 | **0** |
| **Relevant Files** | 77 | **0** |
| **Detailed Session Log** | 46 | **0** |
| Context Recovery | 56 | 56 |

6 / 22 个小节零存活。Goal 只有 59/88 进了 prompt——**34 个会话的 Goal 在 prompt 里根本不存在**。

## 3. 新缺陷一：90 条"假摘要"来自子代理报告正文

`_extract_compaction_summary` 只要文本里出现 `[context compaction` 就提取，不检查载体归属。实测 **90 条消息本不是压缩载体，却被提取出 65,615 字符的"摘要"**，分布在 9 个会话——全是子代理审查报告和 assistant 自己的分析文字里**引用了压缩块示例文本**。

`20260920_194554_afa67c` 一个会话就有 15 处：idx=1/3/17/19/127/230/240/326/460/462/464/468 全是 assistant 消息里讨论"怎么提取压缩摘要"的正文，idx=327/459 是 `[ASYNC DELEGATION BATCH COMPLETE]` 报告。

这些假摘要进了 `summaries` 列表，和真摘要混在一起，`summaries[0]` 有可能取到外部内容。

## 4. 新缺陷二：7 个会话的摘要不是最新的

多载体时 `summaries[0]` 是血缘里第一个压缩块。实测 **7 个会话交付的是旧摘要，不是最新压缩进展**（`20260921_224856_46783e`、`20260920_194554_afa67c` 等）。用户在压缩后聊了两小时的新方向，模型看到的还是压缩那一刻的状态。

## 5. 新缺陷三：群聊信封把主题冲掉

`[Group chat: "Lattice"] You are @lynn, …` 这类信封每一轮都作为 user 消息落库，`clean_captured_text` 不剥它，整封送进 prompt。全库 125 封，信封样板 15,731 字符；348 个会话的 prompt 里有 18,376 字符是群聊信封（每轮重播整个房间记录）。4 个会话的**第一条**用户消息就是信封，这 4 个的标题全部退化成 `Group: rmu9s4i3q-xse6t · tmubbad09-vkdno` 这种哈希兜底——模型读到的是参与规则，不是议题。

## 6. 新缺陷四：prompt 里 14.7% 是重复文本

`all_user` 与 `opening` 的 358 条条目重复（358/3047），其中 53 条是群聊信封。399,249 字符交付量里 **58,744 字符（14.7%）是同一段文字送了两遍**——opening/recent 重叠的注释说"按轮次下标剔除重叠"，但 `all_user` 和 `opening` 之间没有去重。

## 7. 当前正确的 delivery 结构（348 会话汇总）

| 段 | 字符 | 占比 |
|---|---|---|
| all_user | 714,999 | 74.6% |
| summary | 92,435 | 9.6% |
| recent | 83,657 | 8.7% |
| opening | 67,424 | 7.0% |
| 用户意图面（open+alluser+summary） | — | **91.3%** |
| **recent 窗口里 assistant 占比** | — | **67.0%** |

`all_user` 一条就占了交付预算的 3/4，而它内部 96.1% 是 prose（其余 3.9% 是没剥掉的包装）——这是目前唯一在正常工作的通道，`user_message_threshold: 40` 触发的分层采样在 `20260911_042019_a643bc`（1343 条消息）上表现正常：12 条用户消息覆盖全弧线。

## 8. 修正后的修复优先级（按证据强度排序）

1. **`is_system_noise` 前缀命中即整条丢弃** → 按包装类型分流：`interrupted mid-run` / `model changed` 解包取 payload（54 条人类原话），`[IMPORTANT:` / task list 保持丢弃。
2. **`not saw_visible_opening` 门禁** → 压缩载体在血缘中存在即作为独立历史锚点，与 opening 分层共存（影响 141 个会话）。
3. **`_extract_compaction_summary` 载体归属** → 只在消息首部是 `[CONTEXT COMPACTION` / `[PRIOR CONTEXT` 时提取（消除 90 条假摘要）。
4. **多载体取最新而非 `summaries[0]`**（影响 7 个会话）。
5. **`summary_preview` 小节取舍** → Completed Actions / Relevant Files 零存活，需要显式优先级而不是头尾池。
6. **群聊信封剥离**（4 个会话标题退化为哈希兜底）。
7. **`all_user` × `opening` 去重**（释放 14.7% 预算）。

## 9. 仍未验证 / 需要实验

- 以上都是"模型收到什么"，还没有"模型因此输出什么"的端到端对比。修复 1–4 之后应该跑同一批 session 的前后标题差异，才能判断哪一项真正改变了主题锚定。
- `preview_chars=120` / `user_message_preview_chars=300` / `summary_preview_chars=1200` 这三个截断值目前没有任何实验支撑，只是历史默认值。不建议现在动。
