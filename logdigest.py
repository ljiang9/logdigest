#!/usr/bin/env python3
"""logdigest - 把日志按模板聚类，输出级别统计、高频模板和错误摘要。

可选 --explain：把 top 错误模板发给 OpenAI-compatible /chat/completions，
请模型给出可能原因和排查建议（纯本地聚类不需要 key）。
"""
import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
from collections import Counter

LEVELS = {"TRACE", "DEBUG", "INFO", "WARN", "WARNING", "ERROR", "CRITICAL", "FATAL"}

LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?|\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)?"
    r"\s*(?P<rest>.*)$"
)
LEVEL_HEAD_RE = re.compile(r"^(?P<level>[A-Za-z]+)(?:\s*[:|\-\]]\s*|\s+)(?P<msg>.*)$")
HM_RE = re.compile(r"(\d{2}):(\d{2})")

ERROR_KW_RE = re.compile(
    r"error|exception|failed|failure|timeout|timed out|traceback|critical|fatal|panic|denied|refused",
    re.IGNORECASE,
)

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?\b")
HASH_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")
QUOTED_RE = re.compile(r"\"[^\"]*\"|'[^']*'")
NUM_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")


def parse_line(raw, lineno):
    """解析一行日志 -> (ts_text, minutes|None, level, msg)。容忍未知格式。"""
    line = raw.rstrip("\n")
    ts_text, rest = None, line
    m = LINE_RE.match(line)
    if m:
        ts_text = m.group("ts") or None
        rest = m.group("rest")
    level, msg = "UNKNOWN", rest
    lm = LEVEL_HEAD_RE.match(rest)
    if lm and lm.group("level").upper() in LEVELS:
        level = lm.group("level").upper()
        if level == "WARNING":
            level = "WARN"
        msg = lm.group("msg")
    minutes = None
    if ts_text:
        hm = HM_RE.search(ts_text)
        if hm:
            minutes = int(hm.group(1)) * 60 + int(hm.group(2))
    return {"lineno": lineno, "ts": ts_text, "minutes": minutes, "level": level, "msg": msg, "raw": line}


def mask(msg):
    """把可变部分替换成占位符，得到模板。启发式，不是精确解析。"""
    msg = UUID_RE.sub("<UUID>", msg)
    msg = IP_RE.sub("<IP>", msg)
    msg = HASH_RE.sub("<HASH>", msg)
    msg = QUOTED_RE.sub("<STR>", msg)
    msg = NUM_RE.sub("<NUM>", msg)
    return msg


def cluster(records):
    templates = {}
    order = []
    for r in records:
        tpl = mask(r["msg"])
        if tpl not in templates:
            templates[tpl] = {
                "template": tpl,
                "count": 0,
                "levels": Counter(),
                "first_lineno": r["lineno"],
                "last_lineno": r["lineno"],
                "first_ts": r["ts"],
                "last_ts": r["ts"],
                "example": r["raw"][:160],
            }
            order.append(tpl)
        t = templates[tpl]
        t["count"] += 1
        t["levels"][r["level"]] += 1
        t["last_lineno"] = r["lineno"]
        t["last_ts"] = r["ts"]
    return [templates[k] for k in order]


def get_key(args):
    return args.api_key or os.environ.get("LOGDIGEST_API_KEY") or os.environ.get("OPENAI_API_KEY")


def call_llm(prompt, args):
    key = get_key(args)
    if not key:
        sys.stderr.write("error: --explain 需要 API key：请设置 OPENAI_API_KEY 环境变量，或用 --api-key 传入。\n")
        sys.exit(1)
    base = (args.base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    model = args.model or os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
    body = json.dumps({
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": "你是一名资深 SRE。请根据日志模板给出可能原因和排查建议，简洁中文，分点回答。不要编造没有证据的结论。"},
            {"role": "user", "content": prompt},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        base + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.stderr.write("error: 模型请求失败（HTTP %s）：%s\n" % (e.code, e.read().decode("utf-8", "replace")[:300]))
        sys.exit(1)
    except urllib.error.URLError as e:
        sys.stderr.write("error: 网络请求失败：%s\n" % e.reason)
        sys.exit(1)
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        sys.stderr.write("error: 模型返回结构异常：%s\n" % json.dumps(data)[:300])
        sys.exit(1)


def build_parser():
    ap = argparse.ArgumentParser(prog="logdigest", description="日志模板聚类与错误摘要（纯本地；--explain 才联网）")
    ap.add_argument("logfile", nargs="?", help="日志文件路径；不给则从 stdin 读")
    ap.add_argument("--top", type=int, default=10, help="显示 top N 模板（默认 10）")
    ap.add_argument("--level", help="只看某个级别，如 ERROR（WARNING 会被归一为 WARN）")
    ap.add_argument("--since", help='只看某个时间之后的日志，如 --since "10:00"（需要日志带时间戳）')
    ap.add_argument("--explain", action="store_true", help="请 LLM 解释 top 3 错误模板（需要 API key）")
    ap.add_argument("--dry-run", action="store_true", help="只打印要发给模型的 prompt，不联网")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--model", help="模型名（默认 gpt-4o-mini，可用 OPENAI_MODEL 覆盖）")
    ap.add_argument("--base-url", help="OpenAI-compatible base URL（默认 https://api.openai.com/v1）")
    ap.add_argument("--api-key", help="API key（默认读 OPENAI_API_KEY）")
    ap.add_argument("--timeout", type=int, default=60, help="模型请求超时秒数（默认 60）")
    ap.add_argument("--version", action="version", version="logdigest 0.1.0")
    return ap


def read_input(args):
    if args.logfile:
        try:
            with open(args.logfile, encoding="utf-8", errors="replace") as f:
                return f.read().splitlines()
        except OSError as e:
            sys.stderr.write("error: 打不开文件 %s：%s\n" % (args.logfile, e))
            sys.exit(1)
    if sys.stdin.isatty():
        sys.stderr.write("error: 请指定日志文件，或通过管道传入日志。\n")
        sys.exit(1)
    return sys.stdin.read().splitlines()


def main():
    args = build_parser().parse_args()
    lines = read_input(args)
    records = [parse_line(raw, i + 1) for i, raw in enumerate(lines) if raw.strip()]
    if not records:
        sys.stderr.write("error: 输入为空，没有可分析的日志行。\n")
        sys.exit(1)

    if args.level:
        want = args.level.upper()
        if want == "WARNING":
            want = "WARN"
        records = [r for r in records if r["level"] == want]

    if args.since:
        hm = HM_RE.search(args.since)
        if not hm:
            sys.stderr.write('error: --since 格式不对，示例：--since "10:00"\n')
            sys.exit(1)
        since_min = int(hm.group(1)) * 60 + int(hm.group(2))
        records = [r for r in records if r["minutes"] is not None and r["minutes"] >= since_min]

    if not records:
        sys.stderr.write("error: 过滤后没有剩余日志行。\n")
        sys.exit(1)

    templates = cluster(records)
    templates.sort(key=lambda t: t["count"], reverse=True)
    level_counts = Counter(r["level"] for r in records)
    errors = [t for t in templates if ERROR_KW_RE.search(t["template"])]

    if args.explain or args.dry_run:
        targets = errors[:3]
        if not targets:
            sys.stderr.write("提示：没有发现错误模板，无需解释。\n")
        if args.explain and not get_key(args):
            sys.stderr.write("error: --explain 需要 API key：请设置 OPENAI_API_KEY 环境变量，或用 --api-key 传入。\n")
            sys.exit(1)
        for i, t in enumerate(targets, 1):
            prompt = (
                "下面是从生产日志里聚类出的错误模板（共出现 %d 次，级别分布 %s）：\n"
                "模板：%s\n示例行：%s\n"
                "请给出：1）最可能的 2-3 个原因；2）排查建议（先查什么、看什么指标）；3）是否需要立即处理。\n"
                "只根据模板内容推断，不确定的请说明。"
                % (t["count"], dict(t["levels"]), t["template"], t["example"])
            )
            if args.dry_run:
                print("===== prompt #%d（dry-run，未调用网络）=====" % i)
                print(prompt)
                continue
            print("【错误模板解释 #%d】出现 %d 次" % (i, t["count"]))
            print("模板：%s" % t["template"])
            print(call_llm(prompt, args))
            print()
        return 0

    if args.json:
        out = {
            "total_lines": len(records),
            "levels": dict(level_counts),
            "templates": [
                {
                    "template": t["template"], "count": t["count"],
                    "levels": dict(t["levels"]),
                    "first_lineno": t["first_lineno"], "last_lineno": t["last_lineno"],
                    "first_ts": t["first_ts"], "last_ts": t["last_ts"],
                    "example": t["example"],
                }
                for t in templates[: args.top]
            ],
            "errors": [
                {"template": t["template"], "count": t["count"], "example": t["example"]}
                for t in errors
            ],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print("===== 日志摘要：共 %d 行 =====" % len(records))
    print("\n## 级别统计")
    for lv, n in level_counts.most_common():
        print("- %s: %d" % (lv, n))
    print("\n## 高频模板（top %d）" % args.top)
    for i, t in enumerate(templates[: args.top], 1):
        span = ""
        if t["first_ts"] or t["last_ts"]:
            span = "（%s ~ %s）" % (t["first_ts"] or "?", t["last_ts"] or "?")
        print("%d. [%d 次%s] %s" % (i, t["count"], span, t["template"]))
        print("   级别：%s；示例：%s" % (
            "、".join("%s×%d" % (k, v) for k, v in t["levels"].most_common()), t["example"]))
    print("\n## 错误摘要（%d 个错误模板）" % len(errors))
    if not errors:
        print("无。")
    for t in errors:
        print("- [%d 次] %s" % (t["count"], t["template"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
