"""阶段二模拟成交：成交价和手续费、止损、时间止损、加减仓、风控表、挂单进场；资金流带；回测报告和命令行。"""
import math
import random

import pytest

from conftest import ROOT, make_cfg
from flowmon import __main__ as cli
from flowmon.backtest import load_strategy, null_test, report, shifted, spearman, split
from flowmon.band import BandCfg, FlowBand
from flowmon.positions import MAKER, TAKER, Bar, BandPolicy, Simulator, Spec7Policy, summarize
from flowmon.schema import bucket_columns
from flowmon.storage import DailyCsv
from synthmarket import make_rows

W = 15_000
T0 = 1_790_640_000_000  # 2026-09-29T00:00:00Z


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


@pytest.fixture
def st():
    return load_strategy(ROOT / "strategy.example.toml")


def bar(i, S=None, px=60000.0, valid=True, z=None, noise=0.0015, lo=None, hi=None, op=None, bid=None, ask=None,
        no_trade=False, complete=True):
    """第 i 个桶；默认价差 0.2（买一 = px − 0.1，卖一 = px + 0.1），桶内高低等于收盘。"""
    return Bar(t=T0 + i * W, end=T0 + (i + 1) * W, complete=complete, valid=valid, S=S, no_trade=no_trade,
               open=px if op is None else op, high=px if hi is None else hi, low=px if lo is None else lo,
               close=px, bid=px - 0.1 if bid is None else bid, ask=px + 0.1 if ask is None else ask, z=z, noise=noise)


def run(cfg, bars, policy=None, order=TAKER, wait=1):
    return Simulator(cfg, policy or Spec7Policy(cfg), order, wait).run(bars)


# ---------- 规格第 7 节 ----------

def test_spec7_entry_add_halve_exit_arithmetic(cfg):
    bars = [bar(0, S=30), bar(1, S=45), bar(2, S=50, px=60030), bar(3, S=65, px=60060),
            bar(4, S=50, px=60090), bar(5, S=30, px=60060), bar(6, S=10, px=60120)]
    r = run(cfg, bars)
    (t,) = r.trades
    taker = cfg.fees.taker_rate
    q1 = 600 / 60000.1                       # 一档 600 USDT，在卖一买
    q2 = 960 / 60060                          # 二档 960 USDT（按收盘算目标数量）
    add = q2 - q1                             # 在卖一 60060.1 加
    avg = (60000.1 * q1 + 60060.1 * add) / q2
    half = q2 / 2                             # 跌破 35 在买一 60059.9 减半
    pnl = (60059.9 - avg) * half + (60119.9 - avg) * half
    fees = taker * (q1 * 60000.1 + add * 60060.1 + half * 60059.9 + half * 60119.9)
    assert t.d == 1 and t.max_level == 2 and t.reason == "S 跌破清仓线"
    assert t.fees == pytest.approx(fees)
    assert t.pnl == pytest.approx(pnl - fees)
    assert "S 到加仓线（2 档）" in t.actions and "S 跌破减半线" in t.actions


def test_spec7_stop_loses_tier_budget(cfg):
    # 一档止损：整笔亏 3 USDT 的价位 = 均价 − 3 / 数量 = 60000.1 × (1 − 0.5%)
    stop = 60000.1 - 3 / (600 / 60000.1)
    bars = [bar(0, S=30), bar(1, S=45), bar(2, S=45, px=59900, lo=stop - 1)]
    (t,) = run(cfg, bars).trades
    q = 600 / 60000.1
    px = stop - 0.1                            # 止损价再让半个价差
    assert t.reason == "止损" and t.exit_px == pytest.approx(px)
    assert t.pnl == pytest.approx((px - 60000.1) * q - cfg.fees.taker_rate * q * (60000.1 + px))


def test_stop_gap_through_fills_at_open(cfg):
    bars = [bar(0, S=30), bar(1, S=45), bar(2, valid=False, complete=False, lo=None, hi=None, op=None),
            bar(3, S=45, px=59000, op=59100, lo=58900)]
    bars[2] = Bar(t=bars[2].t, end=bars[2].end, complete=False, valid=False, S=None, no_trade=True, open=None,
                  high=None, low=None, close=None, bid=None, ask=None, z=None, noise=None)
    (t,) = run(cfg, bars).trades
    assert t.reason == "止损" and t.exit_px == pytest.approx(59100 - 0.1) and t.gap


def test_short_side_mirror(cfg):
    bars = [bar(0, S=-30), bar(1, S=-45), bar(2, S=-50, px=59900), bar(3, S=-10, px=59800)]
    (t,) = run(cfg, bars).trades
    q = 600 / 59999.9                          # 空在买一卖出
    assert t.d == -1 and t.entry_px == 59999.9 and t.exit_px == pytest.approx(59800.1)
    assert t.pnl == pytest.approx((59999.9 - 59800.1) * q - cfg.fees.taker_rate * q * (59999.9 + 59800.1))


def test_time_stop(cfg):
    bars = [bar(0, S=30), bar(1, S=45)] + [bar(i, S=45, px=60010) for i in range(2, 60)]
    (t,) = run(cfg, bars).trades
    assert t.reason == "时间止损" and t.hold_s == pytest.approx(40 * 15)  # 开仓 10 分钟后、浮盈 < 0.1%


def test_cooldown_and_loss_streak_pause(cfg):
    def losing_trade(i0):
        # 进场后马上清仓：亏手续费和价差
        return [bar(i0, S=30), bar(i0 + 1, S=45), bar(i0 + 2, S=10)]
    bars = losing_trade(0) + losing_trade(3)        # 第二次信号在亏损后 15 秒：冷却 5 分钟挡住
    bars += [bar(i, S=0) for i in range(6, 40)]
    bars += losing_trade(40)                         # 10 分钟后：冷却已过，进场，亏
    bars += [bar(i, S=0) for i in range(43, 70)]
    bars += losing_trade(70)                         # 连亏 2 次后暂停 30 分钟：挡住
    r = run(cfg, bars)
    assert len(r.trades) == 2 and r.blocked == 2 and r.signals == 4


def test_daily_loss_limit_blocks_rest_of_day(cfg):
    # 一笔止损亏到 10 USDT 以上的三档仓位不好造；直接用很大的跳空让一档仓位亏过 12
    bars = [bar(0, S=30), bar(1, S=45), bar(2, S=45, px=58000, op=58000, lo=58000)]
    bars += [bar(i, S=0) for i in range(3, 3 + 4 * 240)]          # 4 小时后（过了所有暂停）
    bars += [bar(1000, S=30), bar(1001, S=45)]
    r = run(cfg, bars)
    assert len(r.trades) == 1 and r.trades[0].pnl < -12 and r.blocked == 1


def test_min_equity_halts(cfg):
    import dataclasses
    rk = dataclasses.replace(cfg.risk, min_equity_usdt=199.5)  # 一笔亏 0.6（手续费和价差）就跌破
    cfg2 = dataclasses.replace(cfg, risk=rk)
    bars = [bar(0, S=30), bar(1, S=45), bar(2, S=10), bar(3, S=0)] + [bar(i, S=0) for i in range(4, 1500)]
    bars += [bar(1500, S=30), bar(1501, S=45)]
    r = run(cfg2, bars)
    assert len(r.trades) == 1 and r.halted and r.blocked == 1


def test_maker_entry_needs_price_through_limit(cfg):
    # 挂在买一 59999.9：下一个桶最低正好 59999.9（碰到不算）→ 没成交，放弃
    bars = [bar(0, S=30), bar(1, S=45), bar(2, S=45, lo=59999.9), bar(3, S=10)]
    r = run(cfg, bars, order=MAKER)
    assert r.trades == [] and r.missed_maker == 1
    # 穿过去才算成交，按挂单价、挂单费率
    bars = [bar(0, S=30), bar(1, S=45), bar(2, S=45, lo=59999.0), bar(3, S=10, px=60100)]
    (t,) = run(cfg, bars, order=MAKER).trades
    q = 600 / 59999.9
    assert t.entry_px == 59999.9
    assert t.fees == pytest.approx(q * 59999.9 * cfg.fees.maker_rate + q * 60099.9 * cfg.fees.taker_rate)


def test_no_trade_condition_blocks_entry(cfg):
    r = run(cfg, [bar(0, S=30), bar(1, S=45, no_trade=True), bar(2, S=50)])
    assert r.trades == [] and r.signals == 0


# ---------- 资金流带 ----------

@pytest.fixture
def band_policy(cfg, st):
    return BandPolicy(cfg, st.band, st.band_position)


def test_band_size_from_noise_and_budget(cfg, st, band_policy):
    # 噪声 0.3% → 止损 0.6%；一档最多亏 3 → 仓位 500 USDT（低于一档上限 600）
    bars = [bar(0, S=30, z=0.5), bar(1, S=45, z=1.5, noise=0.003), bar(2, S=45, z=1.5, px=60000), bar(3, S=10, z=-2)]
    (t,) = run(cfg, bars, band_policy).trades
    q = 500 / 60000.1
    assert t.reason == "资金掉头"
    assert t.fees == pytest.approx(cfg.fees.taker_rate * q * (60000.1 + 59999.9))
    # 噪声很小：止损取下限 0.15%，仓位受该档上限 600 USDT 限制
    bars = [bar(0, S=30, z=0.5), bar(1, S=45, z=1.5, noise=0.0001), bar(2, S=10, z=-2)]
    (t,) = run(cfg, bars, band_policy).trades
    assert t.fees == pytest.approx(cfg.fees.taker_rate * 600 / 60000.1 * (60000.1 + 59999.9))


def test_band_add_reduce_and_fade_exit(cfg, band_policy):
    bars = [bar(0, S=30, z=0.5), bar(1, S=45, z=1.5),
            bar(2, S=45, z=2.5, px=60050),          # 外带之外、有浮盈 → 二档
            bar(3, S=45, z=2.6, px=60080),          # 再加 → 三档
            bar(4, S=45, z=0.5, px=60080), bar(5, S=45, z=0.4, px=60080),   # 连续 2 桶回内带 → 二档
            bar(6, S=45, z=0.3, px=60080), bar(7, S=45, z=0.2, px=60080),   # → 一档
            bar(8, S=45, z=0.3, px=60080), bar(9, S=45, z=0.2, px=60080), bar(10, S=45, z=0.1, px=60080)]
    (t,) = run(cfg, bars, band_policy).trades
    assert t.max_level == 3
    assert t.actions.count("资金到外带") == 2 and t.actions.count("资金回到内带") == 2
    assert t.reason == "资金停了" and t.exit_t == bars[10].end


def test_band_no_add_when_losing(cfg, band_policy):
    bars = [bar(0, S=30, z=0.5), bar(1, S=45, z=1.5), bar(2, S=45, z=3, px=59990), bar(3, S=10, z=-2, px=59990)]
    (t,) = run(cfg, bars, band_policy).trades
    assert t.max_level == 1


def test_band_stop_ratchets_on_add(cfg, band_policy):
    from flowmon.positions import Position, Resize
    sim = Simulator(cfg, band_policy)
    pos = Position(d=1)
    sim._fill(pos, 0.01, 60000, 0)
    sim._set_stop(pos, Resize(qty=0.01, level=1, reason="", budget=3, dist_frac=0.002))
    first = pos.stop
    sim._fill(pos, 0.005, 59950, 0)                 # 均价下移
    sim._set_stop(pos, Resize(qty=0.015, level=2, reason="", budget=6, dist_frac=0.002, ratchet=True))
    assert pos.stop == first                        # 不往不利方向挪


# ---------- 资金流带：和朴素实现逐桶对 ----------

def test_flow_band_matches_naive(cfg):
    base_h = 40 * 15 / 3600
    c = make_cfg(cfg.base_dir, score={"baseline_hours": base_h, "baseline_lookback_hours": base_h * 2}).score
    bc = BandCfg(window_buckets=4, inner=1, outer=2, noise_window_minutes=5, noise_horizon_minutes=5,
                 noise_min_coverage=0.5)
    rng = random.Random(5)
    rows, i, px = [], 0, 100.0
    while len(rows) < 300:
        i += 1
        if rng.random() < 0.03:
            continue
        px *= math.exp(rng.gauss(0, 0.001))
        rows.append({"start_ms": i * W, "complete": rng.random() > 0.05, "buy_vol": rng.random() * 3,
                     "sell_vol": rng.random() * 3, "close": px})
    fb = FlowBand(c, bc, 15)
    got = [fb.update(r) for r in rows]
    by = {r["start_ms"]: r for r in rows}
    n, look = 40, 80
    xs = {}
    for r, g in zip(rows, got):
        s = r["start_ms"]
        win = [by.get(s - k * W) for k in range(4)]
        x = sum(w["buy_vol"] - w["sell_vol"] for w in win) if all(w and w["complete"] for w in win) else None
        if x is not None:
            xs[s] = x
        assert (g.x is None) == (x is None)
        if x is not None:
            assert g.x == pytest.approx(x)
        past = [v for t, v in sorted(xs.items()) if s - look * W < t <= s][-n:]
        ready = rows[0]["start_ms"] <= s - (n - 1) * W and len(past) >= 0.8 * n
        if ready and x is not None:
            assert g.z == pytest.approx(x / math.sqrt(sum(v * v for v in past) / len(past)))
        else:
            assert g.z is None
        rets = []
        for t in range(s - 19 * W, s + W, W):
            a, b = by.get(t - W), by.get(t)
            if a and b and a["complete"] and b["complete"]:
                rets.append(math.log(b["close"] / a["close"]))
        if len(rets) >= 10:
            assert g.noise == pytest.approx(math.sqrt(sum(x * x for x in rets) / len(rets) * 20))
        else:
            assert g.noise is None


# ---------- 回测工具 ----------

def test_shift_keeps_prices_moves_signals():
    bars = [bar(i, S=float(i), z=float(i), px=60000 + i) for i in range(10)]
    sh = shifted(bars, 3)
    assert [b.close for b in sh] == [b.close for b in bars]
    assert [b.S for b in sh] == [7, 8, 9, 0, 1, 2, 3, 4, 5, 6]
    assert [b.z for b in shifted(bars, 0)] == [b.z for b in bars]


def test_split_by_time():
    bars = [bar(i) for i in range(100)]
    tr, te = split(bars, 0.6)
    assert len(tr) + len(te) == 100 and tr[-1].t < te[0].t and len(tr) == 60


def test_spearman():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1)
    assert spearman([1, 2], [1, 2]) is None


def test_tools_tell_noise_from_a_real_edge(cfg, st):
    """没有规律的行情：两套都亏（手续费）；植入了规律的行情：都赚，打乱时间对照的 p 值到最小。"""
    import dataclasses
    st2 = dataclasses.replace(st, backtest=dataclasses.replace(st.backtest, null_runs=19))
    from flowmon.positions import bars_from_rows
    null_rows = make_rows(cfg, 24 * 4, seed=3, edge=0.0)
    edge_rows = make_rows(cfg, 24 * 4, seed=3, edge=0.0003)
    for pol in (Spec7Policy(cfg), BandPolicy(cfg, st.band, st.band_position)):
        nb = bars_from_rows(null_rows, cfg, st.band)
        assert summarize(Simulator(cfg, pol).run(nb))["pnl"] < 0
        eb = bars_from_rows(edge_rows, cfg, st.band)
        actual, dist, p = null_test(cfg, st2, eb, pol, TAKER)
        assert actual > 0 and p == pytest.approx(1 / 20) and max(dist) < actual


def test_report_and_cli(tmp_path, st):
    cfgp = tmp_path / "c.toml"
    cfgp.write_text((ROOT / "config.example.toml").read_text(encoding="utf-8"), encoding="utf-8")
    from flowmon import config as config_mod
    cfg = config_mod.load(cfgp)
    rows = make_rows(cfg, 30, seed=1, edge=0.0002)
    w = DailyCsv(cfg.data_dir / "buckets", [c for c, _ in bucket_columns(cfg)])
    from flowmon.bucket import day_of, iso_utc
    for r in rows:
        w.write(day_of(r["start_ms"]), {**r, "time_utc": iso_utc(r["start_ms"])})
    w.close()
    text = report(cfg, st, rows, with_null=False, with_fit=False)
    assert "## 全部数据" in text and "资金流带" in text and "规格第 7 节" in text
    assert cli.main(["backtest", "--config", str(cfgp), "--strategy", str(ROOT / "strategy.example.toml"),
                     "--quick", "--write"]) == 0
    assert (cfg.data_dir / "reports" / "backtest.md").read_text(encoding="utf-8").startswith("# 阶段二回测")
