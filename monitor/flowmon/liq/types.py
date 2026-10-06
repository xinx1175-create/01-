"""爆仓模块的数据结构。时间一律是 UTC 毫秒。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

SEC_MS = 1_000
MIN_MS = 60_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000

LONG = "long"     # 多头被强平（系统卖出平多）
SHORT = "short"   # 空头被强平（系统买入平空）


def hour_floor(ts: int) -> int:
    return ts - ts % HOUR_MS


def liq_side(side: str | None, pos_side: str | None) -> str:
    """被平的是哪一方。双向持仓看 posSide；单向持仓（net）看 side：系统卖出 = 平多。"""
    if pos_side in (LONG, SHORT):
        return pos_side
    return LONG if side == "sell" else SHORT


@dataclass(frozen=True, slots=True)
class Liq:
    """一笔爆仓单。"""
    ts: int          # 成交时间
    inst: str        # BTC-USDT-SWAP
    side: str        # 被平方向：long（多头被平）/ short（空头被平）
    bk_px: float     # 破产价格
    sz: float        # 数量（张）
    qty: float       # 数量（币）= 张数 × 面值
    usd: float       # 金额（美元）= 币数 × 破产价格
    src: str         # 来源：ws（实时推送）/ rest（接口补全）
    recv: int        # 本机收到的时间

    @property
    def key(self) -> tuple:
        """去重键：同一笔从推送和接口各来一次时字段相同。"""
        return (self.inst, self.ts, self.side, self.bk_px, self.sz)


@dataclass(frozen=True, slots=True)
class Candle:
    """已收盘的 K 线（交易所的 1 小时、1 分钟 K 线）。ts 是开始时间。"""
    inst: str
    ts: int
    o: float
    h: float
    l: float
    c: float
    vol: float        # 成交量（张）
    vol_ccy: float    # 成交量（币）
    vol_quote: float  # 成交额（USDT）
    seen: int | None  # 本机第一次拿到它的时间；回补来的历史为 None


@dataclass(frozen=True, slots=True)
class Point:
    """时间序列上的一个点：交易所小时统计的持仓量（币）/ 多空人数比，或自己录的实时持仓量。"""
    inst: str
    ts: int
    value: float
    seen: int | None  # 本机第一次拿到的时间；回补来的历史为 None（这时按 baseline.rubik_avail_s 推算何时可用）


@dataclass(frozen=True, slots=True)
class SecBar:
    """1 秒 K 线，用自己收到的逐笔成交合成；没有成交的秒没有记录。"""
    inst: str
    ts: int
    o: float
    h: float
    l: float
    c: float
    vol: float        # 币


def merge_intervals(iv: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并重叠或首尾相接的区间 [lo, hi]。"""
    out: list[list[int]] = []
    for lo, hi in sorted((int(a), int(b)) for a, b in iv if b >= a):
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(a, b) for a, b in out]


def covered(iv: list[tuple[int, int]], a: int, b: int) -> bool:
    """[a, b) 是否整段落在合并后的区间里（iv 须已经 merge_intervals 过）。"""
    for lo, hi in iv:
        if lo <= a and b <= hi:
            return True
    return False


@dataclass
class InstData:
    """一个合约在一段时间里的全部原始数据（各列表按 ts 升序、已去重）。"""
    inst: str
    meta: dict                       # 合约信息：ctVal、lotSz、minSz、tickSz（字符串，OKX 原样）
    c1h: list[Candle]
    c1m: list[Candle]
    oi1h: list[Point]                # 交易所小时统计的持仓量（币）
    oilive: list[Point]              # 自己录的实时持仓量（币）
    ratio: list[Point]               # 多空人数比（按币种）
    liqs: list[Liq]
    coverage: list[tuple[int, int]]  # REST 完整覆盖的时间区间（合并过），覆盖到的小时才算爆仓数据完整
    sec: Callable[[int, int], list[SecBar]] = field(default=lambda a, b: [])  # 按需读 [a, b) 的 1 秒 K 线


@dataclass(slots=True)
class Hour:
    """一个合约的一个小时（ts = 整点开始）。

    所有字段都是「决策时刻」能知道的值：决策时刻 = 小时结束 + signal.decision_delay_s。
    缺数据的字段是 None（爆仓金额缺数据时为 0，同时 liq_ok = False）。"""
    inst: str
    ts: int
    # 1 小时 K 线
    o: float | None = None
    h: float | None = None
    l: float | None = None
    c: float | None = None
    vwap: float | None = None          # 成交均价 = 成交额 ÷ 成交量（币）；成交量为 0 时用 (h + l + c) / 3
    tr_pct: float | None = None        # 真实波幅 %：(max(h, 上一小时收盘) − min(l, 上一小时收盘)) ÷ 上一小时收盘 × 100
    atr_pct: float | None = None       # 平均小时波动 %：截至本小时（含）过去 days×24 小时 tr_pct 的平均
    atr_prev_pct: float | None = None  # 截至上一小时（不含本小时）的同一平均（F1 比较用）
    # 持仓量（币）
    oi_open: float | None = None       # 小时开始时刻
    oi_close: float | None = None      # 小时结束时刻
    oi_src: str = ""                   # oi_close 的来源：live（自己录的）/ rubik（交易所小时统计）/ ""
    oi_chg_pct: float | None = None    # (oi_close − oi_open) ÷ oi_open × 100
    d_oi: float | None = None          # 新增持仓量（币）= max(0, oi_close − oi_open)
    # 多空人数比
    ratio: float | None = None         # 决策时刻能拿到的最新一点（不晚于小时结束，不早于 ratio_max_age_s）
    ratio_ts: int | None = None
    ratio_hi: float | None = None      # 过去 days 天里比它低的点所占比例；≥ 1 − ratio_pct/100 → 最高的 10%
    ratio_lo: float | None = None      # 比它高的点所占比例；≥ 1 − ratio_pct/100 → 最低的 10%
    # 爆仓（美元）
    liq_ok: bool = False               # 这个小时整段被 REST 覆盖（数据完整）
    long_usd: float = 0.0
    short_usd: float = 0.0
    long_n: int = 0
    short_n: int = 0
    max_usd: float = 0.0               # 最大单笔
    max_side: str = ""
    long_mean: float | None = None     # 过去 days 天（不含本小时）数据完整的小时的平均；完整小时不够 min_coverage 时 None
    short_mean: float | None = None
    n_mean: float | None = None
    max_mean: float | None = None
    long_mult: float | None = None     # long_usd ÷ long_mean（均值 ≤ 0 时 None）
    short_mult: float | None = None
    n_mult: float | None = None
    max_mult: float | None = None

    @property
    def end(self) -> int:
        return self.ts + HOUR_MS

    @property
    def n(self) -> int:
        return self.long_n + self.short_n


def bin_index(px: float, bin_pct: float) -> int:
    """价格所在的格子：格子按 bin_pct% 等比划分，第 k 格是 [(1+b)^k, (1+b)^(k+1))。"""
    return math.floor(math.log(px) / math.log1p(bin_pct / 100))


def bin_edges(k: int, bin_pct: float) -> tuple[float, float]:
    g = math.log1p(bin_pct / 100)
    return math.exp(k * g), math.exp((k + 1) * g)


def bin_center(k: int, bin_pct: float) -> float:
    return math.exp((k + 0.5) * math.log1p(bin_pct / 100))


ABOVE = "above"   # 现价上方：空头的强平价
BELOW = "below"   # 现价下方：多头的强平价


@dataclass(frozen=True, slots=True)
class Zone:
    """爆仓密集区：一段连续的价格格子。"""
    side: str     # above / below
    lo: float     # 下沿
    hi: float     # 上沿
    peak: float   # 区里金额最大那一格的中心价
    usd: float    # 区里累计金额（美元）

    @property
    def near(self) -> float:
        """靠近现价的那条边。"""
        return self.lo if self.side == ABOVE else self.hi

    @property
    def far(self) -> float:
        return self.hi if self.side == ABOVE else self.lo

    def dist_pct(self, px: float) -> float:
        """现价到靠近的那条边的距离（%，正数）。"""
        return abs(self.near - px) / px * 100

    def contains(self, px: float) -> bool:
        return self.lo <= px <= self.hi


@dataclass(slots=True)
class Heatmap:
    """某一时刻的爆仓价位估算。"""
    px: float                                         # 现价
    bins: dict[int, float] = field(default_factory=dict)  # 格子编号 → 金额（美元），只含没被扫过的
    above: list[Zone] = field(default_factory=list)   # 现价上方的密集区，按离现价由近到远
    below: list[Zone] = field(default_factory=list)   # 现价下方的密集区，按离现价由近到远

    def zones(self, side: str) -> list[Zone]:
        return self.above if side == ABOVE else self.below

    def nearest(self, side: str) -> Zone | None:
        z = self.zones(side)
        return z[0] if z else None

    def top(self, side: str, n: int) -> list[Zone]:
        """金额最大的 n 个（同额时离现价近的在前）。"""
        return sorted(self.zones(side), key=lambda z: (-z.usd, z.dist_pct(self.px)))[:n]
