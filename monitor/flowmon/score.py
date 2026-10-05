"""力量分数（§6）。

不依赖实时连接：按时间顺序喂桶，逐桶给出结果。实时、回放、阶段二都用这一个类。
输入的桶至少要有 start_ms、complete、buy_vol、sell_vol、close、oi 这几个字段。
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Iterable

from .config import ScoreCfg

# 分数量程 ±100 是规格对分数的定义，不是待校准参数
SCORE_SCALE = 100.0


def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


@dataclass
class ScoreResult:
    F: float | None = None
    M: float | None = None
    A: float | None = None
    oi_chg: float | None = None
    Z: float | None = None
    B: float | None = None
    R_raw: float | None = None
    fix1: bool | None = None
    fix2: bool | None = None
    R: float | None = None
    price_chg: float | None = None
    S: float | None = None
    valid: bool = False
    note: str = ""

    def as_row(self) -> dict:
        return {
            "F": self.F, "M": self.M, "A": self.A, "oi_chg": self.oi_chg, "Z": self.Z, "B": self.B,
            "R_raw": self.R_raw, "fix1": self.fix1, "fix2": self.fix2, "R": self.R,
            "price_chg": self.price_chg, "S": self.S, "score_valid": self.valid,
            "score_note": self.note,
        }


class _Window:
    """按时间滑动的窗口，维护和与平方和；每装满一轮重算一次，避免浮点累积误差。"""

    def __init__(self, span_ms: int, resync_every: int):
        self.span = span_ms
        self.q: deque[tuple[int, float]] = deque()
        self.s = 0.0
        self.s2 = 0.0
        self.resync_every = max(1, resync_every)
        self.ops = 0

    def expire(self, now_start: int) -> None:
        cut = now_start - self.span
        q = self.q
        while q and q[0][0] <= cut:
            _, x = q.popleft()
            self.s -= x
            self.s2 -= x * x
            self.ops += 1
        if self.ops >= self.resync_every:
            self.s = math.fsum(x for _, x in q)
            self.s2 = math.fsum(x * x for _, x in q)
            self.ops = 0

    def add(self, t: int, x: float) -> None:
        self.q.append((t, x))
        self.s += x
        self.s2 += x * x

    def __len__(self) -> int:
        return len(self.q)

    def std(self) -> float | None:
        n = len(self.q)
        if n < 2:
            return None
        mean = self.s / n
        return math.sqrt(max(0.0, self.s2 / n - mean * mean))


class ScoreEngine:
    def __init__(self, cfg: ScoreCfg, width_s: int):
        self.cfg = cfg
        self.width_s = width_s
        self.w = width_s * 1000
        self.n_base = max(1, round(cfg.baseline_hours * 3600 / width_s))
        span = self.n_base * self.w
        self.vol = _Window(span, self.n_base)      # 完整桶的成交量
        self.doi = _Window(span, self.n_base)      # 持仓量变化量序列
        self.seen: deque[int] = deque()            # 喂进来的所有桶（含不完整）
        keep = max(cfg.flow_window_buckets, cfg.oi_window_buckets, cfg.smooth_buckets) + 1
        self.keep_ms = keep * self.w
        self.recent: dict[int, tuple[bool, float, float, float | None, float | None]] = {}
        self.r_hist: dict[int, tuple[float | None, bool]] = {}
        self.last_start: int | None = None

    def baseline_ready(self, s: int) -> bool:
        """基准值有效：历史铺满整个回看时长，且其中完整桶的占比够。"""
        if not self.seen or self.seen[0] > s - (self.n_base - 1) * self.w:
            return False
        return len(self.vol) >= self.cfg.baseline_min_coverage * self.n_base

    def update(self, row) -> ScoreResult:
        c = self.cfg
        s = int(row["start_ms"])
        if self.last_start is not None and s <= self.last_start:
            raise ValueError(f"桶必须按时间递增喂入：{s} <= {self.last_start}")
        self.last_start = s
        w = self.w

        # 窗口过期
        cut = s - self.n_base * w
        while self.seen and self.seen[0] <= cut:
            self.seen.popleft()
        self.vol.expire(s)
        self.doi.expire(s)
        for d in (self.recent, self.r_hist):
            for t in [t for t in d if t <= s - self.keep_ms]:
                del d[t]

        complete = bool(row["complete"])
        buy = row["buy_vol"] or 0.0
        sell = row["sell_vol"] or 0.0
        close = row["close"]
        oi = row["oi"]
        self.seen.append(s)
        self.recent[s] = (complete, buy, sell, close, oi)
        if complete:
            self.vol.add(s, buy + sell)

        res = ScoreResult()
        notes: list[str] = []
        if not complete:
            notes.append("incomplete")

        def ok(t):
            r = self.recent.get(t)
            return r is not None and r[0]

        # 1–3. 成交方向 F、成交量倍数 M、成交分项 A
        fw = [s - k * w for k in range(c.flow_window_buckets)]
        if all(ok(t) for t in fw):
            b = sum(self.recent[t][1] for t in fw)
            se = sum(self.recent[t][2] for t in fw)
            tot = b + se
            res.F = (b - se) / tot if tot > 0 else 0.0
            minutes = len(self.vol) * self.width_s / 60
            per_min = self.vol.s / minutes if minutes > 0 else 0.0
            if per_min > 0:
                win_min = c.flow_window_buckets * self.width_s / 60
                res.M = min(c.volume_multiple_cap, (tot / win_min) / per_min)
                res.A = _clip(res.F * res.M, -1.0, 1.0)
            else:
                notes.append("no_volume_baseline")
        else:
            notes.append("flow_window_gap")

        # 4–5. 持仓量变化 Z、持仓分项 B
        t0 = s - c.oi_window_buckets * w
        if complete and oi is not None and ok(t0) and self.recent[t0][4] is not None:
            res.oi_chg = oi - self.recent[t0][4]
            self.doi.add(s, res.oi_chg)
            sd = self.doi.std()
            if sd and sd > 0:
                res.Z = res.oi_chg / sd
                if res.F is not None:
                    res.B = _clip(max(res.Z, 0.0) / c.oi_z_divisor, 0.0, 1.0) * _sign(res.F)
            else:
                notes.append("oi_std_zero")
        else:
            notes.append("oi_window_gap")

        # 价格变化：成交方向窗口起点之前那个桶的收盘 → 当前收盘
        tp = s - c.flow_window_buckets * w
        if ok(tp) and self.recent[tp][3] is not None and close is not None:
            res.price_chg = close - self.recent[tp][3]
        else:
            notes.append("price_window_gap")

        # 6–8. 原始分与两条修正
        if res.A is not None and res.B is not None and res.price_chg is not None:
            r = SCORE_SCALE * (c.weight_flow * res.A + c.weight_oi * res.B)
            res.R_raw = r
            res.fix1 = res.oi_chg <= 0
            if res.fix1:
                r = _clip(r, -c.no_new_money_cap, c.no_new_money_cap)
            res.fix2 = res.price_chg != 0 and r != 0 and _sign(res.price_chg) != _sign(r)
            if res.fix2:
                r *= c.price_disagree_factor
            res.R = r

        ready = self.baseline_ready(s)
        if not ready:
            notes.append("warmup")
        r_valid = res.R is not None and ready and complete
        self.r_hist[s] = (res.R, r_valid)

        # 9. 平滑
        sw = [s - k * w for k in range(c.smooth_buckets)]
        rs = [self.r_hist.get(t) for t in sw]
        if all(x is not None and x[0] is not None for x in rs):
            res.S = sum(x[0] for x in rs) / len(rs)
            res.valid = all(x[1] for x in rs)
            if not res.valid and not notes:
                notes.append("smooth_window_invalid")
        elif not notes:
            notes.append("smooth_window_gap")
        res.note = "|".join(dict.fromkeys(notes))
        return res


def score_series(rows: Iterable, cfg: ScoreCfg, width_s: int) -> list[ScoreResult]:
    """离线入口：输入一段按时间排好的历史桶，输出逐桶分数。阶段二直接调用。"""
    eng = ScoreEngine(cfg, width_s)
    return [eng.update(r) for r in rows]
