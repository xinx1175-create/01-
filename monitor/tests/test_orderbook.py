"""本地盘口、撤单量估算、市价单成交价估算。"""
import zlib

import pytest

from flowmon.bucket import Bucket
from flowmon.orderbook import BookInvalid, OrderBook, walk_fill


def lv(px, sz, n="1"):
    return [px, sz, "0", n]


def book_with(bids, asks, seq=10):
    b = OrderBook()
    b.apply_snapshot([lv(*x) for x in bids], [lv(*x) for x in asks], 1000, seq, 0)
    return b


def test_snapshot_and_best():
    b = book_with([("100.0", "5"), ("99.9", "3")], [("100.1", "4"), ("100.2", "6")])
    assert b.best_bid() == (100.0, 5.0)
    assert b.best_ask() == (100.1, 4.0)
    assert b.mid() == pytest.approx(100.05)
    bids, asks = b.top(1)
    assert bids == [["100.0", "5", "1"]] and asks == [["100.1", "4", "1"]]


def test_update_returns_decreases_and_deletes():
    b = book_with([("100.0", "5"), ("99.9", "3")], [("100.1", "4"), ("100.2", "6")])
    decs = b.apply_update([lv("100.0", "2"), lv("99.8", "7")], [lv("100.2", "0")], 1100, 11, 10, 0)
    assert sorted(decs) == [("ask", 100.2, 6.0), ("bid", 100.0, 3.0)]
    assert 100.2 not in b.asks.levels and b.bids.levels[99.8].sz == 7
    assert b.seq_id == 11


def test_seq_gap_raises():
    b = book_with([("100.0", "5")], [("100.1", "4")], seq=10)
    with pytest.raises(BookInvalid) as e:
        b.apply_update([], [], 1100, 15, 12, 0)
    assert e.value.kind == "seq_gap"


def test_keepalive_and_seq_reset_are_valid():
    b = book_with([("100.0", "5")], [("100.1", "4")], seq=10)
    b.apply_update([], [], 1100, 10, 10, 0)     # 长时间无变化：seqId == prevSeqId
    b.apply_update([], [], 1200, 3, 10, 0)      # 维护时 seqId 重置：变小，但 prevSeqId 仍接得上
    b.apply_update([lv("100.0", "6")], [], 1300, 5, 3, 0)
    assert b.seq_id == 5


def test_crossed_book_raises():
    b = book_with([("100.0", "5")], [("100.1", "4")])
    with pytest.raises(BookInvalid) as e:
        b.apply_update([lv("100.2", "1")], [], 1100, 11, 10, 0)
    assert e.value.kind == "crossed"


def _crc(s):
    v = zlib.crc32(s.encode())
    return v - (1 << 32) if v >= (1 << 31) else v


def test_checksum_interleaves_and_skips_missing_side():
    b = book_with([("100.0", "5")], [("100.1", "4"), ("100.2", "6"), ("100.3", "1")])
    assert b.checksum() == _crc("100.0:5:100.1:4:100.2:6:100.3:1")
    # 非 0 的 checksum 对不上就报错；0 表示交易所不再提供，不校验
    with pytest.raises(BookInvalid):
        b.apply_update([], [], 1100, 11, 10, 12345)
    b2 = book_with([("100.0", "5")], [("100.1", "4")])
    b2.apply_update([lv("99.9", "2")], [], 1100, 11, 10, _crc("100.0:5:100.1:4:99.9:2"))


def test_depth_within_and_truncation():
    b = book_with([("100.0", "5"), ("99.9", "3"), ("99.0", "9")], [("100.1", "4"), ("100.2", "6"), ("101.5", "2")])
    # 中间价 100.05，±0.2% ≈ ±0.2001 → 买 99.8499 以上、卖 100.2501 以下
    bid, ask, trunc = b.depth_within(0.2)
    assert bid == 8 and ask == 10 and trunc is False
    bid, ask, trunc = b.depth_within(5)
    assert bid == 17 and ask == 12 and trunc is True


def test_walk_fill_vwap():
    # 1 张 = 0.01 BTC；第一档 2 张 = 2 USDT，不够 5 USDT，再吃 3 USDT 的 101
    fill = walk_fill([["100.0", "2"], ["101.0", "10"]], 5, 0.01)
    assert fill == pytest.approx(5 / (2 / 100 + 3 / 101))
    assert walk_fill([["100.0", "2"]], 5, 0.01) is None


# ---------- 撤单量估算（§5）：某价位减少的挂单量 − 该价位成交量，取正 ----------

def test_cancel_estimate_basic():
    b = Bucket(0, 15_000)
    mid = 100.05
    b.add_decreases([("bid", 100.0, 3.0), ("ask", 100.2, 6.0)], mid, 0.2)
    b.add_trade(1, 100.0, 1.0, "sell", 1)   # 主动卖吃掉买盘 100.0 上的 1 张
    assert b.cancel_estimate() == (pytest.approx(2.0), pytest.approx(6.0))


def test_cancel_sums_decreases_across_updates():
    b = Bucket(0, 15_000)
    # 99.9 档：3 → 7（增加不算）→ 1（减少 6）；期间该价成交 2 张 → 撤单 4
    b.add_decreases([("bid", 99.9, 6.0)], 100.0, 0.2)
    b.add_trade(1, 99.9, 2.0, "sell", 1)
    assert b.cancel_estimate()[0] == pytest.approx(4.0)


def test_cancel_never_negative_and_side_matched():
    b = Bucket(0, 15_000)
    b.add_decreases([("bid", 100.0, 1.0)], 100.0, 0.2)
    b.add_trade(1, 100.0, 5.0, "sell", 1)   # 成交比减少的还多 → 0
    b.add_trade(2, 100.0, 9.0, "buy", 1)    # 主动买吃的是卖盘，不抵扣买盘
    b.add_decreases([("ask", 100.1, 2.0)], 100.0, 0.2)
    assert b.cancel_estimate() == (0.0, pytest.approx(2.0))


def test_cancel_ignores_far_levels():
    b = Bucket(0, 15_000)
    b.add_decreases([("bid", 90.0, 50.0), ("bid", 99.9, 1.0)], 100.0, 0.2)
    assert b.cancel_estimate()[0] == pytest.approx(1.0)
