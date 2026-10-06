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
    assert [r["complete"] for r in rows] == [False, False, False, False]
    # 断线期间没成交：价格未知，留空，不沿用断线前的收盘（原因里另记 no_price）
    assert rows[0]["incomplete_reason"] == "disconnect|no_price"
    assert all(r["close"] is None and r["high"] is None for r in rows[:3])
    # 重连后第一个桶也没成交：断线之后还没见过成交，价格仍未知
    assert rows[3]["close"] is None and rows[3]["incomplete_reason"] == "no_price"
    # 来了成交之后恢复；之后行情通着、只是没成交的桶，沿用上一个收盘
    agg.on_oi(T0 + 5 * W + 1000, 503.0)
    agg.on_trade(T0 + 5 * W + 2000, 102.0, 1, "buy", 1, T0 + 5 * W + 2010)
    agg.on_oi(T0 + 6 * W + 1000, 504.0)
    r6, r7 = [r for r, _ in agg.advance(T0 + 7 * W + cfg.bucket.close_grace_ms, book)]
    assert r6["complete"] and r6["close"] == 102
    assert r7["complete"] and r7["close"] == 102 and r7["trade_count"] == 0


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
        out = cond.update(row, True, 30 if i % 2 else -30)
    assert out["flips"] >= 3 and out["nt_flips"]
    assert out["nt_low_vol"] and out["range_rank"] < 30


def _flips(cfg, scores, valid=None):
    """按顺序喂一串分数（每个一个桶），返回最后一个桶的条件结果。"""
    cond = Conditions(cfg.conditions, 15, Calendar(None, 15, 30))
    out = None
    for i, S in enumerate(scores):
        row = {"start_ms": T0 + i * W, "complete": True, "high": 100.1, "low": 99.9, "close": 100}
        out = cond.update(row, True if valid is None else valid[i], S)
    return out


def test_flip_definition_examples(cfg_factory):
    cfg = cfg_factory()  # flip_threshold = 20
    # 你给的两个例子：+25 → +5 → −3 → +22 不算翻转；+25 → −22 算一次
    assert _flips(cfg, [25, 5, -3, 22])["flips"] == 0
    assert _flips(cfg, [25, -22])["flips"] == 1
    # 中间在 ±20 以内来回摆多少次都不算，只看两次超出时的方向
    assert _flips(cfg, [25, 5, -3, 4, -19, 19, -22])["flips"] == 1
    # 刚好 ±20 不算超出
    assert _flips(cfg, [25, -20, 20, -20])["flips"] == 0
    # 一次超出持续多个桶只算一个方向；之后每反向超出一次记一次
    assert _flips(cfg, [25, 30, 28, -21, -40, 21, -21])["flips"] == 3
    # 分数无效的桶不参与
    assert _flips(cfg, [25, -50, -22], valid=[True, False, True])["flips"] == 1


def test_flip_is_counted_when_it_happens_even_if_previous_excursion_left_window(cfg_factory):
    # 窗口 2 分钟 = 8 个桶。+25 之后 12 个桶都在 ±20 以内，再跌破 −20：
    # 上一次超出早已滑出窗口，但翻转发生在窗口内，照样算。
    cfg = cfg_factory(conditions={"flip_window_minutes": 2})
    out = _flips(cfg, [25] + [3] * 12 + [-22])
    assert out["flips"] == 1
    # 翻转本身滑出窗口后就不再计数
    out = _flips(cfg, [25, -22] + [3] * 8)
    assert out["flips"] == 0


def test_flip_counts_recorded_per_threshold(cfg_factory):
    cfg = cfg_factory()
    out = _flips(cfg, [2, -2, 15, -15, 25, -25, 35, -35])
    # 门槛 0 即规格原文：任何正负变化都算（7 次）；门槛越高，算进来的越少
    assert (out["flips_0"], out["flips_10"], out["flips_20"], out["flips_30"]) == (7, 5, 3, 1)
    assert out["flips"] == out["flips_20"] == 3 and out["nt_flips"]


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
    cfg = cfg_factory(events={"followup_minutes": 1, "price_horizons_s": [15, 60], "control_per_hour": 0},
                      evaluation={"horizon_s": 60})
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


def test_control_group_four_per_hour_one_per_quarter(cfg_factory):
    cfg = cfg_factory()  # control_per_hour = 4

    def run():
        eng = EventEngine(cfg, 0.01)
        out = []
        for i in range(480):  # 两个小时
            c, _ = eng.on_bucket(row(i), sc(5), NOCOND, cap())
            out += [e for e in c if e["kind"] == "control"]
        return out

    a, b = run(), run()
    assert [e["event_id"] for e in a] == [e["event_id"] for e in b]  # 种子固定，可复现
    assert len(a) == 8 and all(e["tier"] is None for e in a)
    # 每 15 分钟一段，每段恰好一条
    quarters = [(e["ts_ms"] - W - T0) // 900_000 for e in a]
    assert quarters == list(range(8))
    assert {e["direction"] for e in a} <= {1, -1}


def test_control_skips_signal_bucket(cfg_factory):
    cfg = cfg_factory(events={"control_per_hour": 1})
    eng = EventEngine(cfg, 0.01)
    # 先找出这一小时抽到的时刻，再让那个桶正好出信号
    eng.on_bucket(row(0), sc(5), NOCOND, cap())
    idx = eng.ctrl_slots[0][0]
    got = []
    for i in range(1, 240):
        S = 45 if i == idx else 5
        c, _ = eng.on_bucket(row(i), sc(S), NOCOND, cap())
        got += [(i, e["kind"]) for e in c]
    ctrl = [i for i, k in got if k == "control"]
    if idx >= 1:
        assert ctrl == [idx + 1]  # 顺延到下一个桶


def test_gap_marks_followup_incomplete(cfg_factory):
    cfg = cfg_factory(events={"control_per_hour": 0})
    eng = EventEngine(cfg, 0.01)
    eng.on_bucket(row(0), sc(20), NOCOND, cap())
    created, _ = eng.on_bucket(row(1), sc(45), NOCOND, cap())
    eng.on_bucket(row(5), sc(45), NOCOND, cap())  # 缺 2–4 号
    assert all(not e["followup_complete"] for e in created)
