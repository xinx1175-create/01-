"""资金流带：像布林带，但量的是买卖资金本身，不是价格。

布林带的中轨是价格的移动平均，带宽是价格的标准差，两样都要等价格走出来才变，天生滞后。
资金流带直接量「此刻谁在主动出手、出了多少」，读数里没有任何平均：

  读数   最近 1 分钟（成交方向窗口那几个桶）的净主动成交 X = 主动买 − 主动卖（BTC）。每 15 秒更新。
  中轨   0，买卖力量平衡。固定不动，没有滞后。
  带宽   σ = 正常情况下这个读数有多大：基准范围内（和力量分数同一个基准，最近 24 小时的有效数据）
         读数的均方根。σ 只是一把尺子的刻度，变得慢是应该的，它不决定什么时候动作。
  位置   z = X ÷ σ。内带 ±inner（默认 1），外带 ±outer（默认 2）。
         z = 2.5：主动买比正常多出 2.5 个「正常幅度」，在外带之外。

另外给一个价格噪声，用来定止损距离：最近 noise_window 里每 15 秒收益的均方根，折算到 noise_horizon。
用的是实际成交出来的价格，没有移动平均。

只用桶表里已有的列（buy_vol、sell_vol、close、complete），实时、回放、回测都能直接算。
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from .config import ScoreCfg
from .score import _Window


@dataclass(frozen=True)
class BandCfg:
    window_buckets: int          # 读数窗口：最近几个桶的净主动成交
    inner: float                 # 内带倍数
    outer: float                 # 外带倍数
    noise_window_minutes: float  # 价格噪声：用最近多久的 15 秒收益
    noise_horizon_minutes: float  # 折算到多长时间的噪声（止损要扛住的时间尺度）
    noise_min_coverage: float    # 噪声窗口里至少要有这么多比例的有效收益


@dataclass
class BandResult:
    x: float | None = None       # 读数：最近 1 分钟净主动成交（BTC）
    unit: float | None = None    # 带宽单位 σ（BTC）
    z: float | None = None       # 位置 = x / σ；基准不够时为空
    noise: float | None = None   # 价格噪声（小数，0.0015 = 0.15%）；数据不够时为空

    def as_row(self) -> dict:
        return {"band_x": self.x, "band_unit": self.unit, "band_z": self.z, "px_noise": self.noise}


class FlowBand:
    """逐桶喂入（按时间递增），逐桶给出读数。规则和力量分数一致：窗口不跨缺口，基准用最近 24 小时的有效数据。"""

    def __init__(self, score_cfg: ScoreCfg, band_cfg: BandCfg, width_s: int):
        self.cfg = band_cfg
        self.w = width_s * 1000
        self.n_base = max(1, round(score_cfg.baseline_hours * 3600 / width_s))
        look = max(self.n_base, round(score_cfg.baseline_lookback_hours * 3600 / width_s)) * self.w
        self.coverage = score_cfg.baseline_min_coverage
        self.past = _Window(look, self.n_base)       # 过去的读数 X（窗口完整时才有）
        self.first_start: int | None = None
        self.net: dict[int, float | None] = {}       # 最近几个桶的净主动成交；不完整的桶为 None
        self.noise_n = max(2, round(band_cfg.noise_window_minutes * 60 / width_s))
        self.noise_q: deque[tuple[int, float]] = deque()  # (桶起点, 15 秒对数收益的平方)
        self.noise_s2 = 0.0
        self.horizon = band_cfg.noise_horizon_minutes * 60 / width_s
        self.last_close: tuple[int, float] | None = None  # 上一个完整桶 (起点, 收盘)
        self.last_start: int | None = None

    def update(self, row) -> BandResult:
        s = int(row["start_ms"])
        if self.last_start is not None and s <= self.last_start:
            raise ValueError(f"桶必须按时间递增喂入：{s} <= {self.last_start}")
        self.last_start = s
        if self.first_start is None:
            self.first_start = s
        w = self.w
        complete = bool(row["complete"])
        buy, sell, close = row["buy_vol"], row["sell_vol"], row["close"]
        ok = complete and buy is not None and sell is not None
        self.net[s] = (buy - sell) if ok else None
        for t in [t for t in self.net if t <= s - self.cfg.window_buckets * w]:
            del self.net[t]

        res = BandResult()
        # 读数：窗口里每个桶都完整才算，不跨缺口
        win = [self.net.get(s - k * w) for k in range(self.cfg.window_buckets)]
        self.past.expire(s)
        if all(v is not None for v in win):
            res.x = math.fsum(win)
            self.past.add(s, res.x)
        ready = (self.first_start <= s - (self.n_base - 1) * w
                 and len(self.past) >= self.coverage * self.n_base)
        if ready and res.x is not None:
            res.unit = math.sqrt(self.past.s2 / len(self.past)) if len(self.past) else None
            if res.unit:
                res.z = res.x / res.unit

        # 价格噪声：相邻两个完整桶之间的对数收益
        cut = s - self.noise_n * w
        while self.noise_q and self.noise_q[0][0] <= cut:
            self.noise_s2 -= self.noise_q.popleft()[1]
        if complete and close:
            if self.last_close is not None and self.last_close[0] == s - w:
                r = math.log(close / self.last_close[1])
                self.noise_q.append((s, r * r))
                self.noise_s2 += r * r
            self.last_close = (s, close)
        if len(self.noise_q) >= self.cfg.noise_min_coverage * self.noise_n:
            res.noise = math.sqrt(max(0.0, self.noise_s2) / len(self.noise_q) * self.horizon)
        return res
