"""爆仓模块的落盘：data/liq/ 下按天（UTC）分文件，每类一个目录，写入时去重。

  liqs/YYYY-MM-DD.csv        爆仓单（三个币一起，inst 列区分）
  candles_1h/YYYY-MM-DD.csv  1 小时 K 线（已收盘）
  candles_1m/YYYY-MM-DD.csv  1 分钟 K 线（已收盘）
  oi_1h/YYYY-MM-DD.csv       交易所小时统计的持仓量（币）
  oi_live/YYYY-MM-DD.csv     自己录的实时持仓量（币）
  ratio_1h/YYYY-MM-DD.csv    多空人数比（按币种；inst 列写合约名）
  sec/YYYY-MM-DD.csv         1 秒 K 线（自己用逐笔成交合成）
  hourly/YYYY-MM-DD.csv      每小时汇总（实时写，给人看）
  signals/YYYY-MM-DD.csv     信号（实时写）
  meta/instruments.json      合约信息
  state/collector.json       采集进度：REST 覆盖区间、回补到哪里
  state/liq.lock             进程锁：同一个数据目录只允许一个采集者

同一条记录（爆仓单按时间戳、方向、价格、数量；其余按合约 + 时间）只写一次，先写的为准。
所有写入都在同一个线程里做（监控器的事件循环），读可以在别的线程，读之前先 flush。
"""
from __future__ import annotations

import fcntl
import logging
import os
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ..storage import DailyCsv, day_files, load_json, read_csv, save_json
from .types import DAY_MS, Candle, InstData, Liq, Point, SecBar, merge_intervals

log = logging.getLogger(__name__)

LIQ_COLUMNS = ["ts", "inst", "side", "bk_px", "sz", "qty", "usd", "src", "recv"]
CANDLE_COLUMNS = ["ts", "inst", "o", "h", "l", "c", "vol", "vol_ccy", "vol_quote", "seen"]
POINT_COLUMNS = ["ts", "inst", "value", "seen"]
SEC_COLUMNS = ["ts", "inst", "o", "h", "l", "c", "vol"]

_TYPES = {"ts": int, "inst": str, "side": str, "bk_px": float, "sz": float, "qty": float, "usd": float,
          "src": str, "recv": int, "o": float, "h": float, "l": float, "c": float, "vol": float,
          "vol_ccy": float, "vol_quote": float, "seen": int, "value": float}

# 种类 → (子目录, 列, 写入时是否查重)。oi_live 和 sec 只由实时数据按时间顺序写，不查重（只挡住时间倒退的）
KINDS: dict[str, tuple[str, list[str], bool]] = {
    "liq": ("liqs", LIQ_COLUMNS, True),
    "c1h": ("candles_1h", CANDLE_COLUMNS, True),
    "c1m": ("candles_1m", CANDLE_COLUMNS, True),
    "oi1h": ("oi_1h", POINT_COLUMNS, True),
    "ratio1h": ("ratio_1h", POINT_COLUMNS, True),
    "oilive": ("oi_live", POINT_COLUMNS, False),
    "sec": ("sec", SEC_COLUMNS, False),
}
CANDLE_KINDS = ("c1h", "c1m")
POINT_KINDS = ("oi1h", "ratio1h", "oilive")


class LiqBusy(RuntimeError):
    """另一个进程（监控器或回补命令）正在写 data/liq。"""


def day_of(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date().isoformat()


def _days(t0: int, t1: int) -> list[str]:
    """覆盖 [t0, t1) 的每一天。"""
    out = []
    t = t0 - t0 % DAY_MS
    while t < t1:
        out.append(day_of(t))
        t += DAY_MS
    return out


def _to_row(kind: str, r) -> dict:
    if kind == "liq":
        return {"ts": r.ts, "inst": r.inst, "side": r.side, "bk_px": r.bk_px, "sz": r.sz, "qty": r.qty,
                "usd": r.usd, "src": r.src, "recv": r.recv}
    if kind in CANDLE_KINDS:
        return {"ts": r.ts, "inst": r.inst, "o": r.o, "h": r.h, "l": r.l, "c": r.c, "vol": r.vol,
                "vol_ccy": r.vol_ccy, "vol_quote": r.vol_quote, "seen": r.seen}
    if kind in POINT_KINDS:
        return {"ts": r.ts, "inst": r.inst, "value": r.value, "seen": r.seen}
    return {"ts": r.ts, "inst": r.inst, "o": r.o, "h": r.h, "l": r.l, "c": r.c, "vol": r.vol}


def _from_row(kind: str, d: dict):
    """读回一行；缺了必要字段返回 None（崩溃时写了半行）。"""
    try:
        if kind == "liq":
            r = Liq(ts=d["ts"], inst=d["inst"], side=d["side"], bk_px=d["bk_px"], sz=d["sz"], qty=d["qty"],
                    usd=d["usd"], src=d["src"], recv=d["recv"] if d["recv"] is not None else 0)
            ok = None not in (r.bk_px, r.sz, r.qty, r.usd) and r.side in ("long", "short")
        elif kind in CANDLE_KINDS:
            r = Candle(inst=d["inst"], ts=d["ts"], o=d["o"], h=d["h"], l=d["l"], c=d["c"], vol=d["vol"] or 0.0,
                       vol_ccy=d["vol_ccy"] or 0.0, vol_quote=d["vol_quote"] or 0.0, seen=d["seen"])
            ok = None not in (r.o, r.h, r.l, r.c)
        elif kind in POINT_KINDS:
            r = Point(inst=d["inst"], ts=d["ts"], value=d["value"], seen=d["seen"])
            ok = r.value is not None
        else:
            r = SecBar(inst=d["inst"], ts=d["ts"], o=d["o"], h=d["h"], l=d["l"], c=d["c"], vol=d["vol"] or 0.0)
            ok = None not in (r.o, r.h, r.l, r.c)
    except KeyError:
        return None
    return r if ok and r.ts is not None and r.inst else None


def _key(kind: str, r):
    return r.key if kind == "liq" else r.ts


class LiqStore:
    def __init__(self, root: Path, sec_cache_days: int = 2):
        self.root = Path(root)
        self._w: dict[str, DailyCsv] = {}
        # (种类, 日期, 合约) → 这一天这个合约已经写过的键（第一次写这一天时从硬盘读回）
        self._keys: dict[tuple[str, str, str], set] = {}
        self._last: dict[tuple[str, str], int] = {}       # oi_live、sec：每个合约最后写入的时间
        self._sec_cache: OrderedDict[str, dict[str, list[SecBar]]] = OrderedDict()
        self._sec_cache_days = sec_cache_days
        self._lock_f = None

    # ---------- 写 ----------

    def _writer(self, kind: str) -> DailyCsv:
        w = self._w.get(kind)
        if w is None:
            sub, cols, _ = KINDS[kind]
            w = self._w[kind] = DailyCsv(self.root / sub, cols)
        return w

    def _day_keys(self, kind: str, day: str, inst: str) -> set:
        k = (kind, day, inst)
        s = self._keys.get(k)
        if s is None:
            # 读回这一天已经写过的；同一天其它合约的键也顺便建好，免得每个合约各读一遍
            self.flush()
            by_inst: dict[str, set] = {}
            for r in self._read_day(kind, day):
                by_inst.setdefault(r.inst, set()).add(_key(kind, r))
            for i, keys in by_inst.items():
                self._keys.setdefault((kind, day, i), keys)
            s = self._keys.setdefault(k, set())
        return s

    def add(self, kind: str, rows: Iterable) -> list:
        """写入，返回真正新写的那些（重复的跳过）。"""
        _, _, dedupe = KINDS[kind]
        w = self._writer(kind)
        out = []
        for r in rows:
            day = day_of(r.ts)
            if dedupe:
                keys = self._day_keys(kind, day, r.inst)
                k = _key(kind, r)
                if k in keys:
                    continue
                keys.add(k)
            else:
                last = self._last.get((kind, r.inst))
                if last is not None and r.ts <= last:
                    continue
                self._last[(kind, r.inst)] = r.ts
            w.write(day, _to_row(kind, r))
            out.append(r)
        return out

    def forget_before(self, ts: int) -> None:
        """不再需要查重的旧日期（早于 ts）的键从内存里丢掉。"""
        cut = day_of(ts)
        for k in [k for k in self._keys if k[1] < cut]:
            del self._keys[k]

    def flush(self) -> None:
        for w in self._w.values():
            w.flush()

    def close(self) -> None:
        for w in self._w.values():
            w.close()
        self._w.clear()

    # ---------- 读 ----------

    def _read_day(self, kind: str, day: str) -> Iterable:
        sub, cols, _ = KINDS[kind]
        types = {c: _TYPES[c] for c in cols}
        for d in read_csv(day_files(self.root / sub, [day], ".csv"), types):
            r = _from_row(kind, d)
            if r is not None:
                yield r

    def read(self, kind: str, inst: str | None, t0: int, t1: int) -> list:
        """[t0, t1) 内的记录，按时间升序，重复的只留先写的那条。inst=None 表示所有合约。"""
        seen = set()
        out = []
        for day in _days(t0, t1):
            for r in self._read_day(kind, day):
                if (inst is not None and r.inst != inst) or not (t0 <= r.ts < t1):
                    continue
                k = (r.inst, _key(kind, r))
                if k in seen:
                    continue
                seen.add(k)
                out.append(r)
        out.sort(key=lambda r: r.ts)
        return out

    def sec_window(self, inst: str, t0: int, t1: int) -> list[SecBar]:
        """[t0, t1) 的 1 秒 K 线。按天缓存，最近读过的几天留在内存里。"""
        out = []
        for day in _days(t0, t1):
            by = self._sec_cache.get(day)
            if by is None:
                by = {}
                for r in self._read_day("sec", day):
                    by.setdefault(r.inst, []).append(r)
                for v in by.values():
                    v.sort(key=lambda r: r.ts)
                self._sec_cache[day] = by
                while len(self._sec_cache) > self._sec_cache_days:
                    self._sec_cache.popitem(last=False)
            else:
                self._sec_cache.move_to_end(day)
            out += [r for r in by.get(inst, []) if t0 <= r.ts < t1]
        return out

    def load_inst(self, inst: str, t0: int, t1: int, meta: dict | None = None,
                  coverage: list[tuple[int, int]] | None = None) -> InstData:
        """一个合约 [t0, t1) 的全部数据（1 秒 K 线按需读）。meta、coverage 不给就从硬盘读。"""
        if meta is None:
            meta = (self.load_meta() or {}).get(inst, {})
        if coverage is None:
            coverage = self.coverage(inst)
        return InstData(
            inst=inst, meta=meta,
            c1h=self.read("c1h", inst, t0, t1), c1m=self.read("c1m", inst, t0, t1),
            oi1h=self.read("oi1h", inst, t0, t1), oilive=self.read("oilive", inst, t0, t1),
            ratio=self.read("ratio1h", inst, t0, t1), liqs=self.read("liq", inst, t0, t1),
            coverage=coverage, sec=lambda a, b, _i=inst: self.sec_window(_i, a, b))

    def bounds(self, kind: str, inst: str) -> tuple[int, int] | None:
        """已存数据的最早、最晚时间（只看首尾两天的文件）。没有返回 None。"""
        sub = KINDS[kind][0]
        days = sorted({p.name[:10] for p in (self.root / sub).glob("*.csv")})
        lo = hi = None
        for d in days:
            ts = [r.ts for r in self._read_day(kind, d) if r.inst == inst]
            if ts:
                lo = min(ts)
                break
        for d in reversed(days):
            ts = [r.ts for r in self._read_day(kind, d) if r.inst == inst]
            if ts:
                hi = max(ts)
                break
        return (lo, hi) if lo is not None else None

    # ---------- 合约信息、采集进度 ----------

    @property
    def meta_path(self) -> Path:
        return self.root / "meta" / "instruments.json"

    def load_meta(self) -> dict | None:
        """{合约: OKX 合约信息}"""
        return load_json(self.meta_path)

    def save_meta(self, meta: dict) -> None:
        save_json(self.meta_path, meta)

    @property
    def state_path(self) -> Path:
        return self.root / "state" / "collector.json"

    def load_state(self) -> dict:
        return load_json(self.state_path) or {}

    def save_state(self, st: dict) -> None:
        save_json(self.state_path, st)

    def coverage(self, inst: str) -> list[tuple[int, int]]:
        st = self.load_state()
        return merge_intervals([tuple(x) for x in st.get("coverage", {}).get(inst, [])])

    # ---------- 进程锁 ----------

    def lock(self) -> None:
        if self._lock_f is not None:
            return
        p = self.root / "state" / "liq.lock"
        p.parent.mkdir(parents=True, exist_ok=True)
        f = p.open("a+", encoding="utf-8")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.seek(0)
            other = f.read().strip() or "?"
            f.close()
            raise LiqBusy(f"另一个进程（pid {other}）正在写 {self.root}") from None
        f.truncate(0)
        f.write(str(os.getpid()))
        f.flush()
        self._lock_f = f

    def unlock(self) -> None:
        if self._lock_f is not None:
            fcntl.flock(self._lock_f, fcntl.LOCK_UN)
            self._lock_f.close()
            self._lock_f = None
