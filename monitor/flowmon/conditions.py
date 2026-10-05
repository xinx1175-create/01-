"""不交易条件（§8）里能由行情判断的几条：低波动、分数来回翻、经济数据前后、数据不完整。

「连续亏损暂停」和风控表依赖模拟成交的盈亏，阶段一没有仓位，留给阶段二回放判断。
"""
from __future__ import annotations

import bisect
import csv
import logging
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from .config import ConditionsCfg

log = logging.getLogger(__name__)


def _parse_utc(s: str) -> int:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


class Calendar:
    """手工维护的经济数据日程。CSV 两列：time_utc,name。文件改了会自动重读。"""

    def __init__(self, path: Path | None, before_min: float, after_min: float):
        self.path = path
        self.before = int(before_min * 60_000)
        self.after = int(after_min * 60_000)
        self.items: list[tuple[int, str]] = []
        self._mtime: float | None = None
        self.reload()

    def reload(self) -> None:
        if self.path is None or not self.path.exists():
            if self._mtime is not None or self.items:
                log.warning("经济数据日程文件不存在：%s", self.path)
            self.items, self._mtime = [], None
            return
        m = self.path.stat().st_mtime
        if m == self._mtime:
            return
        items = []
        with self.path.open(encoding="utf-8") as f:
            for i, row in enumerate(csv.DictReader(f)):
                try:
                    items.append((_parse_utc(row["time_utc"]), (row.get("name") or "").strip()))
                except Exception as e:  # 一行写错不影响其它行
                    log.error("日程文件第 %d 行无法解析：%s (%s)", i + 2, row, e)
        items.sort()
        self.items, self._mtime = items, m
        log.info("读入经济数据日程 %d 条", len(items))

    def active(self, t_ms: int) -> str | None:
        # 落在 [公布时间 − before, 公布时间 + after] 内
        i = bisect.bisect_left(self.items, (t_ms - self.after, ""))
        for ts, name in self.items[i:]:
            if ts - self.before > t_ms:
                break
            return name or "event"
        return None


class _MonoMax:
    """滑动窗口最大值（单调队列）。"""

    def __init__(self, sign: int):
        self.sign = sign
        self.q: deque[tuple[int, float]] = deque()

    def push(self, t: int, x: float) -> None:
        v = x * self.sign
        while self.q and self.q[-1][1] <= v:
            self.q.pop()
        self.q.append((t, v))

    def expire(self, cut: int) -> None:
        while self.q and self.q[0][0] <= cut:
            self.q.popleft()

    def value(self) -> float | None:
        return self.q[0][1] * self.sign if self.q else None


class Conditions:
    def __init__(self, cfg: ConditionsCfg, width_s: int, calendar: Calendar):
        self.cfg = cfg
        self.w = width_s * 1000
        self.cal = calendar
        self.range_ms = int(round(cfg.low_vol_window_minutes * 60 / width_s)) * self.w
        self.hist_ms = int(cfg.low_vol_lookback_days * 86_400_000)
        self.flip_ms = int(round(cfg.flip_window_minutes * 60 / width_s)) * self.w
        self.hi = _MonoMax(1)
        self.lo = _MonoMax(-1)
        self.seen: deque[int] = deque()
        self.hist: deque[tuple[int, float]] = deque()
        self.sorted: list[float] = []
        self.signs: deque[tuple[int, int]] = deque()

    def update(self, row: dict, score_valid: bool, S: float | None) -> dict:
        s = int(row["start_ms"])
        end = s + self.w
        complete = bool(row["complete"])

        # 低波动：最近窗口的 (最高 − 最低) / 收盘，在过去若干天同类数值里的分位
        cut = s - self.range_ms
        while self.seen and self.seen[0] <= cut:
            self.seen.popleft()
        self.seen.append(s)
        self.hi.expire(cut)
        self.lo.expire(cut)
        if complete and row["high"] is not None:
            self.hi.push(s, row["high"])
            self.lo.push(s, row["low"])
        rng = rank = hist_h = None
        span_ok = self.seen[0] <= s - self.range_ms + self.w
        h, l = self.hi.value(), self.lo.value()
        if span_ok and h is not None and complete and row["close"]:
            rng = (h - l) / row["close"] * 100
            hcut = s - self.hist_ms
            while self.hist and self.hist[0][0] <= hcut:
                _, old = self.hist.popleft()
                del self.sorted[bisect.bisect_left(self.sorted, old)]
            if self.sorted:
                rank = bisect.bisect_left(self.sorted, rng) / len(self.sorted) * 100
                hist_h = (s - self.hist[0][0] + self.w) / 3_600_000
            self.hist.append((s, rng))
            bisect.insort(self.sorted, rng)
        nt_low = rank is not None and rank < self.cfg.low_vol_percentile

        # 分数正负翻转次数
        fcut = s - self.flip_ms
        while self.signs and self.signs[0][0] <= fcut:
            self.signs.popleft()
        if score_valid and S:
            self.signs.append((s, 1 if S > 0 else -1))
        q = self.signs
        flips = sum(1 for i in range(1, len(q)) if q[i][1] != q[i - 1][1])
        nt_flips = flips >= self.cfg.flip_count

        # 经济数据：按信号时刻（桶结束）判断
        ev = self.cal.active(end)
        nt_data = not complete

        reasons = []
        if nt_low:
            reasons.append("low_vol")
        if nt_flips:
            reasons.append("flips")
        if ev:
            reasons.append(f"calendar:{ev}")
        if nt_data:
            reasons.append("data")
        return {
            "range_pct": rng, "range_rank": rank, "range_hist_h": hist_h, "flips": flips,
            "nt_low_vol": nt_low, "nt_flips": nt_flips, "nt_calendar": ev is not None,
            "nt_data": nt_data, "no_trade": bool(reasons), "no_trade_reason": "|".join(reasons),
        }
