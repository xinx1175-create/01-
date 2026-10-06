"""本地盘口：先收完整快照，再逐条套增量。

连续性按 seqId / prevSeqId 校验：新消息的 prevSeqId 必须等于上一条的 seqId。
OKX 自 2026-06-23 起把 checksum 固定为 0，不能再用来校验；这里只在它非 0 时顺带核对。
"""
from __future__ import annotations

import bisect
import zlib
from dataclasses import dataclass

# OKX 协议规定 checksum 取双方各前 25 档，属于协议常量，不是待校准参数
CHECKSUM_DEPTH = 25


class BookInvalid(Exception):
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind


@dataclass(slots=True)
class Level:
    px_s: str
    sz_s: str
    sz: float
    orders_s: str


class _Side:
    """一侧盘口。价格升序存放；买盘最优价在末尾，卖盘最优价在开头。"""

    __slots__ = ("levels", "prices")

    def __init__(self):
        self.levels: dict[float, Level] = {}
        self.prices: list[float] = []

    def clear(self):
        self.levels.clear()
        self.prices.clear()

    def set(self, px: float, lv: Level) -> float:
        """写入一档，返回该价位减少的数量（没减少返回 0）。"""
        old = self.levels.get(px)
        if old is None:
            bisect.insort(self.prices, px)
            self.levels[px] = lv
            return 0.0
        self.levels[px] = lv
        return old.sz - lv.sz if old.sz > lv.sz else 0.0

    def remove(self, px: float) -> float:
        old = self.levels.pop(px, None)
        if old is None:
            return 0.0
        i = bisect.bisect_left(self.prices, px)
        del self.prices[i]
        return old.sz


class OrderBook:
    def __init__(self):
        self.bids = _Side()
        self.asks = _Side()
        self.seq_id: int | None = None
        self.ts: int | None = None
        self.ready = False

    def reset(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.seq_id = None
        self.ts = None
        self.ready = False

    # ---------- 写入 ----------

    def apply_snapshot(self, bids, asks, ts: int, seq_id: int, checksum: int | None) -> None:
        self.reset()
        for row in bids:
            self._put(self.bids, row)
        for row in asks:
            self._put(self.asks, row)
        self.seq_id = seq_id
        self.ts = ts
        self._verify(checksum)
        self.ready = True

    def apply_update(self, bids, asks, ts: int, seq_id: int, prev_seq_id: int,
                     checksum: int | None) -> list[tuple[str, float, float]]:
        """套一条增量。返回 [(side, 价格, 减少的张数)]，供撤单量估算用。"""
        if not self.ready:
            raise BookInvalid("not_ready", "还没收到快照")
        if prev_seq_id != self.seq_id:
            raise BookInvalid("seq_gap", f"prevSeqId={prev_seq_id} 上一条 seqId={self.seq_id}")
        dec: list[tuple[str, float, float]] = []
        for side_name, side, rows in (("bid", self.bids, bids), ("ask", self.asks, asks)):
            for row in rows:
                d = self._put(side, row)
                if d > 0:
                    dec.append((side_name, float(row[0]), d))
        self.seq_id = seq_id
        self.ts = ts
        self._verify(checksum)
        return dec

    @staticmethod
    def _put(side: _Side, row) -> float:
        px_s, sz_s = row[0], row[1]
        orders_s = row[3] if len(row) > 3 else ""
        px = float(px_s)
        sz = float(sz_s)
        if sz == 0:
            return side.remove(px)
        return side.set(px, Level(px_s, sz_s, sz, orders_s))

    def _verify(self, checksum: int | None) -> None:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is not None and ba is not None and bb[0] >= ba[0]:
            raise BookInvalid("crossed", f"买一 {bb[0]} >= 卖一 {ba[0]}")
        if checksum:  # 0 / None 表示交易所不再提供
            mine = self.checksum()
            if mine != checksum:
                raise BookInvalid("checksum", f"本地 {mine} 交易所 {checksum}")

    # ---------- 读取 ----------

    def best_bid(self) -> tuple[float, float] | None:
        if not self.bids.prices:
            return None
        px = self.bids.prices[-1]
        return px, self.bids.levels[px].sz

    def best_ask(self) -> tuple[float, float] | None:
        if not self.asks.prices:
            return None
        px = self.asks.prices[0]
        return px, self.asks.levels[px].sz

    def mid(self) -> float | None:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        return (bb[0] + ba[0]) / 2

    def top(self, n: int) -> tuple[list[list[str]], list[list[str]]]:
        """前 n 档，最优价在前，每档 [价格, 张数, 订单数]，保留交易所原始字符串。"""
        bp, ap = self.bids.prices, self.asks.prices
        bids = [self._row(self.bids.levels[p]) for p in reversed(bp[-n:])] if n > 0 else []
        asks = [self._row(self.asks.levels[p]) for p in ap[:n]]
        return bids, asks

    @staticmethod
    def _row(lv: Level) -> list[str]:
        return [lv.px_s, lv.sz_s, lv.orders_s]

    def depth_within(self, pct: float) -> tuple[float, float, bool] | None:
        """中间价上下 pct% 内的买单、卖单张数；第三项为 True 表示盘口没铺满这个范围。"""
        mid = self.mid()
        if mid is None:
            return None
        lo = mid * (1 - pct / 100)
        hi = mid * (1 + pct / 100)
        bp, ap = self.bids.prices, self.asks.prices
        i = bisect.bisect_left(bp, lo)
        j = bisect.bisect_right(ap, hi)
        bid_sum = sum(self.bids.levels[p].sz for p in bp[i:])
        ask_sum = sum(self.asks.levels[p].sz for p in ap[:j])
        truncated = i == 0 or j == len(ap)
        return bid_sum, ask_sum, truncated

    def checksum(self) -> int:
        bp, ap = self.bids.prices, self.asks.prices
        bids = [self.bids.levels[p] for p in reversed(bp[-CHECKSUM_DEPTH:])]
        asks = [self.asks.levels[p] for p in ap[:CHECKSUM_DEPTH]]
        return checksum_of(bids, asks)


def checksum_of(bids: list[Level], asks: list[Level]) -> int:
    """OKX 规则：买卖交替排成 买价:买量:卖价:卖量:…，缺的一侧跳过，CRC32 取有符号 32 位。"""
    parts: list[str] = []
    for i in range(CHECKSUM_DEPTH):
        if i < len(bids):
            parts.append(f"{bids[i].px_s}:{bids[i].sz_s}")
        if i < len(asks):
            parts.append(f"{asks[i].px_s}:{asks[i].sz_s}")
    v = zlib.crc32(":".join(parts).encode())
    return v - (1 << 32) if v >= (1 << 31) else v


def walk_fill(levels: list[list[str]], notional: float, ct_val: float) -> float | None:
    """按给定档位吃掉 notional（计价币）的市价单，返回预计成交均价；档位不够返回 None。

    levels 为 [价格, 张数, …]，最优价在前；1 张 = ct_val 个基础币。
    """
    need = notional
    cost = 0.0
    qty = 0.0
    for row in levels:
        px = float(row[0])
        base = float(row[1]) * ct_val
        take = min(need, px * base)
        cost += take
        qty += take / px
        need -= take
        if need <= 0:
            return cost / qty
    return None
