"""每日简报（§12）：运行时长、不完整桶比例、信号次数、平均延迟。"""
from __future__ import annotations

import statistics
from collections import Counter

from .config import Config
from .schema import bucket_columns, event_columns
from .storage import day_files, read_csv


def build_daily(cfg: Config, day: str) -> tuple[str, str] | None:
    """返回 (Markdown 正文, 推送用的一行摘要)；那天没有数据返回 None。"""
    d = cfg.data_dir
    btypes = dict(bucket_columns(cfg))
    rows = list(read_csv(day_files(d / "buckets", [day], ".csv"), btypes))
    if not rows:
        return None
    etypes = dict(event_columns(cfg))
    events = list(read_csv(day_files(d / "events", [day], ".csv"), etypes))

    width = rows[0]["width_s"] or cfg.bucket.width_s
    expected = 86_400 // width
    n = len(rows)
    incomplete = [r for r in rows if not r["complete"]]
    reasons = Counter()
    for r in incomplete:
        for x in (r["incomplete_reason"] or "").split("|"):
            if x:
                reasons[x] += 1
    valid = sum(1 for r in rows if r["score_valid"])
    lats = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
    sig = [e for e in events if e["kind"] == "signal"]
    ctrl = [e for e in events if e["kind"] == "control"]
    by_tier = Counter((e["tier"], e["direction"]) for e in sig)
    no_trade = sum(1 for r in rows if r["no_trade"])

    hours = n * width / 3600
    inc_pct = len(incomplete) / n * 100
    lines = [
        f"# 监控器日报 {day}（UTC）",
        "",
        "| 项目 | 数值 |",
        "| --- | --- |",
        f"| 运行时长 | {hours:.2f} 小时（记录 {n} / {expected} 个桶） |",
        f"| 不完整桶 | {len(incomplete)} 个，{inc_pct:.2f}% |",
        f"| 分数有效的桶 | {valid} 个，{valid / n * 100:.2f}% |",
        f"| 信号事件 | {len(sig)} 条 |",
        f"| 对照组 | {len(ctrl)} 条 |",
        f"| 处于不交易条件的桶 | {no_trade} 个，{no_trade / n * 100:.2f}% |",
    ]
    if lats:
        lines.append(f"| 数据延迟 | 平均 {statistics.fmean(lats):.1f} ms，"
                     f"中位 {statistics.median(lats):.1f} ms，最大 {max(lats):.1f} ms |")
    else:
        lines.append("| 数据延迟 | 无 |")
    if reasons:
        lines += ["", "不完整原因（一个桶可能有多个）：", ""]
        lines += [f"- {k}：{v}" for k, v in reasons.most_common()]
    if by_tier:
        lines += ["", "| 档位 | 多 | 空 |", "| --- | --- | --- |"]
        for t in sorted({t for t, _ in by_tier}):
            lines.append(f"| {t:g} | {by_tier[(t, 1)]} | {by_tier[(t, -1)]} |")
    lines.append("")
    fm = f"{cfg.events.followup_minutes:g}"
    lines.append(f"信号事件跟踪满 {fm} 分钟才写入。实时运行时，日报在次日 0 点（UTC）再过 {fm} 分钟后生成，"
                 f"所以当天最后 {fm} 分钟的信号也算在内。")
    summary = (f"{day} 运行 {hours:.1f}h，不完整 {inc_pct:.1f}%，信号 {len(sig)} 条，"
               f"平均延迟 {statistics.fmean(lats):.0f}ms" if lats else
               f"{day} 运行 {hours:.1f}h，不完整 {inc_pct:.1f}%，信号 {len(sig)} 条")
    return "\n".join(lines) + "\n", summary


def write_daily(cfg: Config, day: str) -> str | None:
    out = build_daily(cfg, day)
    if out is None:
        return None
    text, summary = out
    p = cfg.data_dir / "reports" / f"{day}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return summary
