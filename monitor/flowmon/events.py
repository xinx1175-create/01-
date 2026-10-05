"""信号事件（§10）与对照组。

信号时刻取触发那个桶的结束时间。事件建好后逐桶跟踪，跟满 followup_minutes 才落盘。
限价单是否成交用信号之后的逐笔成交判断：价格穿过挂单价算成交；
刚好在挂单价上成交的量超过当时排在前面的量，也算成交（不计前面有人撤单，偏保守）。
"""
from __future__ import annotations

import random
from collections import deque

from .bucket import BookCapture, iso_utc
from .config import Config
from .orderbook import walk_fill
from .schema import event_columns, threshold_col
from .score import ScoreResult

HOUR_MS = 3_600_000


def limit_filled(trades, start: int, end: int, direction: int, px: float, queue_ct: float) -> bool:
    vol = 0.0
    want = "sell" if direction > 0 else "buy"  # 挂买单等主动卖，挂卖单等主动买
    for ts, tpx, sz, side in trades:
        if ts < start or ts >= end or side != want:
            continue
        if (tpx < px) if direction > 0 else (tpx > px):
            return True
        if tpx == px:
            vol += sz
            if vol > queue_ct:
                return True
    return False


class EventEngine:
    def __init__(self, cfg: Config, ct_val: float):
        self.cfg = cfg
        self.ev = cfg.events
        self.ru = cfg.rules
        self.ct_val = ct_val
        self.w = cfg.bucket.width_s * 1000
        self.follow_ms = int(self.ev.followup_minutes * 60_000)
        self.columns = [c for c, _ in event_columns(cfg)]
        keep = max(self.ev.limit_fill_windows_s) * 1000 + 2 * self.w
        self.trade_keep_ms = keep
        self.trades: deque[tuple[int, float, float, str]] = deque()
        self.pending: list[dict] = []
        self.prev: tuple[int, float] | None = None  # 上一个桶的有效分数
        self.ctrl_hour: int | None = None
        self.ctrl_slots: list[tuple[int, int]] = []   # [(桶序号, 方向)]
        self.ctrl_done = 0
        self.last_end: int | None = None

    # ---------- 输入 ----------

    def on_trade(self, ts: int, px: float, sz_ct: float, side: str) -> None:
        self.trades.append((ts, px, sz_ct, side))

    def on_bucket(self, row: dict, score: ScoreResult, cond: dict,
                  cap: BookCapture | None) -> tuple[list[dict], list[dict]]:
        """返回 (本桶新建的事件, 本桶跟踪完毕可以落盘的事件)。"""
        s = row["start_ms"]
        end = s + self.w
        if self.last_end is not None and s != self.last_end:
            for e in self.pending:  # 中间缺了桶（停机），跟踪数据不全
                e["followup_complete"] = False
        self.last_end = end
        done = self._follow(row, score, end)
        created: list[dict] = []

        if score.valid:
            prev = self.prev[1] if self.prev and self.prev[0] == s - self.w else None
            if prev is not None:
                S = score.S
                for tier in self.ev.tiers:
                    if prev < tier <= S:
                        created.append(self._new("signal", 1, tier, row, score, cond, cap, end))
                    if prev > -tier >= S:
                        created.append(self._new("signal", -1, tier, row, score, cond, cap, end))
            self.prev = (s, score.S)
        else:
            self.prev = None

        ctrl = self._control(row, score, created, cond, cap, end)
        if ctrl is not None:
            created.append(ctrl)
        self.pending.extend(created)

        cut = end - self.trade_keep_ms
        while self.trades and self.trades[0][0] < cut:
            self.trades.popleft()
        return created, done

    # ---------- 建事件 ----------

    def _new(self, kind: str, d: int, tier, row, score: ScoreResult, cond, cap: BookCapture | None,
             ts: int) -> dict:
        e = {c: None for c in self.columns}
        e.update({
            "event_id": f"{kind}-{ts}-{'L' if d > 0 else 'S'}" + (f"-{tier:g}" if tier is not None else ""),
            "kind": kind, "time_utc": iso_utc(ts), "ts_ms": ts, "direction": d, "tier": tier,
            "S": score.S, "F": score.F, "M": score.M, "A": score.A, "Z": score.Z, "B": score.B,
            "R": score.R, "price": row["close"],
            "no_trade": cond["no_trade"], "no_trade_reason": cond["no_trade_reason"],
            "latency_ms": row["latency_ms"], "followup_complete": True,
        })
        if cap is not None:
            mid = (cap.bid1 + cap.ask1) / 2
            e["bid1"], e["ask1"] = cap.bid1, cap.ask1
            fill = walk_fill(cap.asks if d > 0 else cap.bids, self.ev.market_order_notional_usdt, self.ct_val)
            e["mkt_fill_px"] = fill
            if fill is not None:
                e["mkt_slip_bps"] = (fill - mid) / mid * 10_000 * d
            e["limit_px"] = cap.bid1 if d > 0 else cap.ask1
            e["limit_queue"] = (cap.bid1_sz if d > 0 else cap.ask1_sz) * self.ct_val
            e["_queue_ct"] = cap.bid1_sz if d > 0 else cap.ask1_sz
        # 跟踪用的内部字段，以下划线开头，不落盘
        e["_best_fav"] = None
        e["_best_adv"] = None
        e["_limit_left"] = list(self.ev.limit_fill_windows_s) if cap is not None else []
        return e

    def _control(self, row, score, created, cond, cap, end) -> dict | None:
        n = self.ev.control_per_hour
        if n <= 0:
            return None
        s = row["start_ms"]
        hour = s - s % HOUR_MS
        if hour != self.ctrl_hour:
            # 种子含小时起点：同一份数据回放时抽到同一批时刻
            rng = random.Random(f"{self.ev.control_seed}:{hour}")
            per_hour = HOUR_MS // self.w
            slots = sorted(rng.sample(range(per_hour), min(n, per_hour)))
            self.ctrl_slots = [(i, rng.choice((1, -1))) for i in slots]
            self.ctrl_hour = hour
            self.ctrl_done = 0
        if self.ctrl_done >= len(self.ctrl_slots):
            return None
        idx, d = self.ctrl_slots[self.ctrl_done]
        if (s - hour) // self.w < idx:
            return None
        # 到点了，但要求这一刻数据完整、分数有效、没有信号；不满足就顺延到本小时内下一个桶
        if not row["complete"] or not score.valid or any(e["kind"] == "signal" for e in created):
            return None
        self.ctrl_done += 1
        return self._new("control", d, None, row, score, cond, cap, end)

    # ---------- 跟踪 ----------

    def _follow(self, row: dict, score: ScoreResult, end: int) -> list[dict]:
        done, keep = [], []
        for e in self.pending:
            E, d, p = e["ts_ms"], e["direction"], e["price"]
            if end <= E:
                keep.append(e)
                continue
            if not row["complete"]:
                e["followup_complete"] = False
            for h in self.ev.price_horizons_s:
                if end == E + h * 1000:
                    e[f"px_{h}s"] = row["close"]
            if p and row["high"] is not None:
                hi, lo = row["high"], row["low"]
                hi_t = row["high_ms"] if row["high_ms"] is not None else end
                lo_t = row["low_ms"] if row["low_ms"] is not None else end
                fav, fav_t, adv, adv_t = ((hi - p, hi_t, p - lo, lo_t) if d > 0 else (p - lo, lo_t, hi - p, hi_t))
                if e["_best_fav"] is None or fav > e["_best_fav"]:
                    e["_best_fav"] = fav
                    e["mfe_pct"] = fav / p * 100
                    e["mfe_after_s"] = (fav_t - E) / 1000
                if e["_best_adv"] is None or adv > e["_best_adv"]:
                    e["_best_adv"] = adv
                    e["mae_pct"] = adv / p * 100
                    e["mae_after_s"] = (adv_t - E) / 1000
            if score.valid:
                ds = d * score.S
                t = (end - E) / 1000
                if e["max_score"] is None or ds > e["max_score"]:
                    e["max_score"], e["max_score_after_s"] = ds, t
                for a in self.ru.add_thresholds:
                    k = threshold_col("ge", a)
                    if e[k] is None and ds >= a:
                        e[k] = t
                for v in (self.ru.halve_threshold, self.ru.exit_threshold):
                    k = threshold_col("lt", v)
                    if e[k] is None and ds < v:
                        e[k] = t
            left = []
            for win in e["_limit_left"]:
                if end >= E + win * 1000:
                    e[f"limit_fill_{win}s"] = limit_filled(self.trades, E, E + win * 1000, d,
                                                           e["limit_px"], e["_queue_ct"])
                else:
                    left.append(win)
            e["_limit_left"] = left
            if end >= E + self.follow_ms:
                done.append(e)
            else:
                keep.append(e)
        self.pending = keep
        return done

    # ---------- 重启恢复 ----------

    def state(self) -> dict:
        return {"pending": self.pending, "prev": self.prev, "ctrl_hour": self.ctrl_hour,
                "ctrl_slots": self.ctrl_slots, "ctrl_done": self.ctrl_done, "last_end": self.last_end}

    def load_state(self, st: dict) -> None:
        """重启后接着跟踪没跟完的事件。停机那段缺的桶由 on_bucket 的断档检查标为跟踪不完整；
        逐笔成交缓存没有存盘，还没判定的限价单窗口记为空（未知）。"""
        for e in st.get("pending", []):
            e["_limit_left"] = []
            self.pending.append(e)
        prev = st.get("prev")
        self.prev = tuple(prev) if prev else None
        self.ctrl_hour = st.get("ctrl_hour")
        self.ctrl_slots = [tuple(x) for x in st.get("ctrl_slots", [])]
        self.ctrl_done = st.get("ctrl_done", 0)
        self.last_end = st.get("last_end")

    def flush_all(self) -> list[dict]:
        """停机时把没跟完的事件按现状落盘（followup_complete=0）。"""
        out = self.pending
        for e in out:
            e["followup_complete"] = False
        self.pending = []
        return out


def public(e: dict, columns: list[str]) -> dict:
    return {c: e.get(c) for c in columns}
