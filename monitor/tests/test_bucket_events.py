"""封桶、完整性标记、信号事件与对照组。"""
import pytest

from flowmon.bucket import Aggregator, BookCapture
from flowmon.conditions import Calendar, Conditions
from flowmon.events import EventEngine, limit_filled
from flowmon.orderbook import OrderBook
from flowmon.score import ScoreResult

W = 15_000
T0 = 1_760_000_000_000 - 1_760_000_000_000 % 3_600_000  # 某个整点


def ready_book():
    b = OrderBook()
    b.apply_snapshot([["100.0", "5", "0", "1"]], [["100.1", "4", "0", "1"]], T0, 1, 0)
    return b


def test_aggregator_closes_on_watermark_and_marks_down(cfg_factory):
    cfg = cfg_factory()
    agg = Aggregator(cfg.bucket, 0.01)
    book = ready_book()
    agg.on_oi(T0 - 1000, 500.0)
    agg.on_trade(T0 + 100, 100.0, 2, "buy", 3, T0 + 120)
    agg.on_trade(T0 + 200, 101.0, 1, "sell", 1, T0 + 230)
    assert agg.advance(T0 + W + cfg.bucket.close_grace_ms - 1, book) == []
    (row, _), = agg.advance(T0 + W + cfg.bucket.close_grace_ms, book)
    assert row["complete"] and row["open"] == 100 and row["high"] == 101 and row["close"] == 101
    assert row["buy_vol"] == pytest.approx(0.02) and row["sell_vol"] == pytest.approx(0.01)
    assert row["trade_count"] == 4 and row["latency_ms"] == 25
    assert row["oi"] == 500 and row["bid1"] == 100.0
    # 断线：从 T0+W+5000 到 T0+3W+1000，覆盖第 2、3、4 个桶
    agg.on_oi(T0 + W + 1000, 501.0)
    agg.set_down("disconnect", T0 + W + 5000)
    agg.clear_down({"disconnect"}, T0 + 3 * W + 1000)
    agg.on_oi(T0 + 3 * W + 2000, 502.0)
    rows = [r for r, _ in agg.advance(T0 + 5 * W + cfg.bucket.close_grace_ms, book)]
    assert [r["complete"] for r in rows] == [False, False, False, True]
    assert rows[0]["incomplete_reason"] == "disconnect"
    assert rows[0]["close"] == 101  # 没成交沿用上一个收盘


def test_oi_stale_and_late_trade(cfg_factory):
    cfg = cfg_factory()
    agg = Aggregator(cfg.bucket, 0.01)
    book = ready_book()
    agg.on_oi(T0 - 40_000, 500.0)  # 到桶结束已 55 秒没更新
    agg.on_trade(T0 + 1, 100.0, 1, "buy", 1, T0 + 2)
    (row, _), = agg.advance(T0 + W + cfg.bucket.close_grace_ms, book)
    assert not row["complete"] and "oi_stale" in row["incomplete_reason"]
    agg.on_trade(T0 + 5, 100.0, 1, "buy", 1, T0 + W + 900)  # 封桶后才到
    agg.on_oi(T0 + W + 1, 501.0)
    (row2, _), = agg.advance(T0 + 2 * W + cfg.bucket.close_grace_ms, book)
    assert row2["late_trades"] == 1 and row2["buy_vol"] == 0


def test_book_capture_before_first_update_past_boundary(cfg_factory):
    cfg = cfg_factory()
    agg = Aggregator(cfg.bucket, 0.01)
    book = ready_book()
    agg.on_oi(T0, 1.0)
    agg.on_trade(T0 + 1, 100.0, 1, "buy", 1, T0 + 2)
    # 桶结束之后的第一条盘口增量到来前截图，截到的是桶结束时的盘口
    agg.before_book(T0 + W + 50, book)
    book.apply_update([["100.0", "9", "0", "1"]], [], T0 + W + 50, 2, 1, 0)
    (row, b), = agg.advance(T0 + W + 10_000, book)
    assert b.capture.bid1_sz == 5


def test_calendar_window(tmp_path):
    p = tmp_path / "cal.csv"
    p.write_text("time_utc,name\n2026-10-10T12:30:00Z,US CPI\n", encoding="utf-8")
    cal = Calendar(p, 15, 30)
    t = 1791635400000  # 2026-10-10T12:30:00Z
    assert cal.active(t - 15 * 60_000) == "US CPI"
    assert cal.active(t - 15 * 60_000 - 1) is None
    assert cal.active(t + 30 * 60_000) == "US CPI"
    assert cal.active(t + 30 * 60_000 + 1) is None


def test_conditions_flips_and_low_vol(cfg_factory, tmp_path):
    cfg = cfg_factory(conditions={"low_vol_window_minutes": 1, "flip_window_minutes": 2, "flip_count": 3})
    cond = Conditions(cfg.conditions, 15, Calendar(None, 15, 30))
    out = None
    for i in range(40):
        px = 100 + (0.5 if i % 2 else 0) * (1 if i < 30 else 0.01)  # 后段几乎不动
        row = {"start_ms": T0 + i * W, "complete": True, "high": px + 0.1, "low": px - 0.1, "close": px}
        out = cond.update(row, True, 10 if i % 2 else -10)
    assert out["flips"] >= 3 and out["nt_flips"]
    assert out["nt_low_vol"] and out["range_rank"] < 30


# ---------- 信号事件 ----------

def cap(bid=100.0, ask=100.1):
    return BookCapture(ts=0, seq=1, bids=[[f"{bid}", "3", "1"]], asks=[[f"{ask}", "5", "1"], ["100.2", "1000", "1"]],
                       bid1=bid, ask1=ask, bid1_sz=3, ask1_sz=5, near_bid=3, near_ask=5, near_truncated=False)


def row(i, close=100.0, hi=None, lo=None, complete=True):
    return {"start_ms": T0 + i * W, "close": close, "high": hi if hi is not None else close,
            "low": lo if lo is not None else close, "high_ms": None, "low_ms": None,
            "complete": complete, "latency_ms": 20.0}


def sc(S, valid=True):
    return ScoreResult(F=0.5, M=1, A=0.5, Z=1, B=0.3, R=S, S=S, valid=valid)


NOCOND = {"no_trade": False, "no_trade_reason": ""}


def test_signal_crossings_and_followup(cfg_factory):
    cfg = cfg_factory(events={"followup_minutes": 1, "price_horizons_s": [15, 60], "control_per_hour": 0})
    eng = EventEngine(cfg, 0.01)
    eng.on_bucket(row(0), sc(20), NOCOND, cap())
    created, _ = eng.on_bucket(row(1), sc(45), NOCOND, cap())
    # 20 → 45 穿过 30 和 40 两档
    assert sorted(e["tier"] for e in created) == [30, 40]
    e = created[0]
    assert e["direction"] == 1 and e["ts_ms"] == T0 + 2 * W and e["price"] == 100.0
    assert e["limit_px"] == 100.0 and e["limit_queue"] == pytest.approx(0.03)
    # 600 USDT 市价买：卖一 5 张 = 0.05 BTC = 5.005 USDT，其余 594.995 USDT 在 100.2 成交
    assert e["mkt_fill_px"] == pytest.approx(600 / (0.05 + 594.995 / 100.2))
    assert e["mkt_slip_bps"] == pytest.approx((e["mkt_fill_px"] - 100.05) / 100.05 * 1e4)
    # 没回到门槛内就不会重复触发
    created, _ = eng.on_bucket(row(2, 101, hi=102, lo=99.5), sc(60), NOCOND, cap())
    assert [x["tier"] for x in created] == [50]
    eng.on_bucket(row(3, 101.5), sc(30), NOCOND, cap())
    eng.on_bucket(row(4, 100.5), sc(10), NOCOND, cap())
    _, done = eng.on_bucket(row(5, 103), sc(5), NOCOND, cap())
    e = next(x for x in done if x["tier"] == 40)
    assert e["px_15s"] == 101 and e["px_60s"] == 103
    assert e["mfe_pct"] == pytest.approx(3.0) and e["mae_pct"] == pytest.approx(0.5)
    assert e["max_score"] == 60 and e["max_score_after_s"] == 15
    assert e["t_ge_60_s"] == 15 and e["t_ge_75_s"] is None
    assert e["t_lt_35_s"] == 30 and e["t_lt_15_s"] == 45
    assert e["followup_complete"]


def test_short_crossing_needs_previous_inside(cfg_factory):
    cfg = cfg_factory(events={"control_per_hour": 0})
    eng = EventEngine(cfg, 0.01)
    eng.on_bucket(row(0), sc(-50), NOCOND, cap())
    created, _ = eng.on_bucket(row(1), sc(-55), NOCOND, cap())
    assert created == []  # 上一个就已经在门槛外
    eng.on_bucket(row(2), sc(0, valid=False), NOCOND, cap())
    created, _ = eng.on_bucket(row(3), sc(-45), NOCOND, cap())
    assert created == []  # 上一个无效，不算穿越
    eng.on_bucket(row(4), sc(-35), NOCOND, cap())
    created, _ = eng.on_bucket(row(5), sc(-41), NOCOND, cap())
    assert [(e["direction"], e["tier"]) for e in created] == [(-1, 40)]
    assert created[0]["limit_px"] == 100.1


def test_limit_fill_rules():
    tr = [(0, 100.0, 2, "sell"), (1, 100.0, 1.5, "sell"), (2, 99.9, 1, "buy")]
    # 买一 100.0 前面排 3 张：同价成交 3.5 张 > 3 → 成交
    assert limit_filled(tr, 0, 5000, 1, 100.0, 3)
    assert not limit_filled(tr, 0, 5000, 1, 100.0, 4)
    # 价格穿过挂单价 → 成交
    assert limit_filled([(0, 99.9, 0.01, "sell")], 0, 5000, 1, 100.0, 1000)
    # 时间窗之外的不算
    assert not limit_filled([(6000, 99.0, 9, "sell")], 0, 5000, 1, 100.0, 1)
    # 空单挂卖一，等主动买
    assert limit_filled([(0, 100.2, 1, "buy")], 0, 5000, -1, 100.1, 50)


def test_limit_fill_evaluated_from_trade_stream(cfg_factory):
    cfg = cfg_factory(events={"control_per_hour": 0})
    eng = EventEngine(cfg, 0.01)
    eng.on_bucket(row(0), sc(20), NOCOND, cap())
    created, _ = eng.on_bucket(row(1), sc(45), NOCOND, cap())
    E = created[0]["ts_ms"]
    eng.on_trade(E + 6000, 99.9, 1, "sell")  # 5 秒之后才穿价
    eng.on_bucket(row(2), sc(45), NOCOND, cap())
    e = created[0]
    assert e["limit_fill_5s"] is False and e["limit_fill_15s"] is True


def test_control_group_deterministic_and_skips_signal_buckets(cfg_factory):
    cfg = cfg_factory()

    def run():
        eng = EventEngine(cfg, 0.01)
        out = []
        for i in range(240):
            c, _ = eng.on_bucket(row(i), sc(5), NOCOND, cap())
            out += [e for e in c if e["kind"] == "control"]
        return out

    a, b = run(), run()
    assert len(a) == 1 and a[0]["event_id"] == b[0]["event_id"]
    assert a[0]["tier"] is None


def test_gap_marks_followup_incomplete(cfg_factory):
    cfg = cfg_factory(events={"control_per_hour": 0})
    eng = EventEngine(cfg, 0.01)
    eng.on_bucket(row(0), sc(20), NOCOND, cap())
    created, _ = eng.on_bucket(row(1), sc(45), NOCOND, cap())
    eng.on_bucket(row(5), sc(45), NOCOND, cap())  # 缺 2–4 号
    assert all(not e["followup_complete"] for e in created)
