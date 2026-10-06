"""阶段二回测：规格第 7 节和资金流带两套仓位管理，跑同一批进场信号，比较哪套更好。

防止过拟合的三道关：
1. 训练段 / 测试段：数据按时间切开，参数只在前面的训练段里挑，成绩看后面的测试段。
2. 打乱时间对照：把信号（S、资金流带读数）整体错开一个随机时间（至少一天）再跑，信号和价格的对应关系被打乱，
   剩下的只是运气。实际成绩要明显好过这些打乱的结果（p < 0.05），才说明不是运气。
3. 参数稳不稳：资金流带的几组参数，在训练段的排名和在测试段的排名对不对得上。对不上（相关接近 0）
   说明挑参数就是在挑噪声，挑出来的「最好」到了新数据上不会更好。

参数单独放在 strategy.toml 里，不影响正在运行的监控器。
"""
from __future__ import annotations

import dataclasses
import random
import statistics
import tomllib
import typing
from dataclasses import dataclass
from itertools import product
from pathlib import Path

from .band import BandCfg
from .bucket import iso_utc
from .config import Config, ConfigError, _build
from .positions import (MAKER, TAKER, Bar, BandPolicy, BandPositionCfg, SimResult, Simulator, Spec7Policy,
                        bars_from_rows, summarize)
from .schema import bucket_columns
from .storage import day_files, read_csv


@dataclass(frozen=True)
class BacktestCfg:
    maker_wait_buckets: int
    train_fraction: float
    null_runs: int
    null_min_shift_hours: float
    seed: str
    grid_outer: list[float]
    grid_stop_noise_mult: list[float]
    grid_exit_fade_buckets: list[int]


@dataclass(frozen=True)
class StrategyCfg:
    band: BandCfg
    band_position: BandPositionCfg
    backtest: BacktestCfg


def load_strategy(path: str | Path) -> StrategyCfg:
    p = Path(path).resolve()
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"找不到策略配置 {p}。先复制一份：cp strategy.example.toml {p}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"策略配置格式错误：{e}") from None
    hints = typing.get_type_hints(StrategyCfg)
    sections = [f.name for f in dataclasses.fields(StrategyCfg)]
    missing = [s for s in sections if s not in raw]
    extra = [s for s in raw if s not in sections]
    if missing or extra:
        raise ConfigError(f"策略配置段落不对：缺少 {missing}，不认识 {extra}")
    s = StrategyCfg(**{k: _build(k, hints[k], raw[k]) for k in sections})
    b, bp, bt = s.band, s.band_position, s.backtest
    checks = [
        (b.window_buckets >= 1, "band.window_buckets 至少 1"),
        (0 < b.inner < b.outer, "band 要求 0 < inner < outer"),
        (b.noise_window_minutes > 0 and b.noise_horizon_minutes > 0, "band 的噪声窗口必须大于 0"),
        (0 < b.noise_min_coverage <= 1, "band.noise_min_coverage 应在 (0, 1]"),
        (bp.stop_noise_mult > 0 and bp.min_stop_pct > 0, "band_position 的止损参数必须大于 0"),
        (bp.fade_buckets >= 1 and bp.exit_fade_buckets >= 1, "band_position 的桶数至少 1"),
        (bt.maker_wait_buckets >= 1, "backtest.maker_wait_buckets 至少 1"),
        (0 < bt.train_fraction < 1, "backtest.train_fraction 应在 (0, 1)"),
        (bt.null_runs >= 0, "backtest.null_runs 不能为负"),
        (all(x > b.inner for x in bt.grid_outer), "backtest.grid_outer 都要大于 band.inner"),
    ]
    for ok, msg in checks:
        if not ok:
            raise ConfigError(msg)
    return s


# ---------- 数据 ----------

def load_rows(cfg: Config, data: Path, days: list[str] | None = None) -> list[dict]:
    bdir = data / "buckets"
    if days is None:
        days = sorted({p.name[:10] for p in bdir.glob("*.csv")})
    rows = sorted(read_csv(day_files(bdir, days, ".csv"), dict(bucket_columns(cfg))), key=lambda r: r["start_ms"])
    out, last = [], None
    for r in rows:
        if r["width_s"] == cfg.bucket.width_s and (last is None or r["start_ms"] > last):
            out.append(r)
            last = r["start_ms"]
    return out


def split(bars: list[Bar], frac: float) -> tuple[list[Bar], list[Bar]]:
    """按时间切：前 frac 的时间是训练段。"""
    if not bars:
        return [], []
    t0, t1 = bars[0].t, bars[-1].t
    cut = t0 + (t1 - t0) * frac
    return [b for b in bars if b.t < cut], [b for b in bars if b.t >= cut]


def shifted(bars: list[Bar], k: int) -> list[Bar]:
    """把信号（S、分数有效、不交易条件、资金流带读数）整体往后错开 k 个桶（循环），价格不动。"""
    n = len(bars)
    out = []
    for i, b in enumerate(bars):
        s = bars[(i - k) % n]
        out.append(dataclasses.replace(b, valid=s.valid, S=s.S, no_trade=s.no_trade, z=s.z))
    return out


# ---------- 跑 ----------

def policies(cfg: Config, st: StrategyCfg, band_position: BandPositionCfg | None = None,
             band: BandCfg | None = None) -> list:
    return [Spec7Policy(cfg), BandPolicy(cfg, band or st.band, band_position or st.band_position)]


def run(cfg: Config, st: StrategyCfg, bars: list[Bar], policy, order: str) -> SimResult:
    return Simulator(cfg, policy, order, st.backtest.maker_wait_buckets).run(bars)


def null_test(cfg: Config, st: StrategyCfg, bars: list[Bar], policy, order: str) -> tuple[float, list[float], float]:
    """返回 (实际净盈亏, 打乱后的净盈亏列表, p 值)。"""
    bt = st.backtest
    actual = summarize(run(cfg, st, bars, policy, order))["pnl"]
    w = cfg.bucket.width_s * 1000
    min_k = int(bt.null_min_shift_hours * 3_600_000 / w)
    n = len(bars)
    if bt.null_runs == 0 or n <= 2 * min_k:
        return actual, [], float("nan")
    rng = random.Random(f"{bt.seed}:{policy.name}:{order}")
    dist = []
    for _ in range(bt.null_runs):
        k = rng.randrange(min_k, n - min_k)
        dist.append(summarize(run(cfg, st, shifted(bars, k), policy, order))["pnl"])
    p = (1 + sum(1 for x in dist if x >= actual)) / (1 + len(dist))
    return actual, dist, p


def _rank(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2
        i = j + 1
    return ranks


def spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3:
        return None
    ra, rb = _rank(a), _rank(b)
    ma, mb = statistics.fmean(ra), statistics.fmean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return num / den if den else None


def fit_band(cfg: Config, st: StrategyCfg, bars: list[Bar], frac: float, order: str) -> list[dict]:
    """资金流带的几组参数：各自在训练段、测试段的成绩。读数本身和外带倍数无关，只有仓位规则用到外带。"""
    bt = st.backtest
    out = []
    tr, te = split(bars, frac)
    for outer, mult, fade in product(bt.grid_outer, bt.grid_stop_noise_mult, bt.grid_exit_fade_buckets):
        band = dataclasses.replace(st.band, outer=outer)
        pc = dataclasses.replace(st.band_position, stop_noise_mult=mult, exit_fade_buckets=fade)
        pol = BandPolicy(cfg, band, pc)
        out.append({"outer": outer, "mult": mult, "fade": fade,
                    "train": summarize(run(cfg, st, tr, pol, order)),
                    "test": summarize(run(cfg, st, te, pol, order))})
    return out


# ---------- 报告 ----------

def _n(x, nd=2, pct=False) -> str:
    if x is None:
        return "-"
    return f"{x * 100:.{nd}f}%" if pct else f"{x:.{nd}f}"


def _row(name: str, order: str, s: dict) -> str:
    hold = f"{s['avg_hold_s'] / 60:.1f} 分钟" if s["avg_hold_s"] is not None else "-"
    return (f"| {name} | {'挂单' if order == MAKER else '吃单'} | {s['trades']} | {_n(s['win_rate'], 1, True)} | "
            f"{_n(s['avg_win'])} | {_n(s['avg_loss'])} | {_n(s['profit_factor'])} | {_n(s['pnl'])} | "
            f"{_n(s['fees'])} | {_n(s['pnl_per_trade'], 3)} | {_n(s['max_drawdown'])} | {hold} |")


HEADER = ["| 仓位管理 | 进场 | 交易 | 胜率 | 平均赚 | 平均亏 | 盈亏比 | 净盈亏 | 手续费 | 每笔 | 最大回撤 | 平均持仓 |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]


def report(cfg: Config, st: StrategyCfg, rows: list[dict], with_null: bool = True,
           with_fit: bool = True) -> str:
    bt = st.backtest
    bars = bars_from_rows(rows, cfg, st.band)
    lines = ["# 阶段二回测：规格第 7 节 vs 资金流带", ""]
    if not bars:
        return "\n".join(lines + ["没有数据。"]) + "\n"
    n = len(bars)
    lines += [
        f"数据：{iso_utc(bars[0].t)[:16]} → {iso_utc(bars[-1].end)[:16]}（UTC），{n} 个桶，"
        f"完整 {sum(b.complete for b in bars) / n:.1%}，分数有效 {sum(b.valid for b in bars) / n:.1%}，"
        f"资金流带有读数 {sum(b.z is not None for b in bars) / n:.1%}。",
        f"金额单位 USDT；本金 {cfg.risk.capital_usdt:g}，满仓保证金 {cfg.rules.full_margin_usdt:g} × "
        f"{cfg.rules.leverage:g} 倍；手续费吃单 {cfg.fees.taker_rate * 100:g}%、挂单 {cfg.fees.maker_rate * 100:g}%。",
        "",
        "**这是模拟成交，不是实盘**：买在卖一、卖在买一，止损按止损价成交再让半个价差。"
        "交易少于 100 笔时，下面的差别大多是噪声。",
        "",
        "## 全部数据", "",
    ] + HEADER
    results = {}
    for pol in policies(cfg, st):
        for order in (TAKER, MAKER):
            r = run(cfg, st, bars, pol, order)
            s = summarize(r)
            results[(pol.name, order)] = s
            lines.append(_row(pol.name, order, s))
    lines += ["", "其它情况：", ""]
    for (name, order), s in results.items():
        reasons = "，".join(f"{k} {v}" for k, v in sorted(s["reasons"].items(), key=lambda x: -x[1])) or "无"
        extra = [f"离场原因：{reasons}", f"空仓时的进场信号 {s['signals']} 条，被冷却 / 暂停 / 亏损上限挡住 {s['blocked']} 条"]
        if order == MAKER:
            extra.append(f"挂单没成交放弃 {s['missed_maker']} 条")
        if s["gap_trades"]:
            extra.append(f"经过数据缺口的交易 {s['gap_trades']} 笔（缺口里没法判断止损，结果可能偏好）")
        if s["halted"]:
            extra.append(f"**触发彻底停止**：{s['halted']}")
        lines.append(f"- {name}、{'挂单' if order == MAKER else '吃单'}进场：" + "；".join(extra))

    tr, te = split(bars, bt.train_fraction)
    if tr and te:
        lines += ["", f"## 训练段 / 测试段（参数不变，按时间切：前 {bt.train_fraction:.0%} / 后 {1 - bt.train_fraction:.0%}）",
                  "", f"训练段 {iso_utc(tr[0].t)[:16]} → {iso_utc(tr[-1].end)[:16]}；"
                      f"测试段 {iso_utc(te[0].t)[:16]} → {iso_utc(te[-1].end)[:16]}。", ""]
        lines += ["| 段 " + HEADER[0], "| --- " + HEADER[1]]
        for seg, bs in (("训练", tr), ("测试", te)):
            for pol in policies(cfg, st):
                for order in (TAKER, MAKER):
                    lines.append(f"| {seg} " + _row(pol.name, order, summarize(run(cfg, st, bs, pol, order))))

    if with_null:
        lines += ["", "## 打乱时间对照", "",
                  f"把信号（S、资金流带读数）整体错开一个随机时间（至少 {bt.null_min_shift_hours:g} 小时）重跑 "
                  f"{bt.null_runs} 次。信号和价格的对应关系被打乱，剩下的只是运气。"
                  "p 值 = 打乱后的成绩不比实际差的比例；**p < 0.05 才说明盈亏不是运气**。", "",
                  "| 仓位管理 | 进场 | 实际净盈亏 | 打乱后中位数 | 打乱后最好的 10% | p 值 |", "| --- | --- | --- | --- | --- | --- |"]
        for pol in policies(cfg, st):
            for order in (TAKER, MAKER):
                actual, dist, p = null_test(cfg, st, bars, pol, order)
                if dist:
                    q90 = sorted(dist)[int(0.9 * (len(dist) - 1))]
                    lines.append(f"| {pol.name} | {'挂单' if order == MAKER else '吃单'} | {actual:.2f} | "
                                 f"{statistics.median(dist):.2f} | {q90:.2f} | {p:.3f} |")
                else:
                    lines.append(f"| {pol.name} | {'挂单' if order == MAKER else '吃单'} | {actual:.2f} | - | - | "
                                 f"数据不到 {2 * bt.null_min_shift_hours:g} 小时，做不了 |")

    if with_fit and tr and te:
        grid = fit_band(cfg, st, bars, bt.train_fraction, TAKER)
        best = max(grid, key=lambda g: g["train"]["pnl"])
        corr = spearman([g["train"]["pnl"] for g in grid], [g["test"]["pnl"] for g in grid])
        med_test = statistics.median(g["test"]["pnl"] for g in grid)
        lines += ["", "## 资金流带参数：只在训练段挑", "",
                  f"外带倍数 {bt.grid_outer} × 止损倍数 {bt.grid_stop_noise_mult} × 平仓确认桶数 {bt.grid_exit_fade_buckets}，"
                  f"共 {len(grid)} 组，吃单进场。", "",
                  f"- 训练段最好的一组：外带 {best['outer']:g}σ、止损 {best['mult']:g} 倍噪声、平仓确认 {best['fade']} 个桶；"
                  f"训练段净盈亏 {best['train']['pnl']:.2f}（{best['train']['trades']} 笔）",
                  f"- 这一组在测试段：净盈亏 {best['test']['pnl']:.2f}（{best['test']['trades']} 笔）；"
                  f"所有组在测试段的中位数 {med_test:.2f}",
                  f"- 训练段排名和测试段排名的相关系数：{_n(corr)}。接近 0 或为负，说明挑参数是在挑噪声（过拟合），"
                  "训练段的「最好」到了新数据上不会更好；明显为正（比如 > 0.5）且交易够多，参数才有参考价值。",
                  "", "| 外带 | 止损倍数 | 平仓确认 | 训练段净盈亏 | 训练段笔数 | 测试段净盈亏 | 测试段笔数 |",
                  "| --- | --- | --- | --- | --- | --- | --- |"]
        for g in sorted(grid, key=lambda g: -g["train"]["pnl"]):
            lines.append(f"| {g['outer']:g} | {g['mult']:g} | {g['fade']} | {g['train']['pnl']:.2f} | "
                         f"{g['train']['trades']} | {g['test']['pnl']:.2f} | {g['test']['trades']} |")
    return "\n".join(lines) + "\n"
