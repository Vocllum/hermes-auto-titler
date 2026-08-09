# hermes-auto-titler

Hermes 插件：会话标题自动维护。每次对话结束后按配置的轮数间隔评估当前标题是否还准确，由独立模型判断并写回 Hermes session DB。

## 工作原理

- 挂 `on_session_end` hook（每轮对话结束触发），按 `every_n_turns` 轮数节流评估
- 评估输入：当前标题 + 最近 N 轮消息 +（可选）全部用户消息，只取纯文本，附件仅留占位符
- 判断模型独立于主对话（默认宿主模型，可配 deepseek-v4-flash），输出 `keep` / `rename` JSON
- 写回遵守 Hermes 标题来源优先级：**用户手改的标题永不覆盖**；自动标题（llm/derived）可更新且保持可升级
- 标题冲突（被其他会话占用）自动加后缀重试

## 安装

```bash
cd ~/.hermes/plugins
mkdir -p hermes-auto-titler
ln -s <repo>/config.yaml <repo>/hermes_auto_titler <repo>/plugin.yaml hermes-auto-titler/
```

启用插件（修改 ~/.hermes/config.yaml）：

```yaml
plugins:
  enabled:
    - hermes-auto-titler
  entries:
    hermes-auto-titler:
      llm:
        allow_model_override: true   # 允许插件指定模型（本地配 dsv4 时需要）
```

重启 Hermes 生效。验证：`/autotitler status`。

## 配置（插件目录 config.yaml）

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关；false 时 hook 不注册，零开销 |
| `every_n_turns` | `3` | 每 N 轮对话结束评估一次 |
| `on_close` | `true` | 会话关闭信号（gateway/退出）时强制再评估一次 |
| `recent_turns` | `2` | 每次评估携带最近 N 轮消息 |
| `opening_turns` | `2` | 额外携带会话开头 N 轮消息（让模型看到主线，避免被最新小任务带偏标题） |
| `ignore_model_messages` | `false` | true 时评估只喂用户消息，过滤模型回复（A/B 对比用） |
| `preview_chars` | `200` | 开头/结尾消息只保留前 N 字符（≈ 前几句话），全文梗概模式；0 = 不截断 |
| `include_all_user_messages` | `true` | 附加全部用户消息（不含附件内容） |
| `strategy` | `conservative` | `conservative` 明显不匹配才改（优先主线）/ `aggressive` 每次优化（优先最近主题） |
| `model` | `""` | 留空 = 宿主默认模型；本地示例 `deepseek-v4-flash` |
| `min_interval_minutes` | `5` | 同一会话两次评估的最短间隔 |
| `max_title_length` | `80` | 标题最大字符数（上限 100，Hermes 限制） |

## 命令

```
/autotitler status                          # 当前配置与状态
/autotitler config <key> [value]            # 查看/修改配置（写回 config.yaml，立即生效）
/autotitler rename-now [session_id]         # 立即评估当前（或指定）会话
/autotitler retitle-all [--dry-run] [--limit N] [--min-messages N]   # 批量重生成所有标题
```

## 成本

每次评估约 1.5K 输入 + 150 输出 token（deepseek-v4-flash 级别模型），另有轮数节流 + 时间节流，可忽略。

## 开发

```bash
uv venv .venv && uv pip install --python .venv/bin/python pytest PyYAML
.venv/bin/python -m pytest tests/ -v
~/.hermes/hermes-agent/venv/bin/python scripts/integration_check.py   # 真实 SessionDB 集成验证
```
