"""阶段一通过标准（§11）的判定。

口径在看到结果之前定死，写在配置 [evaluation] 里，事后不改：
- 信号组：进场门槛那一档（rules.entry_threshold）的信号事件，处于 §8 不交易条件的不算（exclude_no_trade）；
  对照组：每小时等分几段各抽一个随机时刻、随机方向（种子固定）
- 收益：horizon_s 秒后的同向涨跌幅，起算价用市价单的预计成交价（mkt_fill_px），不用中间价
- 先筛掉不交易条件下的信号，再去重：相邻信号间隔不足 dedupe_minutes 的只保留前一条（和上一条保留下来的比）
- 最低数据量（min_signals 条、min_weeks 周）按筛选、去重之后的条数算，两个都满足才判定
- 三条同时满足才算通过：
  1. 信号组均值 − 对照组均值，自助抽样 bootstrap_reps 次的置信区间下限 > 0
  2. 信号组均值 ≥ 来回手续费（进出都按吃单）；进场吃单、离场挂单的结果另列一行，只作参考
  3. 做多、做空分开算均值都为正；整段数据按时间等分成 segments 段，至少 segments_positive_min 段均值为正
- horizon_s 是唯一判定口径，其它时长照常报告，不参与判定
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

from .bucket import iso_utc
from .config import Config
from .schema import bucket_columns, event_columns
from .storage import day_files, read_csv


def same_dir_return(e: dict, horizon_s: int) -> float | None:
    """horizon_s 秒后相对预计成交价的同向涨跌幅（%）。"""
    entry = e.get("mkt_fill_px")
    px = e.get(f"px_{horizon_s}s")
    if not entry or px is None:
        return None
    return (px / entry - 1) * e["direction"] * 100


def dedupe(events: list[dict], gap_ms: int) -> list[dict]:
    out: list[dict] = []
    last = None
    for e in sorted(events, key=lambda x: x["ts_ms"]):
        if last is None or e["ts_ms"] - last >= gap_ms:
            out.append(e)
            last = e["ts_ms"]
    return out


def mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def quantile(sorted_xs: list[float], q: float) -> float:
    pos = q * (len(sorted_xs) - 1)
    i = int(pos)
    if i + 1 >= len(sorted_xs):
        return sorted_xs[-1]
    return sorted_xs[i] + (sorted_xs[i + 1] - sorted_xs[i]) * (pos - i)


def bootstrap_diff_ci(sig: list[float], ctrl: list[float], reps: int, conf: float,
                      seed: str) -> tuple[float, float]:
    """两组各自有放回重抽，算均值差的百分位置信区间。"""
    rng = random.Random(seed)
    ns, nc = len(sig), len(ctrl)
    diffs = []
    for _ in range(reps):
        diffs.append(sum(rng.choices(sig, k=ns)) / ns - sum(rng.choices(ctrl, k=nc)) / nc)
    diffs.sort()
    a = (1 - conf) / 2
    return quantile(diffs, a), quantile(diffs, 1 - a)


@dataclass
class Check:
    name: str
    rule: str
    result: str
    ok: bool | None  # None = 算不出来


def _pct(x: float | None, nd: int = 4) -> str:
    return "-" if x is None else f"{x:.{nd}f}%"


def evaluate(cfg: Config, data: Path, days: list[str] | None = None) -> tuple[str, bool | None]:
    """返回 (Markdown 报告, 结论)。结论 None 表示数据量不够、不做判定。"""
    ev_cfg, fees = cfg.evaluation, cfg.fees
    h = ev_cfg.horizon_s
    ev_dir, b_dir = data / "events", data / "buckets"
    if days is None:
        days = sorted({p.name[:10] for d in (ev_dir, b_dir) for p in d.glob("*.csv")})
    events = list(read_csv(day_files(ev_dir, days, ".csv"), dict(event_columns(cfg))))
    buckets = list(read_csv(day_files(b_dir, days, ".csv"), dict(bucket_columns(cfg))))

    tier = cfg.rules.entry_threshold
    gap = int(ev_cfg.dedupe_minutes * 60_000)
    sig_tier = [e for e in events if e["kind"] == "signal" and e["tier"] == tier]
    ctrl_all = [e for e in events if e["kind"] == "control"]
    nt_sig = [e for e in sig_tier if e["no_trade"]]
    # 先筛掉不交易条件下的信号，再去重
    pool = [e for e in sig_tier if not e["no_trade"]] if ev_cfg.exclude_no_trade else sig_tier
    sig_d = dedupe(pool, gap)

    def usable(es):
        return [e for e in es if same_dir_return(e, h) is not None]

    sig, ctrl = usable(sig_d), usable(ctrl_all)
    rs = [same_dir_return(e, h) for e in sig]
    rc = [same_dir_return(e, h) for e in ctrl]

    t0 = t1 = None
    span_days = 0.0
    if buckets:
        starts = [b["start_ms"] for b in buckets]
        span_days = (max(starts) - min(starts)) / 86_400_000
        # 分段用的「整段数据」从第一个有效分数开始：预热期不可能有信号，算进去会让第一段系统性偏少
        valid = [b["start_ms"] for b in buckets if b["score_valid"]]
        if valid:
            t0, t1 = min(valid), max(starts) + cfg.bucket.width_s * 1000
    enough = len(rs) >= ev_cfg.min_signals and span_days >= ev_cfg.min_weeks * 7 and len(rc) >= 2

    fee_rt = 2 * fees.taker_rate * 100
    fee_ref = (fees.taker_rate + fees.maker_rate) * 100
    ms, mc = mean(rs), mean(rc)
    checks: list[Check] = []

    # 1. 统计上有差别
    if len(rs) >= 2 and len(rc) >= 2:
        lo, hi = bootstrap_diff_ci(rs, rc, ev_cfg.bootstrap_reps, ev_cfg.confidence, ev_cfg.bootstrap_seed)
        checks.append(Check("1 统计上有差别",
                            f"信号均值 − 对照均值的 {ev_cfg.confidence:.0%} 自助区间下限 > 0",
                            f"差 {_pct(ms - mc)}，区间 [{_pct(lo)}, {_pct(hi)}]", lo > 0))
    else:
        checks.append(Check("1 统计上有差别", "区间下限 > 0", "样本不足", None))

    # 2. 够付手续费
    checks.append(Check("2 够付手续费", f"信号均值 ≥ {fee_rt:.2f}%（进出都按吃单）", _pct(ms),
                        None if ms is None else ms >= fee_rt))

    # 3. 结果稳定：多空分开 + 按时间等分
    longs = [r for r, e in zip(rs, sig) if e["direction"] > 0]
    shorts = [r for r, e in zip(rs, sig) if e["direction"] < 0]
    ml, msh = mean(longs), mean(shorts)
    checks.append(Check("3a 多空分开", "做多、做空均值都 > 0",
                        f"多 {_pct(ml)}（{len(longs)} 条），空 {_pct(msh)}（{len(shorts)} 条）",
                        None if ml is None or msh is None else (ml > 0 and msh > 0)))
    n_seg = ev_cfg.segments
    segs: list[list[float]] = [[] for _ in range(n_seg)]
    if t0 is not None and t1 > t0:
        for r, e in zip(rs, sig):
            k = (e["ts_ms"] - t0) * n_seg // (t1 - t0)
            segs[min(max(k, 0), n_seg - 1)].append(r)
    # 没有信号的段算不上「为正」
    pos = sum(1 for v in segs if v and mean(v) > 0)
    checks.append(Check("3b 按时间等分", f"整段数据等分 {n_seg} 段，至少 {ev_cfg.segments_positive_min} 段均值 > 0",
                        f"{pos} / {n_seg} 段为正", None if not rs else pos >= ev_cfg.segments_positive_min))

    if not enough:
        verdict = None
    elif any(c.ok is None for c in checks):
        verdict = None
    else:
        verdict = all(c.ok for c in checks)

    def yn(ok):
        return "—" if ok is None else ("通过" if ok else "不通过")

    filt = "，不交易条件下的不进判定" if ev_cfg.exclude_no_trade else ""
    lines = [
        f"# 阶段一判定（§11）：{days[0] if days else '-'} → {days[-1] if days else '-'}",
        "",
        f"判定口径：信号后 {h} 秒的同向涨跌幅，起算价为市价单预计成交价；信号取 {tier:g} 档{filt}，"
        f"相邻不足 {ev_cfg.dedupe_minutes:g} 分钟只留前一条。",
        "",
        "| 数据 | 数量 |",
        "| --- | --- |",
        f"| 覆盖天数 | {span_days:.1f} 天（要求 ≥ {ev_cfg.min_weeks * 7:g} 天） |",
        f"| {tier:g} 档信号 | {len(sig_tier)} 条，其中处于不交易条件 {len(nt_sig)} 条"
        + ("（不进判定）" if ev_cfg.exclude_no_trade else "（照常计入）") + " |",
        f"| 筛选、去重后 | {len(sig_d)} 条，缺 {h} 秒价格或预计成交价的 {len(sig_d) - len(sig)} 条不计 |",
        f"| 参与判定的信号 | {len(rs)} 条（要求 ≥ {ev_cfg.min_signals}） |",
        f"| 参与判定的对照组 | {len(rc)} 条 |",
        f"| 信号组均值 | {_pct(ms)} |",
        f"| 对照组均值 | {_pct(mc)} |",
        "",
        "| 判定项 | 标准 | 结果 | 结论 |",
        "| --- | --- | --- | --- |",
    ]
    lines += [f"| {c.name} | {c.rule} | {c.result} | {yn(c.ok)} |" for c in checks]
    concl = ("数据量不够，不做判定" if not enough else
             "有一项算不出来，不做判定" if verdict is None else
             "**通过**" if verdict else "**不通过**")
    lines += [
        f"| **结论** | 三条同时满足 | {concl} | |",
        "",
        "## 参考（不参与判定）",
        "",
        f"进场吃单、离场挂单的来回手续费 {fee_ref:.2f}%：信号均值"
        f"{'不低于' if ms is not None and ms >= fee_ref else '低于'}它（{_pct(ms)}）。",
        "",
        f"不交易条件的过滤作用（各组各自去重，{h} 秒同向涨跌幅）：",
        "",
        "| 组 | 条数 | 均值 | 与对照组的差 |",
        "| --- | --- | --- | --- |",
    ]

    def group_row(name: str, es: list[dict]) -> str:
        r = [same_dir_return(e, h) for e in dedupe(es, gap)]
        r = [x for x in r if x is not None]
        m = mean(r)
        diff = None if m is None or mc is None else m - mc
        return f"| {name} | {len(r)} | {_pct(m)} | {_pct(diff)} |"

    judged = "参与判定（不交易条件之外）" if ev_cfg.exclude_no_trade else "参与判定（全部信号）"
    lines.append(group_row(judged, pool))
    lines.append(group_row(f"全部 {tier:g} 档信号", sig_tier))
    lines.append(group_row("处于不交易条件的", nt_sig))
    for key, label in (("low_vol", "低波动"), ("flips", "分数翻转"), ("calendar", "经济数据"), ("data", "数据不完整")):
        grp = [e for e in nt_sig if any(x.split(":")[0] == key for x in (e["no_trade_reason"] or "").split("|"))]
        if grp:
            lines.append(group_row(f"　其中 {label}", grp))

    lines += [
        "",
        "其它时长（只报告，不能事后换成判定口径）：",
        "",
        "| 时长 | 信号条数 | 信号均值 | 对照条数 | 对照均值 | 差 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for hz in cfg.events.price_horizons_s:
        a = [same_dir_return(e, hz) for e in sig_d]
        a = [x for x in a if x is not None]
        b = [same_dir_return(e, hz) for e in ctrl_all]
        b = [x for x in b if x is not None]
        ma, mb = mean(a), mean(b)
        diff = None if ma is None or mb is None else ma - mb
        mark = "（判定口径）" if hz == h else ""
        lines.append(f"| {hz} 秒{mark} | {len(a)} | {_pct(ma)} | {len(b)} | {_pct(mb)} | {_pct(diff)} |")
    if t0 is not None and t1 > t0:
        lines += ["", f"按时间等分 {n_seg} 段（从第一个有效分数 {iso_utc(t0)[:16]} 起）：", "", "| 段 | 时间（UTC） | 信号条数 | 均值 |", "| --- | --- | --- | --- |"]
        for k, v in enumerate(segs):
            a = t0 + (t1 - t0) * k // n_seg
            b = t0 + (t1 - t0) * (k + 1) // n_seg
            lines.append(f"| {k + 1} | {iso_utc(a)[:16]} → {iso_utc(b)[:16]} | {len(v)} | "
                         f"{_pct(mean(v)) if v else '无信号'} |")
    return "\n".join(lines) + "\n", verdict
