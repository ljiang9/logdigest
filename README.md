# logdigest

把日志按模板聚类，输出级别统计、高频模板和错误摘要。纯本地运行，不联网；
只有 `--explain` 会调用 LLM 解释 top 错误模板。

## 安装

Python 3.10+，零第三方依赖：

```bash
python3 -m logdigest examples/app.log
```

## 用法

```bash
# 基本摘要
python3 -m logdigest app.log

# 只看 ERROR
python3 -m logdigest --level ERROR app.log

# 只看 10:00 之后的（需要日志带时间戳）
python3 -m logdigest --since "10:00" app.log

# 管道输入
tail -f app.log | python3 -m logdigest

# JSON 输出
python3 -m logdigest --json app.log > digest.json

# 请 LLM 解释 top 3 错误模板（需要 key）
export OPENAI_API_KEY=sk-...
python3 -m logdigest --explain app.log

# 先看 prompt，不花钱
python3 -m logdigest --explain --dry-run app.log
```

`--explain` 兼容任何 OpenAI-compatible 接口：

```bash
export OPENAI_BASE_URL=https://api.deepseek.com/v1
export OPENAI_MODEL=deepseek-chat
```

## 示例输出

```
===== 日志摘要：共 60 行 =====

## 级别统计
- INFO: 37
- ERROR: 12
- WARN: 9

## 高频模板（top 10）
1. [18 次（2026-10-05 09:00:00 ~ 2026-10-05 09:09:25）] user login succeeded: user_id=<NUM> from <IP> in <NUM>ms
   级别：INFO×18；示例：2026-10-05 09:00:00 INFO user login succeeded: ...

## 错误摘要（9 个错误模板）
- [4 次] db query timeout after <NUM>ms: SELECT * FROM audit_log WHERE day=<STR>
```

## 工作原理

1. **解析**：识别 `时间戳 + 级别 + 消息` 的常见格式（`2026-10-05 09:00:00 INFO ...`、
   `INFO: ...`、`09:00:00 WARN ...`）；认不出的行按 `UNKNOWN` 级别、整行当消息处理，
   不丢数据。
2. **mask**：把 IP、UUID、长十六进制串、引号字符串、数字替换成 `<IP>` / `<UUID>` /
   `<HASH>` / `<STR>` / `<NUM>` 占位符，剩下的就是模板。
3. **聚类**：单遍扫描按模板聚合，记录次数、级别分布、首次/末次出现、示例行。

## 诚实说明（局限）

- **模板 mask 是启发式**：不是真正的日志解析器。数字后面紧跟字母（如 `80ms`）能处理，
  但版本号 `v2.3.1`、十六进制短串等可能划分不准；极端格式下两个不同的模板可能被合并，
  或同一个模板被拆开。`--json` 拿原始行去核对最保险。
- **级别识别是启发式**：行首单词是已知级别（INFO/WARN/ERROR…）才算级别；
  以 "Error" 开头的普通英文句子可能被误判为 ERROR 级别。
- **错误摘要靠关键词**（error/exception/timeout/failed…）：不含关键词的错误发现不了；
  含关键词的正常行（如 `retrying failed task`）会被收录——这是故意的，宁可多看。
- **`--explain` 的解释是模型的猜测**：它只看到模板和一行示例，没看你的代码和指标；
  排查结论请以实际代码和监控为准。
- `--since` 依赖时间戳里能解析出 `HH:MM`；无时间戳的行在加 `--since` 时会被过滤掉。

## License

MIT
