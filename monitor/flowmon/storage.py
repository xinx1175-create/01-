"""按天分文件的存储。

  data/buckets/YYYY-MM-DD.csv      15 秒桶（含分数和不交易条件）
  data/events/YYYY-MM-DD.csv       信号事件和对照组
  data/raw/trades/YYYY-MM-DD.csv   逐笔成交原文，改桶宽时用来重算
  data/raw/books/YYYY-MM-DD.jsonl  每个桶结束时的前 N 档盘口
  data/raw/misc/YYYY-MM-DD.jsonl   持仓量、资金费率、强平、每个桶的完整性记录
  data/meta/instrument.json        合约信息（面值、精度），接口失败时用它兜底
  data/state/events.json           没跟踪完的事件，重启后接着跟
  data/reports/YYYY-MM-DD.md       每日简报

日期按记录本身的交易所时间（UTC）划分。
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Iterable, Iterator

TRADE_COLUMNS = ["ts", "recv", "trade_id", "px", "sz", "side", "count"]


def fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        if v != v:  # NaN
            return ""
        return f"{v:.10g}"
    return str(v)


def parse(v: str, tp: type):
    if v == "" or v is None:
        return None
    if tp is bool:
        return v == "1"
    if tp is int:
        return int(float(v))
    if tp is float:
        return float(v)
    return v


class DailyCsv:
    """按天追加写 CSV。已有文件表头不同（升级换了列）时另起一个带序号的文件。"""

    def __init__(self, directory: Path, columns: list[str]):
        self.dir = directory
        self.columns = columns
        self.day: str | None = None
        self.f = None
        self.w = None
        self.dir.mkdir(parents=True, exist_ok=True)

    def _open(self, day: str) -> None:
        self.close()
        header = ",".join(self.columns)
        n = 0
        while True:
            p = self.dir / (f"{day}.csv" if n == 0 else f"{day}.{n}.csv")
            if not p.exists() or p.stat().st_size == 0:
                new = True
                break
            with p.open(encoding="utf-8") as f:
                first = f.readline().rstrip("\r\n")
            if first == header:
                new = False
                break
            n += 1
        self.f = p.open("a", encoding="utf-8", newline="")
        self.w = csv.writer(self.f)
        if new:
            self.w.writerow(self.columns)
        self.day = day

    def write(self, day: str, row: dict) -> None:
        if day != self.day:
            self._open(day)
        self.w.writerow([fmt(row.get(c)) for c in self.columns])

    def flush(self) -> None:
        if self.f:
            self.f.flush()

    def close(self) -> None:
        if self.f:
            self.f.close()
        self.f = self.w = None
        self.day = None


class DailyJsonl:
    def __init__(self, directory: Path):
        self.dir = directory
        self.day: str | None = None
        self.f = None
        self.dir.mkdir(parents=True, exist_ok=True)

    def write(self, day: str, obj) -> None:
        if day != self.day:
            self.close()
            self.f = (self.dir / f"{day}.jsonl").open("a", encoding="utf-8")
            self.day = day
        self.f.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n")

    def flush(self) -> None:
        if self.f:
            self.f.flush()

    def close(self) -> None:
        if self.f:
            self.f.close()
        self.f = None
        self.day = None


def day_files(directory: Path, days: Iterable[str], ext: str) -> list[Path]:
    out = []
    for d in days:
        out += sorted(directory.glob(f"{d}{ext}")) + sorted(directory.glob(f"{d}.*{ext}"))
    return out


def read_csv(paths: Iterable[Path], types: dict[str, type] | None = None) -> Iterator[dict]:
    for p in paths:
        with p.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if types:
                    yield {k: parse(v, types.get(k, str)) for k, v in row.items()}
                else:
                    yield row


def read_jsonl(paths: Iterable[Path]) -> Iterator[dict]:
    for p in paths:
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue  # 崩溃时写了半行，跳过


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
