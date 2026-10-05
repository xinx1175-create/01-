"""15 秒数据桶（§5）。

桶按交易所时间戳对齐，统一 UTC。实时与回放共用 Bucket；实时另由 Aggregator 负责
开桶、封桶、盘口截图和断线区间。
"""
from __future__ import annotations

import bisect
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import BucketCfg
from .orderbook import OrderBook


def iso_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def day_of(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass(slots=True)
class BookCapture:
    """桶结束那一刻的盘口：前 N 档原文 + 由完整本地盘口算出的指标。"""
    ts: int | None
    seq: int | None
    bids: list[list[str]]
    asks: list[list[str]]
    bid1: float
    ask1: float
    bid1_sz: float  # 张
    ask1_sz: float
    near_bid: float | None  # 张
    near_ask: float | None
    near_truncated: bool | None


def capture_book(book: OrderBook, cfg: BucketCfg) -> BookCapture | None:
    bb, ba = book.best_bid(), book.best_ask()
    if not book.ready or bb is None or ba is None:
        return None
    bids, asks = book.top(cfg.snapshot_levels)
    near = book.depth_within(cfg.near_depth_pct)
    return BookCapture(
        ts=book.ts, seq=book.seq_id, bids=bids, asks=asks,
        bid1=bb[0], ask1=ba[0], bid1_sz=bb[1], ask1_sz=ba[1],
        near_bid=near[0] if near else None, near_ask=near[1] if near else None,
        near_truncated=near[2] if near else None,
    )


def capture_from_levels(bids: list[list[str]], asks: list[list[str]], ts: int | None,
                        seq: int | None, cfg: BucketCfg) -> BookCapture | None:
    """回放用：从存下来的前 N 档重建截图。近处挂单只能用这 N 档算，可能偏小。"""
    book = OrderBook()
    try:
        book.apply_snapshot([[r[0], r[1], "0", r[2] if len(r) > 2 else ""] for r in bids],
                            [[r[0], r[1], "0", r[2] if len(r) > 2 else ""] for r in asks],
                            ts or 0, seq or 0, None)
    except Exception:
        return None
    cap = capture_book(book, cfg)
    if cap is not None:
        cap.ts, cap.seq = ts, seq
    return cap


def pick_oi(updates, end_ms: int) -> tuple[int, float] | None:
    """桶结束时刻的最新持仓量：时间戳 < 桶结束的最后一条。updates 按时间升序 [(ts, oi)]。"""
    i = bisect.bisect_left(updates, (end_ms, float("-inf")))
    return updates[i - 1] if i > 0 else None


@dataclass
class Bucket:
    start_ms: int
    width_ms: int
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    high_ms: int | None = None
    low_ms: int | None = None
    buy_ct: float = 0.0
    sell_ct: float = 0.0
    trade_count: int = 0
    trade_msgs: int = 0
    buy_by_px: dict[float, float] = field(default_factory=dict)   # 主动买：吃卖盘
    sell_by_px: dict[float, float] = field(default_factory=dict)  # 主动卖：吃买盘
    decreases: dict[tuple[str, float], float] = field(default_factory=dict)
    cancel_tracked: bool = True
    liq_long_ct: float = 0.0
    liq_short_ct: float = 0.0
    latencies: list[float] = field(default_factory=list)
    late_trades: int = 0
    reasons: set[str] = field(default_factory=set)
    capture: BookCapture | None = None

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.width_ms

    def add_trade(self, ts: int, px: float, sz_ct: float, side: str, count: int) -> None:
        if self.open is None:
            self.open = self.high = self.low = px
            self.high_ms = self.low_ms = ts
        else:
            if px > self.high:
                self.high, self.high_ms = px, ts
            if px < self.low:
                self.low, self.low_ms = px, ts
        self.close = px
        if side == "buy":
            self.buy_ct += sz_ct
            self.buy_by_px[px] = self.buy_by_px.get(px, 0.0) + sz_ct
        else:
            self.sell_ct += sz_ct
            self.sell_by_px[px] = self.sell_by_px.get(px, 0.0) + sz_ct
        self.trade_count += count
        self.trade_msgs += 1

    def add_decreases(self, decs, mid: float | None, range_pct: float) -> None:
        if mid is None:
            return
        lim = mid * range_pct / 100
        for side, px, d in decs:
            if abs(px - mid) <= lim:
                k = (side, px)
                self.decreases[k] = self.decreases.get(k, 0.0) + d

    def cancel_estimate(self) -> tuple[float, float]:
        """§5：某价位减少的挂单量减去该价位的成交量，取正的部分，买卖两侧分开（张）。

        买盘被吃掉的是主动卖单，卖盘被吃掉的是主动买单。
        """
        bid = ask = 0.0
        for (side, px), d in self.decreases.items():
            traded = (self.sell_by_px if side == "bid" else self.buy_by_px).get(px, 0.0)
            c = d - traded
            if c > 0:
                if side == "bid":
                    bid += c
                else:
                    ask += c
        return bid, ask

    def finish(self, prev_close: float | None, oi: tuple[int, float] | None,
               funding: float | None, ct_val: float, oi_stale_ms: int, judge: bool = True) -> dict:
        """封桶，产出桶表里属于桶本身的那部分字段（列定义见 schema.bucket_columns）。

        judge=False 时不自行判断完整性，只用预先放进 reasons 的原因（回放沿用实时记录）。
        """
        reasons = set(self.reasons)
        o, h, l, c = self.open, self.high, self.low, self.close
        if c is None:  # 整个桶没有成交，价格沿用上一个收盘
            o = h = l = c = prev_close
        cap = self.capture
        if judge:
            if c is None:
                reasons.add("no_price")
            if oi is None:
                reasons.add("oi_missing")
            elif self.end_ms - oi[0] > oi_stale_ms:
                reasons.add("oi_stale")
            if cap is None:
                reasons.add("no_book")
        cb, ca = self.cancel_estimate() if self.cancel_tracked else (None, None)
        lat = statistics.median(self.latencies) if self.latencies else None
        return {
            "time_utc": iso_utc(self.start_ms),
            "start_ms": self.start_ms,
            "width_s": self.width_ms // 1000,
            "open": o, "high": h, "low": l, "close": c,
            "high_ms": self.high_ms, "low_ms": self.low_ms,
            "buy_vol": self.buy_ct * ct_val,
            "sell_vol": self.sell_ct * ct_val,
            "trade_count": self.trade_count,
            "trade_msgs": self.trade_msgs,
            "oi": oi[1] if oi else None,
            "oi_ms": oi[0] if oi else None,
            "funding_rate": funding,
            "bid1": cap.bid1 if cap else None,
            "ask1": cap.ask1 if cap else None,
            "spread": (cap.ask1 - cap.bid1) if cap else None,
            "book_ms": cap.ts if cap else None,
            "book_seq": cap.seq if cap else None,
            "near_bid_vol": cap.near_bid * ct_val if cap and cap.near_bid is not None else None,
            "near_ask_vol": cap.near_ask * ct_val if cap and cap.near_ask is not None else None,
            "near_truncated": cap.near_truncated if cap else None,
            "cancel_bid_vol": cb * ct_val if cb is not None else None,
            "cancel_ask_vol": ca * ct_val if ca is not None else None,
            "liq_long_vol": self.liq_long_ct * ct_val,
            "liq_short_vol": self.liq_short_ct * ct_val,
            "latency_ms": lat,
            "late_trades": self.late_trades,
            "complete": not reasons,
            "incomplete_reason": "|".join(sorted(reasons)),
        }


class Aggregator:
    """实时聚合：按交易所时间开桶，水位线越过 桶结束 + 宽限 才封桶。"""

    def __init__(self, cfg: BucketCfg, ct_val: float):
        self.cfg = cfg
        self.ct_val = ct_val
        self.w = cfg.width_s * 1000
        self.grace = cfg.close_grace_ms
        self.oi_stale_ms = int(cfg.oi_stale_s * 1000)
        self.open: dict[int, Bucket] = {}
        self.cur: int | None = None          # 最早还没封的桶起点
        self.min_start = 0                   # 重启后不重复生成已经落盘的桶
        self.prev_close: float | None = None
        self.oi_updates: deque[tuple[int, float]] = deque()
        self.funding: float | None = None
        self.down: set[str] = set()
        self.down_since: int | None = None
        self.down_seen: set[str] = set()
        self.intervals: list[tuple[int, int, frozenset[str]]] = []

    # ---------- 桶 ----------

    def _bucket(self, ts: int) -> Bucket | None:
        s = ts - ts % self.w
        if self.cur is None:
            self.cur = max(s, self.min_start)
        if s < self.cur:
            return None
        b = self.open.get(s)
        if b is None:
            b = self.open[s] = Bucket(s, self.w)
        return b

    def _late(self) -> None:
        if self.cur is not None:
            b = self._bucket(self.cur)
            b.late_trades += 1

    # ---------- 输入 ----------

    def on_trade(self, ts: int, px: float, sz_ct: float, side: str, count: int, recv: int) -> None:
        b = self._bucket(ts)
        if b is None:
            self._late()
            return
        b.add_trade(ts, px, sz_ct, side, count)
        b.latencies.append(recv - ts)

    def before_book(self, ts: int, book: OrderBook) -> None:
        """在套用一条时间戳 ≥ 某桶结束的盘口消息之前，给那些桶截图。"""
        if self.cur is None or not book.ready:
            return
        s = self.cur
        while s + self.w <= ts:
            b = self._bucket(s)
            if b.capture is None:
                b.capture = capture_book(book, self.cfg)
            s += self.w

    def after_book(self, ts: int, decs, mid: float | None, recv: int) -> None:
        b = self._bucket(ts)
        if b is None:
            return
        if decs:
            b.add_decreases(decs, mid, self.cfg.cancel_range_pct)
        b.latencies.append(recv - ts)

    def book_reset(self, ts_est: int) -> None:
        """盘口重建：当前桶的撤单量不再可信。"""
        b = self._bucket(ts_est)
        if b is not None:
            b.cancel_tracked = False

    def on_oi(self, ts: int, oi: float) -> None:
        if self.oi_updates and ts < self.oi_updates[-1][0]:
            return
        self.oi_updates.append((ts, oi))

    def on_funding(self, rate: float) -> None:
        self.funding = rate

    def on_liq(self, ts: int, long_side: bool, sz_ct: float) -> None:
        b = self._bucket(ts)
        if b is None:
            return
        if long_side:
            b.liq_long_ct += sz_ct
        else:
            b.liq_short_ct += sz_ct

    # ---------- 断线区间 ----------

    def set_down(self, reason: str, at: int) -> None:
        if not self.down:
            self.down_since = at
            self.down_seen = set()
        self.down.add(reason)
        self.down_seen.add(reason)

    def clear_down(self, reasons: set[str], at: int) -> None:
        if not self.down:
            return
        self.down -= reasons
        if not self.down:
            self.intervals.append((self.down_since, at, frozenset(self.down_seen)))
            self.down_since = None

    def is_down(self) -> bool:
        return bool(self.down)

    def _down_reasons(self, s: int, e: int) -> set[str]:
        out: set[str] = set()
        for a, b, rs in self.intervals:
            if a < e and b >= s:
                out |= rs
        if self.down and self.down_since is not None and self.down_since < e:
            out |= self.down_seen
        return out

    # ---------- 封桶 ----------

    def advance(self, watermark: int, book: OrderBook) -> list[tuple[dict, Bucket]]:
        out = []
        if self.cur is None:
            return out
        while self.cur + self.w + self.grace <= watermark:
            s = self.cur
            b = self.open.pop(s, None) or Bucket(s, self.w)
            if b.capture is None and book.ready:
                b.capture = capture_book(book, self.cfg)
            b.reasons |= self._down_reasons(s, s + self.w)
            row = b.finish(self.prev_close, pick_oi(self.oi_updates, s + self.w), self.funding,
                           self.ct_val, self.oi_stale_ms)
            if row["close"] is not None:
                self.prev_close = row["close"]
            out.append((row, b))
            self.cur = s + self.w
            self.intervals = [iv for iv in self.intervals if iv[1] >= self.cur]
            # 持仓量只需留下当前桶之前的最后一条和之后的
            while len(self.oi_updates) >= 2 and self.oi_updates[1][0] < self.cur:
                self.oi_updates.popleft()
        return out
