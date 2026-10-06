"""合成的 15 秒桶，用来检验回测工具本身（不是用来评价策略）。

- 平时：主动买、主动卖各自随机（对数正态），持仓量随机游走，价格随机游走（每 15 秒约 0.035%，接近 BTC 年化 50%）。
- 资金涌入：平均约半小时一次，持续 2–6 分钟，一边的主动成交放大 2–4 倍，持仓量跟着增加（新资金）。
- edge = 0：价格和资金完全无关（没有任何可赚的规律）。
- edge > 0：涌入期间价格每个桶朝涌入方向多走 edge（小数）；涌入结束后的 2 分钟每个桶再走 after
  （默认等于 edge，继续走；设成负数就是涌入结束后价格回吐）。分数在涌入后约 45–60 秒才到进场门槛。

分数（S）用真实的 ScoreEngine 算；不交易条件一律不触发。
"""
from __future__ import annotations

import math
import random

from flowmon.score import ScoreEngine

W = 15_000
T0 = 1_790_640_000_000  # 2026-09-29T00:00:00Z


def make_rows(cfg, hours: float, seed: int, edge: float = 0.0, vol: float = 0.00035,
              after: float | None = None) -> list[dict]:
    rng = random.Random(seed)
    se = ScoreEngine(cfg.score, cfg.bucket.width_s)
    n = int(hours * 3600 * 1000 / W)
    px, oi = 60000.0, 50000.0
    burst_left, burst_dir, burst_mult, left_after = 0, 0, 1.0, 0
    rows = []
    for i in range(n):
        if burst_left == 0 and rng.random() < 1 / 120:
            burst_left = rng.randint(8, 24)
            burst_dir = rng.choice((1, -1))
            burst_mult = rng.uniform(2, 4)
        buy = rng.lognormvariate(0.5, 0.6)
        sell = rng.lognormvariate(0.5, 0.6)
        doi = rng.gauss(0, 3)
        drift = 0.0
        if burst_left:
            if burst_dir > 0:
                buy *= burst_mult
            else:
                sell *= burst_mult
            doi += 10 * burst_mult
            drift = edge * burst_dir
            burst_left -= 1
            if burst_left == 0:
                left_after = 8
        elif left_after:
            drift = (edge if after is None else after) * burst_dir
            left_after -= 1
        oi += doi
        o = px
        px = o * math.exp(rng.gauss(0, vol) + drift)
        hi = max(o, px) * (1 + abs(rng.gauss(0, vol / 2)))
        lo = min(o, px) * (1 - abs(rng.gauss(0, vol / 2)))
        row = {"start_ms": T0 + i * W, "width_s": 15, "complete": True, "buy_vol": buy, "sell_vol": sell,
               "open": o, "high": hi, "low": lo, "close": px, "oi": oi,
               "bid1": round(px - 0.05, 2), "ask1": round(px + 0.05, 2), "no_trade": False}
        sc = se.update(row)
        row["S"], row["score_valid"] = sc.S, sc.valid
        rows.append(row)
    return rows
