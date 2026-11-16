#!/usr/bin/env python3
"""把每日简报压缩成精简摘要，通过 pushplus 推送到微信。

项目本身不内嵌推送通道（SKILL.md：「推送通道属宿主环境，脚本不内嵌推送」），
本脚本是给 GitHub Actions 用的最薄一层：只解析简报文本，不依赖项目内部模块，
所以可以单独运行、单独测试。

用法：
    export PUSHPLUS_TOKEN=xxxxx
    python scripts/push_pushplus.py --in reports/daily.md

    # 只打印摘要，不发送
    python scripts/push_pushplus.py --in reports/daily.md --dry-run

    # 简报文件缺失时的兜底文案（例如选股脚本整体失败）
    python scripts/push_pushplus.py --in reports/daily.md --fallback "今日简报生成失败"

pushplus 文档：https://www.pushplus.plus/doc/guide/api.html
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date

PUSHPLUS_API = "https://www.pushplus.plus/send"

# 简报用 45 个 = / - / ! 做段落分隔线
SEP_RE = re.compile(r"^\s*[=\-!]{20,}\s*$")
# 推荐条目行：4 空格缩进 + 奖牌（🥇 / 4.）+ 6 位代码 + 名称
REC_RE = re.compile(r"^\s{4}(\S+)\s+(\d{6})\s+(.+?)\s*$")
# 长线条目：4 空格 + "1." + 🎯 + 代码 + 名称
LONG_RE = re.compile(r"^\s{4}(\d+\.)\s*🎯\s*(\d{6})\s+(.+?)\s*$")
DATE_RE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")


def split_blocks(text):
    """按「分隔线 / 段头行 / 分隔线」把简报切块。"""
    lines = text.splitlines()
    blocks, cur = [], None
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        if SEP_RE.match(line.strip()):
            # 段头形如 ===== / 标题 / =====（允许中间夹空行）
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            k = j + 1
            while k < n and not lines[k].strip():
                k += 1
            if j < n and k < n and SEP_RE.match(lines[k].strip()) \
                    and not SEP_RE.match(lines[j].strip()):
                if cur is not None:
                    blocks.append(cur)
                cur = {"title": lines[j].strip(), "lines": []}
                i = k + 1
                continue
            i += 1
            continue
        if cur is None:
            cur = {"title": None, "lines": []}
        if cur["title"] is None and line.strip():
            cur["title"] = line.strip()
        else:
            cur["lines"].append(line.rstrip())
        i += 1
    if cur is not None:
        blocks.append(cur)
    return [b for b in blocks if b["title"] or any(l.strip() for l in b["lines"])]


def _clean_lines(lines):
    """去掉空行和纯分隔线，保留缩进。"""
    out = []
    for line in lines:
        if not line.strip() or SEP_RE.match(line.strip()):
            continue
        if set(line.strip()) <= {"=", "-", "!"}:
            continue
        out.append(line.rstrip())
    return out


def _find_block(blocks, *keywords):
    for b in blocks:
        title = b.get("title") or ""
        if all(k in title for k in keywords):
            return b
    return None


def extract_theme_lines(block, limit):
    if not block:
        return []
    lines = [l for l in block["lines"] if l.strip()]
    return lines[:limit]


def extract_recs(block, limit):
    """从「短线评分排名」/「长线关注」块里取出前 N 条推荐（名称行 + 评分行 + 板块行）。"""
    if not block:
        return []
    recs, cur = [], None
    for line in block["lines"]:
        m = REC_RE.match(line) or LONG_RE.match(line)
        if m:
            if cur:
                recs.append(cur)
            cur = {"lines": [line.strip()]}
            continue
        if cur is None:
            continue
        stripped = line.strip()
        if not stripped:
            recs.append(cur)
            cur = None
            continue
        # 评分/仓位行、板块行、长线 ROE 行保留；因子明细与龙虎榜留在完整简报里
        if stripped.startswith(("⭐", "🏷️", "💹")):
            cur["lines"].append(stripped)
    if cur:
        recs.append(cur)
    return recs[:limit]


def extract_risks(blocks):
    risks = []
    for b in blocks:
        title = b.get("title") or ""
        if "降级警示" in title:
            risks.append(title)
            for line in _clean_lines(b["lines"]):
                if line.strip() and not set(line.strip()) <= {"!", "-", "="}:
                    risks.append(line.strip())
        elif "停推" in title:
            for line in _clean_lines(b["lines"]):
                if "🚫" in line:
                    risks.append(line.strip())
        elif "影子观察" in title:
            for line in _clean_lines(b["lines"]):
                if "原因" in line:
                    risks.append(line.strip())

    # 短线块内的软提示 / 门槛过滤说明
    short_block = _find_block(blocks, "短线评分排名")
    if short_block:
        for line in short_block["lines"]:
            stripped = line.strip()
            if "策略健康提示" in stripped or stripped.startswith("🚫"):
                risks.append(stripped)

    # 简报尾部追加的数据质量 / 漂移状态
    for b in blocks:
        for line in b["lines"]:
            stripped = line.strip()
            if ("数据质量" in stripped or "绩效漂移" in stripped) and stripped:
                risks.append(stripped)
    # 去重保序
    seen, uniq = set(), []
    for r in risks:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq


def build_digest(text, max_recs=3, max_themes=3):
    blocks = split_blocks(text)
    m = DATE_RE.search(text)
    day = m.group(1) if m else date.today().isoformat()

    lines = [f"📈 A股智能监测 · 每日简报 · {day}", ""]

    overview = _find_block(blocks, "市场概览")
    if overview:
        lines.append("📊 市场概览")
        lines += _clean_lines(overview["lines"])
        lines.append("")

    themes = extract_theme_lines(_find_block(blocks, "题材热度"), max_themes)
    if themes:
        lines.append(f"🔥 题材热度 TOP{len(themes)}")
        lines += themes
        lines.append("")

    rec_block = _find_block(blocks, "短线评分排名") or _find_block(blocks, "长线关注")
    recs = extract_recs(rec_block, max_recs) if rec_block else []
    if recs:
        label = "🎯 长线关注 TOP" if "长线" in (rec_block.get("title") or "") else "🎯 今日推荐 TOP"
        lines.append(f"{label}{len(recs)}")
        for rec in recs:
            lines.append(f"  {rec['lines'][0]}")
            for sub in rec["lines"][1:]:
                lines.append(f"    {sub}")
            lines.append("")
    elif _find_block(blocks, "影子观察") or _find_block(blocks, "停推"):
        lines.append("🎯 今日推荐：无（今日停推，原因见下方风险提示）")
        lines.append("")
    else:
        # 没有推荐条目、也没有停推说明：如实说明，不让微信端静默
        lines.append("🎯 今日推荐")
        lines.append("  （无评分达标标的，详见完整简报）")
        lines.append("")

    risks = extract_risks(blocks)
    if risks:
        lines.append("⚠️ 风险提示")
        lines += [f"  {r}" for r in risks]
        lines.append("")

    lines.append("——完整简报见仓库 reports/ 或 Actions 工件——")
    return "\n".join(lines).strip() + "\n", day, len(recs)


def send_pushplus(token, title, content):
    payload = json.dumps({
        "token": token,
        "title": title,
        "content": content,
        # 简报是带 emoji 的纯文本，txt 模板保留换行，微信端最好读
        "template": "txt",
    }).encode("utf-8")
    req = urllib.request.Request(
        PUSHPLUS_API,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        raise RuntimeError(f"pushplus 请求失败: {e}") from e

    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"pushplus 返回非 JSON: {body[:200]}") from e

    if data.get("code") != 200:
        raise RuntimeError(f"pushplus 返回错误: {body[:300]}")
    return data


def main():
    # Windows 控制台默认 GBK，打印 emoji 会 UnicodeEncodeError（CI 上是 UTF-8，无影响）
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="简报精简摘要 → pushplus 微信推送")
    parser.add_argument("--in", dest="infile", default="reports/daily.md",
                        help="简报文件路径（默认 reports/daily.md）")
    parser.add_argument("--token", default=os.environ.get("PUSHPLUS_TOKEN", ""),
                        help="pushplus token（默认读环境变量 PUSHPLUS_TOKEN）")
    parser.add_argument("--max-recs", type=int, default=3, help="推荐条数上限")
    parser.add_argument("--max-themes", type=int, default=3, help="题材条数上限")
    parser.add_argument("--fallback", default="",
                        help="简报文件缺失时推送的兜底文案；留空则跳过推送")
    parser.add_argument("--dry-run", action="store_true", help="只打印摘要，不发送")
    args = parser.parse_args()

    if os.path.exists(args.infile):
        with open(args.infile, "r", encoding="utf-8") as f:
            text = f.read()
        content, day, n_recs = build_digest(text, args.max_recs, args.max_themes)
        title = f"A股简报 {day}" + (f" · {n_recs}只推荐" if n_recs else " · 今日停推")
    elif args.fallback:
        day = date.today().isoformat()
        content = f"📈 A股智能监测 · {day}\n\n{args.fallback}"
        title = f"A股简报 {day}"
    else:
        print(f"[skip] 简报文件不存在: {args.infile}", file=sys.stderr)
        return 0

    if args.dry_run:
        print(f"标题: {title}\n{'-' * 45}\n{content}")
        return 0

    if not args.token:
        print("[error] 缺少 PUSHPLUS_TOKEN（环境变量或 --token）", file=sys.stderr)
        return 2

    data = send_pushplus(args.token, title, content)
    print(f"[ok] 已推送: {data.get('msg', '')} (code={data.get('code')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
