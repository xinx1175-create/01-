"""回放工具：读已保存的逐笔成交、盘口快照、持仓量等原始数据，重新生成桶和信号事件。

用途：改桶宽、改分数算法或参数之后重算；也用来核对实时结果。
输出写到单独目录，不会覆盖实时数据。

和实时结果的已知差别：
- 撤单量需要完整的盘口增量，原始数据里没有存，回放时为空。
- 近处挂单只能用存下的前 N 档算，范围铺不满时偏小（near_truncated=1）。
- 数据延迟只用逐笔成交算（实时还包含盘口推送）。
- 完整性沿用实时记录：实时判为不完整的时段，回放也判为不完整；实时没在运行的时段直接跳过。
- 实时里封桶后才到的成交（late_trades）回放时会算进它本该在的桶。
"""
from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterator

from .bucket import Bucket, capture_from_levels, day_of, pick_oi
from .conditions import Calendar, Conditions
from .config import Config
from .events import EventEngine, public
from .okx import contract_size
from .schema import BUCKET_COLUMNS, event_columns
from .score import ScoreEngine
from .storage import DailyCsv, day_files, load_json, read_csv, read_jsonl

log = logging.getLogger(__name__)


def days_between(a: str, b: str) -> list[str]:
    d0, d1 = date.fromisoformat(a), date.fromisoformat(b)
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


@dataclass
class Recorded:
    """实时记录过的一个桶：时间范围和完整性。"""
    s: int
    e: int
    ok: bool
    why: str


def _iter_trades(data: Path, days: list[str]) -> Iterator[tuple[int, int, float, float, str, int]]:
    for d in days:
        rows = []
        for r in read_csv(day_files(data / "raw" / "trades", [d], ".csv")):
            try:
                rows.append((int(r["ts"]), int(r["recv"]), float(r["px"]), float(r["sz"]), r["side"],
                             int(r["count"] or 1)))
            except (KeyError, ValueError):
                continue  # 崩溃时写了半行
        rows.sort(key=lambda x: x[0])
        yield from rows


class Replayer:
    def __init__(self, cfg: Config, src_data: Path, out_dir: Path):
        self.cfg = cfg
        self.src = src_data
        self.out = out_dir
        meta = load_json(src_data / "meta" / "instrument.json")
        if not meta:
            raise RuntimeError(f"{src_data}/meta/instrument.json 不存在，无法换算张数")
        self.ct = contract_size(meta["instrument"])
        self.w = cfg.bucket.width_s * 1000

    def run(self, day_from: str, day_to: str) -> dict:
        cfg, w = self.cfg, self.w
        days = days_between(day_from, day_to)
        misc = list(read_jsonl(day_files(self.src / "raw" / "misc", days, ".jsonl")))
        recorded = sorted((Recorded(m["s"], m["s"] + m["w"] * 1000, bool(m["ok"]), m.get("why") or "")
                           for m in misc if m.get("type") == "bucket"), key=lambda r: r.s)
        if not recorded:
            raise RuntimeError("这段日期里没有桶记录（raw/misc 里 type=bucket），无法判断哪些时段实时在运行")
        rec_starts = [r.s for r in recorded]
        oi = sorted((int(m["ts"]), float(m["oi"])) for m in misc if m.get("type") == "oi")
        funding = sorted((int(m["ts"]), float(m["rate"])) for m in misc if m.get("type") == "funding")
        liq = sorted((int(m["ts"]), m.get("pos"), m.get("side"), float(m["sz"]))
                     for m in misc if m.get("type") == "liq")
        # 盘口快照按时间顺序逐条读，不整份装进内存
        book_iter = read_jsonl(day_files(self.src / "raw" / "books", days, ".jsonl"))
        book_next = next(book_iter, None)
        book_last = None

        score = ScoreEngine(cfg.score, cfg.bucket.width_s)
        c = cfg.conditions
        cal = Calendar(cfg.path(c.calendar_file), c.calendar_before_minutes, c.calendar_after_minutes)
        cond = Conditions(c, cfg.bucket.width_s, cal)
        events = EventEngine(cfg, self.ct)
        ev_cols = [n for n, _ in event_columns(cfg)]
        w_b = DailyCsv(self.out / "buckets", [n for n, _ in BUCKET_COLUMNS])
        w_e = DailyCsv(self.out / "events", ev_cols)
        stats = {"buckets": 0, "skipped": 0, "events": 0, "trades": 0}
        prev_close = None
        li = 0

        def coverage(s: int, e: int) -> tuple[bool, set[str]] | None:
            """新桶 [s, e) 在实时记录里的情况：None = 实时没在运行，跳过。"""
            i = bisect.bisect_right(rec_starts, s) - 1
            i = max(i, 0)
            covered = 0
            ok = True
            why: set[str] = set()
            while i < len(recorded) and recorded[i].s < e:
                r = recorded[i]
                lo, hi = max(r.s, s), min(r.e, e)
                if hi > lo:
                    covered += hi - lo
                    if not r.ok:
                        ok = False
                        why |= {x for x in r.why.split("|") if x}
                i += 1
            if covered == 0:
                return None
            if covered < e - s:
                ok = False
                why.add("not_recorded")
            return ok, why

        def emit(b: Bucket) -> None:
            nonlocal prev_close, li, book_next, book_last
            s, e = b.start_ms, b.end_ms
            cov = coverage(s, e)
            while book_next is not None and int(book_next["t"]) <= e:
                book_last = book_next
                book_next = next(book_iter, None)
            while li < len(liq) and liq[li][0] < e:
                ts, pos, side, sz = liq[li]
                if ts >= s:
                    long_side = pos == "long" if pos in ("long", "short") else side == "sell"
                    if long_side:
                        b.liq_long_ct += sz
                    else:
                        b.liq_short_ct += sz
                li += 1
            if cov is None:
                if b.close is not None:
                    prev_close = b.close
                stats["skipped"] += 1
                return
            ok, why = cov
            b.reasons = set() if ok else (why or {"incomplete"})
            if book_last is not None and int(book_last["t"]) > s:
                bk = book_last
                b.capture = capture_from_levels(bk["bids"], bk["asks"], bk.get("ts"), bk.get("seq"),
                                                cfg.bucket)
            b.cancel_tracked = False
            k = bisect.bisect_left(funding, (e, float("-inf")))
            fr = funding[k - 1][1] if k > 0 else None
            row = b.finish(prev_close, pick_oi(oi, e), fr, self.ct, int(cfg.bucket.oi_stale_s * 1000),
                           judge=False)
            if row["close"] is not None:
                prev_close = row["close"]
            sc = score.update(row)
            cd = cond.update(row, sc.valid, sc.S)
            full = {**row, **sc.as_row(), **cd}
            w_b.write(day_of(s), full)
            _, done = events.on_bucket(full, sc, cd, b.capture)
            for ev in done:
                w_e.write(day_of(ev["ts_ms"]), public(ev, ev_cols))
                stats["events"] += 1
            stats["buckets"] += 1

        first = recorded[0].s - recorded[0].s % w
        last_end = recorded[-1].e
        cur = first
        b = Bucket(cur, w)
        for ts, recv, px, sz, side, count in _iter_trades(self.src, days):
            if ts < first:
                continue
            if ts >= last_end:
                break
            while ts >= cur + w:
                emit(b)
                cur += w
                b = Bucket(cur, w)
            b.add_trade(ts, px, sz, side, count)
            b.latencies.append(recv - ts)
            events.on_trade(ts, px, sz, side)
            stats["trades"] += 1
        while cur < last_end:
            emit(b)
            cur += w
            b = Bucket(cur, w)
        for ev in events.flush_all():
            w_e.write(day_of(ev["ts_ms"]), public(ev, ev_cols))
            stats["events"] += 1
        w_b.close()
        w_e.close()
        return stats


def compare(cfg: Config, live_data: Path, replay_out: Path, days: list[str]) -> dict:
    """逐桶比对实时与回放的分数、逐条比对事件，返回差异统计。桶宽不同时没有意义。"""
    bt = dict(BUCKET_COLUMNS)
    live = {r["start_ms"]: r for r in read_csv(day_files(live_data / "buckets", days, ".csv"), bt)}
    rep = {r["start_ms"]: r for r in read_csv(day_files(replay_out / "buckets", days, ".csv"), bt)}
    common = sorted(set(live) & set(rep))
    diff_s = []
    for t in common:
        a, b = live[t], rep[t]
        if a["score_valid"] != b["score_valid"] or a["complete"] != b["complete"]:
            diff_s.append(t)
        elif a["S"] is not None and b["S"] is not None and abs(a["S"] - b["S"]) > 1e-6:
            diff_s.append(t)
        elif (a["S"] is None) != (b["S"] is None):
            diff_s.append(t)
    et = dict(event_columns(cfg))
    le = {r["event_id"] for r in read_csv(day_files(live_data / "events", days, ".csv"), et)}
    re_ = {r["event_id"] for r in read_csv(day_files(replay_out / "events", days, ".csv"), et)}
    return {
        "buckets_live": len(live), "buckets_replay": len(rep), "buckets_common": len(common),
        "score_mismatch": len(diff_s), "first_mismatch": diff_s[:5],
        "events_live": len(le), "events_replay": len(re_),
        "events_only_live": sorted(le - re_)[:10], "events_only_replay": sorted(re_ - le)[:10],
    }
