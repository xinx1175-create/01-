"""每日简报（§12）：运行时长、不完整桶比例、信号次数、平均延迟、当天数据占用的磁盘空间。"""
from __future__ import annotations

import shutil
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .bucket import DOWNTIME
from .config import Config
from .schema import bucket_columns, event_columns
from .storage import day_files, read_csv


# 当天数据按类别统计：(名称, data 下的子目录)。文件名都以日期开头
DAY_FILES = [("逐笔成交", "raw/trades"), ("盘口快照", "raw/books"), ("持仓量等原文", "raw/misc"),
             ("桶表", "buckets"), ("事件", "events")]
LOG_NAME = "flowmon.log"


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _size(paths) -> int:
    total = 0
    for p in paths:
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def day_disk_usage(cfg: Config, day: str) -> list[tuple[str, int]]:
    """某天（UTC）的数据文件占用的磁盘空间，按类别列出，最后一项是日志。"""
    d = cfg.data_dir
    out = [(name, _size((d / sub).glob(f"{day}*"))) for name, sub in DAY_FILES]
    # 日志按 UTC 零点切分：切分后的旧文件叫 flowmon.log.<日期>；当天还没切分时就是 flowmon.log 本身
    logs = [cfg.log_dir / f"{LOG_NAME}.{day}"]
    if day == datetime.now(timezone.utc).date().isoformat():
        logs.append(cfg.log_dir / LOG_NAME)
    out.append(("日志", _size(logs)))
    return out


def dir_size(path: Path) -> int:
    return _size(p for p in path.rglob("*") if p.is_file()) if path.exists() else 0


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
    # 停机占位桶（downtime）是重启后补写的，那段时间监控器没在运行，不算运行时长
    n_down = sum(1 for r in rows if r["incomplete_reason"] == DOWNTIME)
    n_run = n - n_down
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

    hours = n_run * width / 3600
    inc_pct = len(incomplete) / n * 100
    usage = day_disk_usage(cfg, day)
    used = sum(b for _, b in usage)
    lines = [
        f"# 监控器日报 {day}（UTC）",
        "",
        "| 项目 | 数值 |",
        "| --- | --- |",
        f"| 运行时长 | {hours:.2f} 小时（记录 {n_run} / {expected} 个桶） |",
    ]
    if n_down:
        lines.append(f"| 停机 | {n_down * width / 3600:.2f} 小时（{n_down} 个占位桶，标为 downtime） |")
    lines += [
        f"| 不完整桶 | {len(incomplete)} 个，{inc_pct:.2f}%（含停机） |",
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
    lines.append(f"| 当天数据占用磁盘 | {fmt_bytes(used)}（"
                 + "，".join(f"{name} {fmt_bytes(b)}" for name, b in usage) + "） |")
    try:
        free = shutil.disk_usage(cfg.data_dir).free
        lines.append(f"| 数据目录合计 / 磁盘剩余 | {fmt_bytes(dir_size(cfg.data_dir))} / {fmt_bytes(free)} |")
    except OSError:
        pass
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
               + (f"平均延迟 {statistics.fmean(lats):.0f}ms，" if lats else "")
               + f"当天数据 {fmt_bytes(used)}")
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
