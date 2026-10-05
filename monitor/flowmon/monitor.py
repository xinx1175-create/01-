"""阶段一监控器主循环：接行情 → 聚合 15 秒桶 → 算分 → 记不交易条件和信号事件 → 落盘。"""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import shutil
import signal
import time
from datetime import date, timedelta

from .bucket import DOWNTIME, Aggregator, Bucket, day_of, downtime_row, iso_utc
from .conditions import Calendar, Conditions
from .config import Config
from .events import EventEngine, public
from .heartbeat import ping
from .notify import Notifier
from .okx import Feed, contract_size, now_ms
from .orderbook import BookInvalid, OrderBook
from .power import SleepGuard, power_source
from .report import write_daily
from .schema import bucket_columns, event_columns
from .score import ScoreEngine
from .storage import (TRADE_COLUMNS, DailyCsv, DailyJsonl, day_files, load_json, read_csv,
                      save_json)

log = logging.getLogger(__name__)

# 进程内下线原因：启动、断线、行情停了、盘口校验失败、电脑睡眠过。收到新的盘口快照才算恢复
DOWN_ALL = {"startup", "disconnect", "stale", "book_invalid", "sleep"}
DAY_MS = 86_400_000
AC_POWER = "AC Power"


class AlreadyRunning(RuntimeError):
    """同一个数据目录已经有一个监控器在写。两个同时写会把数据搞乱。"""


def _next_day(d: str) -> str:
    return (date.fromisoformat(d) + timedelta(days=1)).isoformat()


class Monitor:
    def __init__(self, cfg: Config, instrument: dict):
        self.cfg = cfg
        self.inst = instrument
        self.inst_id = cfg.exchange.inst_id
        self.ct = contract_size(instrument)
        self.w = cfg.bucket.width_s * 1000
        self.follow_ms = int(cfg.events.followup_minutes * 60_000)

        self.book = OrderBook()
        self.agg = Aggregator(cfg.bucket, self.ct)
        self.score = ScoreEngine(cfg.score, cfg.bucket.width_s)
        c = cfg.conditions
        self.cal = Calendar(cfg.path(c.calendar_file), c.calendar_before_minutes, c.calendar_after_minutes)
        self.cond = Conditions(c, cfg.bucket.width_s, self.cal)
        self.events = EventEngine(cfg, self.ct)
        self.ev_cols = [n for n, _ in event_columns(cfg)]
        self.b_cols = bucket_columns(cfg)

        d = cfg.data_dir
        self.w_buckets = DailyCsv(d / "buckets", [n for n, _ in self.b_cols])
        self.w_events = DailyCsv(d / "events", self.ev_cols)
        self.w_trades = DailyCsv(d / "raw" / "trades", TRADE_COLUMNS)
        self.w_books = DailyJsonl(d / "raw" / "books")
        self.w_misc = DailyJsonl(d / "raw" / "misc")
        self.state_path = d / "state" / "events.json"
        self.lock_path = d / "state" / "run.lock"         # 进程锁：同一个数据目录只允许一个监控器
        self.marker_path = d / "state" / "running.json"   # 运行标记：正常停止时删掉，启动时还在说明上次没正常停
        self.health_path = d / "state" / "health.json"    # 运行状况，给 check 命令看

        self.notifier = Notifier(cfg.notify, f"[flowmon {self.inst_id}]")
        self.feed = Feed(cfg, self)
        self.stop = asyncio.Event()
        self.max_ts = 0              # 见过的最大交易所时间戳（成交、盘口）
        self.offset_ms = 0.0         # 本地时间 − 交易所时间，取上一个桶的延迟中位数
        self.report_day: str | None = None
        self.last_row: dict | None = None
        self.n_buckets = 0
        self.n_signals = 0
        self.book_errors = 0
        self.parse_errors: dict[str, int] = {}
        self._tasks: set[asyncio.Task] = set()
        self.last_start: int | None = None          # 最后一个落盘的桶；重启后据此补停机占位桶
        self.started_ms: int | None = None
        self._lock_f = None
        # 睡眠检测：系统时钟（睡眠时照走）和进程计时（睡眠时停住）之差
        self._wall = time.time
        self._mono = time.monotonic
        self._clk: tuple[float, float] | None = None
        self.sleeps: list[dict] = []
        self.guard = SleepGuard(cfg.power.prevent_sleep)
        self.power_src: str | None = None
        self.last_complete_wall: float | None = None  # 最近一个完整的桶封桶时的本机时间
        self.hb = {"last_ok_ms": None, "fail_streak": 0, "last_err": None, "last_err_ms": None,
                   "paused_since_ms": None}

    # ---------- 重启恢复 ----------

    def restore(self) -> None:
        """读回最近几天的桶，恢复基准值、7 天分位和翻转计数；再接上没跟踪完的事件。"""
        # 按文件名取最近几天，不按本机日期算，免得本机时钟和交易所时间对不上时漏读
        bdir = self.cfg.data_dir / "buckets"
        days = sorted({p.name[:10] for p in bdir.glob("*.csv")})[-(self.cfg.storage.restore_days + 1):]
        rows = list(read_csv(day_files(bdir, days, ".csv"), dict(self.b_cols)))
        rows.sort(key=lambda r: r["start_ms"])
        last = None
        n = 0
        for r in rows:
            if r["width_s"] != self.cfg.bucket.width_s:
                continue
            if last is not None and r["start_ms"] <= last:
                continue
            sc = self.score.update(r)
            self.cond.update(r, sc.valid, sc.S)
            last = r["start_ms"]
            if r["close"] is not None:
                self.agg.prev_close = r["close"]
            n += 1
        if last is not None:
            self.agg.min_start = last + self.w
            self.last_start = last
            log.info("恢复了 %d 个历史桶，最后一个 %s", n, iso_utc(last))
            # 接上日报的进度：停机跨过零点时，第一个新桶就会触发前一天的日报
            self.report_day = day_of(last + self.w - self.follow_ms)
            if self.cfg.health.daily_report:
                for d in days:
                    if d < self.report_day and not (self.cfg.data_dir / "reports" / f"{d}.md").exists():
                        write_daily(self.cfg, d)
                        log.info("补写日报 %s", d)
        st = load_json(self.state_path)
        if st:
            self.events.load_state(st)
            log.info("接着跟踪 %d 个未完成的事件", len(self.events.pending))

    # ---------- 行情回调（Feed 调用） ----------

    def on_open(self) -> None:
        self._misc(now_ms(), {"type": "conn", "event": "open"})

    def on_close(self, reason: str) -> None:
        at = self.max_ts or self._est_now()
        self.agg.set_down("stale" if reason == "stale" else "disconnect", at)
        self.agg.book_reset(at)
        self.book.reset()
        self._misc(at, {"type": "conn", "event": "close", "reason": reason})

    def on_fail_streak(self, n: int) -> None:
        if n == self.cfg.connection.reconnect_fail_notify:
            self._notify("连续重连失败", f"已连续 {n} 次连不上 OKX，最近一次断开时间 {iso_utc(now_ms())}", "reconnect")

    def on_message(self, msg: dict, recv: int) -> None:
        self._check_clock()
        try:
            self._dispatch(msg, recv)
        except (KeyError, ValueError, TypeError) as e:
            # 字段缺失或格式变了：记下来跳过这条，不能因此反复断线重连
            ch = msg.get("arg", {}).get("channel", "?")
            n = self.parse_errors[ch] = self.parse_errors.get(ch, 0) + 1
            if n == 1 or n % 1000 == 0:
                log.error("解析 %s 推送失败（第 %d 次）：%r 原文：%s", ch, n, e, str(msg)[:300])

    def _dispatch(self, msg: dict, recv: int) -> None:
        if "event" in msg:
            ev = msg["event"]
            if ev == "error":
                log.error("交易所返回错误：%s", msg)
            elif ev in ("subscribe", "unsubscribe"):
                log.info("%s %s", ev, msg.get("arg"))
            else:
                log.info("交易所事件：%s", msg)
            return
        ch = msg.get("arg", {}).get("channel")
        data = msg.get("data") or []
        if ch == "trades":
            for d in data:
                self._trade(d, recv)
        elif ch == "books":
            for d in data:
                self._book(msg.get("action"), d, recv)
        elif ch == "open-interest":
            for d in data:
                ts = int(d["ts"])
                self.agg.on_oi(ts, float(d["oiCcy"]))
                self._misc(ts, {"type": "oi", "ts": ts, "recv": recv, "oi": d["oiCcy"], "oi_ct": d.get("oi")})
        elif ch == "funding-rate":
            for d in data:
                self.agg.on_funding(float(d["fundingRate"]))
                ts = int(d["ts"]) if d.get("ts") else recv
                self._misc(ts, {"type": "funding", "ts": ts, "recv": recv, "rate": d["fundingRate"],
                                "next": d.get("nextFundingRate"), "time": d.get("fundingTime")})
        elif ch == "liquidation-orders":
            for d in data:
                if d.get("instId") != self.inst_id:
                    continue
                for x in d.get("details") or []:
                    ts = int(x["ts"])
                    pos = x.get("posSide")
                    # 多头被强平 = 系统卖出平多。双向持仓看 posSide，单向持仓（net）看 side
                    long_side = pos == "long" if pos in ("long", "short") else x.get("side") == "sell"
                    self.agg.on_liq(ts, long_side, float(x["sz"]))
                    self._misc(ts, {"type": "liq", "ts": ts, "recv": recv, "side": x.get("side"),
                                    "pos": pos, "sz": x.get("sz"), "px": x.get("bkPx")})

    def _trade(self, d: dict, recv: int) -> None:
        ts = int(d["ts"])
        px = float(d["px"])
        sz = float(d["sz"])
        side = d["side"]
        count = int(d.get("count") or 1)
        self.w_trades.write(day_of(ts), {"ts": ts, "recv": recv, "trade_id": d.get("tradeId"),
                                         "px": d["px"], "sz": d["sz"], "side": side, "count": count})
        self.agg.on_trade(ts, px, sz, side, count, recv)
        self.events.on_trade(ts, px, sz, side)
        if ts > self.max_ts:
            self.max_ts = ts

    def _book(self, action: str | None, d: dict, recv: int) -> None:
        ts = int(d["ts"])
        seq = int(d.get("seqId", -1))
        prev = int(d.get("prevSeqId", -1))
        chk = d.get("checksum")
        chk = int(chk) if chk not in (None, "") else None
        if ts > self.max_ts:
            self.max_ts = ts
        if action != "snapshot" and not self.book.ready:
            return  # 已经在等重新订阅后的快照，这期间的增量没有意义
        try:
            if action == "snapshot":
                self.book.apply_snapshot(d.get("bids", []), d.get("asks", []), ts, seq, chk)
                self.agg.clear_down(DOWN_ALL, ts)
                self.feed.mark_healthy()
                log.info("盘口快照就绪 seqId=%s 买 %d 档 卖 %d 档", seq,
                         len(self.book.bids.prices), len(self.book.asks.prices))
            else:
                self.agg.before_book(ts, self.book)
                decs = self.book.apply_update(d.get("bids", []), d.get("asks", []), ts, seq, prev, chk)
                self.agg.after_book(ts, decs, self.book.mid(), recv)
        except BookInvalid as e:
            self.book_errors += 1
            log.warning("盘口校验失败（%s），重新订阅盘口", e)
            at = self.max_ts or ts
            self.agg.set_down("book_invalid", at)
            self.agg.book_reset(at)
            self.book.reset()
            self._misc(at, {"type": "conn", "event": "book_invalid", "reason": str(e)})
            self._spawn(self.feed.resubscribe_books())

    # ---------- 封桶后的处理 ----------

    def _on_bucket(self, row: dict, b: Bucket) -> None:
        start = row["start_ms"]
        end = start + self.w
        day = day_of(start)
        if self.last_start is not None and start > self.last_start + self.w:
            self._fill_downtime(self.last_start + self.w, start)
        self.last_start = start
        sc = self.score.update(row)
        self.cal.reload()
        cond = self.cond.update(row, sc.valid, sc.S)
        full = {**row, **sc.as_row(), **cond}
        self.w_buckets.write(day, full)
        cap = b.capture
        if cap is not None:
            self.w_books.write(day, {"s": start, "t": end, "ts": cap.ts, "seq": cap.seq,
                                     "bids": cap.bids, "asks": cap.asks})
        self._misc(start, {"type": "bucket", "s": start, "w": self.cfg.bucket.width_s,
                           "ok": row["complete"], "why": row["incomplete_reason"]})
        created, done = self.events.on_bucket(full, sc, cond, cap)
        for e in created:
            if e["kind"] == "signal":
                self.n_signals += 1
            log.info("事件 %s S=%.1f 价格=%s 不交易=%s", e["event_id"], e["S"], e["price"],
                     e["no_trade_reason"] or "否")
        for e in done:
            self.w_events.write(day_of(e["ts_ms"]), public(e, self.ev_cols))
        save_json(self.state_path, self.events.state())
        self.w_buckets.flush()
        self.w_events.flush()
        if row["latency_ms"] is not None:
            self.offset_ms = row["latency_ms"]
        if row["complete"]:
            self.last_complete_wall = self._wall()
        self.n_buckets += 1
        self.last_row = full
        s_txt = f"{sc.S:.1f}" if sc.S is not None else "-"
        log.info("桶 %s 完整=%s S=%s%s 买=%.3f 卖=%.3f OI=%s 延迟=%s",
                 row["time_utc"], int(row["complete"]), s_txt, "" if sc.valid else f"（无效:{sc.note}）",
                 row["buy_vol"], row["sell_vol"], row["oi"], row["latency_ms"])
        self._maybe_report(end)

    def _fill_downtime(self, a: int, b: int) -> None:
        """补写 [a, b) 的停机占位桶（标为不完整，原因 downtime），照常喂给分数、不交易条件和事件跟踪，
        回放时也会生成同样的占位桶。停机太久只补最近 restore_days 天，再早的不影响任何窗口。"""
        keep = (self.cfg.storage.restore_days * DAY_MS // self.w) * self.w
        if b - a > keep:
            log.warning("停机 %.1f 天，只补最近 %d 天的占位桶", (b - a) / DAY_MS, self.cfg.storage.restore_days)
            a = b - keep
        n = 0
        for s in range(a, b, self.w):
            row = downtime_row(s, self.w)
            sc = self.score.update(row)
            cond = self.cond.update(row, sc.valid, sc.S)
            full = {**row, **sc.as_row(), **cond}
            self.w_buckets.write(day_of(s), full)
            self._misc(s, {"type": "bucket", "s": s, "w": self.cfg.bucket.width_s, "ok": False, "why": DOWNTIME})
            _, done = self.events.on_bucket(full, sc, cond, None)
            for e in done:
                self.w_events.write(day_of(e["ts_ms"]), public(e, self.ev_cols))
            n += 1
        log.info("补写停机占位桶 %d 个：%s → %s", n, iso_utc(a), iso_utc(b))

    def _maybe_report(self, end: int) -> None:
        # 某天的事件要在次日 0 点再过跟踪时长后才全部落盘，那时再写这一天的日报。
        # 停机跨过好几天时，中间每一天都补一份
        target = day_of(end - self.follow_ms)
        if self.report_day is not None and target > self.report_day and self.cfg.health.daily_report:
            d = self.report_day
            while d < target:
                summary = write_daily(self.cfg, d)
                if summary:
                    log.info("日报：%s", summary)
                    if self.cfg.notify.daily_summary:
                        self._notify("日报", summary, None)
                d = _next_day(d)
        self.report_day = target

    # ---------- 睡眠检测 ----------

    def _check_clock(self) -> None:
        """系统时钟比进程计时多走了 sleep_detect_s 秒以上，说明中间电脑睡眠过（进程计时在睡眠时停住）。"""
        wall, mono = self._wall(), self._mono()
        if self._clk is not None:
            w0, m0 = self._clk
            slept = (wall - w0) - (mono - m0)
            if slept > self.cfg.power.sleep_detect_s:
                self._on_sleep(w0, wall, slept)
        self._clk = (wall, mono)

    def _on_sleep(self, w0: float, w1: float, slept: float) -> None:
        # 从睡前那一刻起，到重新连上拿到盘口快照为止，桶都标为不完整（sleep）；本地盘口已不可信，强制重连
        at = int(w0 * 1000 - self.offset_ms)
        self.agg.set_down("sleep", at)
        self.feed.request_reconnect("sleep")
        rec = {"from_utc": iso_utc(int(w0 * 1000)), "to_utc": iso_utc(int(w1 * 1000)), "seconds": round(slept, 1)}
        self.sleeps = (self.sleeps + [rec])[-20:]
        self._misc(at, {"type": "sleep", "from": int(w0 * 1000), "to": int(w1 * 1000), "s": round(slept, 1)})
        log.warning("电脑睡眠了约 %.0f 秒（%s → %s），这段时间的桶标为 sleep，重新连接", slept,
                    rec["from_utc"], rec["to_utc"])
        self._notify("电脑睡眠过", f"{rec['from_utc'][:19]} 起睡眠了约 {slept / 60:.1f} 分钟，这段没有数据。"
                     "运行期间本应阻止睡眠：检查是不是合上了笔记本的盖子，或者拔了电源。", "sleep")

    # ---------- 循环 ----------

    def _est_now(self) -> int:
        return int(now_ms() - self.offset_ms)

    def _watermark(self) -> int:
        return max(self.max_ts, self._est_now() - self.cfg.bucket.fallback_close_lag_ms)

    async def _ticker(self) -> None:
        tick = self.cfg.bucket.close_grace_ms / 2000
        last_flush = time.monotonic()
        while not self.stop.is_set():
            self._check_clock()
            for row, b in self.agg.advance(self._watermark(), self.book):
                self._on_bucket(row, b)
            t = time.monotonic()
            if t - last_flush >= self.cfg.storage.raw_flush_s:
                for wr in (self.w_trades, self.w_books, self.w_misc):
                    wr.flush()
                last_flush = t
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=tick)
            except asyncio.TimeoutError:
                pass

    async def _health(self) -> None:
        h = self.cfg.health
        while not self.stop.is_set():
            try:
                free_gb = shutil.disk_usage(self.cfg.data_dir).free / 1e9
                if free_gb < h.disk_min_free_gb:
                    log.error("磁盘剩余 %.2f GB，低于 %.2f GB", free_gb, h.disk_min_free_gb)
                    self._notify("磁盘空间不足", f"数据盘只剩 {free_gb:.2f} GB", "disk")
            except OSError as e:
                log.error("检查磁盘失败：%s", e)
            if await self._sleep(h.disk_check_interval_s):
                break

    async def _power(self) -> None:
        """供电方式变了就推送；caffeinate 意外退出就重新挂上。"""
        while not self.stop.is_set():
            if self.guard.ensure():
                self._notify("阻止睡眠曾经失效", "caffeinate 意外退出，已重新挂上", "caffeinate")
            src = await asyncio.to_thread(power_source)
            if src is not None and src != self.power_src:
                if src != AC_POWER:
                    log.warning("改用 %s 供电", src)
                    self._notify("改用电池供电", f"电脑现在由 {src} 供电（拔了电源或停电）。电池用完会关机，"
                                 "合盖或电量低时也可能睡眠。", "power")
                elif self.power_src is not None:
                    log.info("恢复接电源")
                    self._notify("恢复接电源", "电脑重新接上了电源", "power_ac")
                self.power_src = src
            if await self._sleep(self.cfg.power.check_interval_s):
                break

    async def _heartbeat(self) -> None:
        """每 interval_s 秒：最近收到过完整的桶就向心跳服务报到；顺便把运行状况写到 health.json。"""
        hb, st = self.cfg.heartbeat, self.hb
        while not await self._sleep(hb.interval_s):
            fresh = (self.last_complete_wall is not None
                     and self._wall() - self.last_complete_wall <= hb.max_data_age_s)
            if not fresh:
                if self.last_complete_wall is not None and st["paused_since_ms"] is None:
                    st["paused_since_ms"] = now_ms()
                    log.warning("超过 %.0f 秒没有完整的桶，暂停心跳报到；持续下去心跳服务会报警", hb.max_data_age_s)
            else:
                if st["paused_since_ms"] is not None:
                    log.info("数据恢复，继续心跳报到")
                    st["paused_since_ms"] = None
                if hb.url:
                    err = await asyncio.to_thread(ping, hb.url, hb.timeout_s)
                    if err is None:
                        if st["fail_streak"]:
                            log.info("心跳报到恢复（之前连续失败 %d 次）", st["fail_streak"])
                        st["last_ok_ms"], st["fail_streak"] = now_ms(), 0
                    else:
                        st["fail_streak"] += 1
                        st["last_err"], st["last_err_ms"] = err, now_ms()
                        if st["fail_streak"] == 1 or st["fail_streak"] % 60 == 0:
                            log.warning("心跳报到失败（连续第 %d 次）：%s", st["fail_streak"], err)
            self._write_health()

    def _write_health(self) -> None:
        def t(ms):
            return iso_utc(ms) if ms else None
        hb = self.hb
        save_json(self.health_path, {
            "pid": os.getpid(),
            "started_utc": t(self.started_ms),
            "updated_utc": t(now_ms()),
            "last_bucket_utc": t(self.last_start),
            "last_complete_age_s": (round(self._wall() - self.last_complete_wall, 1)
                                    if self.last_complete_wall is not None else None),
            "buckets": self.n_buckets,
            "signals": self.n_signals,
            "book_errors": self.book_errors,
            "parse_errors": self.parse_errors,
            "heartbeat": {"configured": bool(self.cfg.heartbeat.url), "last_ok_utc": t(hb["last_ok_ms"]),
                          "fail_streak": hb["fail_streak"], "last_err": hb["last_err"],
                          "last_err_utc": t(hb["last_err_ms"]), "paused_since_utc": t(hb["paused_since_ms"])},
            "power": {"prevent_sleep": self.guard.enabled, "caffeinate_running": self.guard.alive(),
                      "source": self.power_src},
            "sleeps": self.sleeps,
        })

    async def _sleep(self, s: float) -> bool:
        """等 s 秒；期间收到停止信号返回 True。"""
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=s)
            return True
        except asyncio.TimeoutError:
            return False

    # ---------- 进程锁 ----------

    def lock(self) -> None:
        """同一个数据目录只允许一个监控器在写（例如后台服务在跑时又手动启动一个）。进程退出锁自动释放。"""
        if self._lock_f is not None:
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        f = self.lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.seek(0)
            other = f.read().strip() or "?"
            f.close()
            raise AlreadyRunning(f"另一个监控器（pid {other}）正在写数据目录 {self.cfg.data_dir}，先停掉它") from None
        f.truncate(0)
        f.write(str(os.getpid()))
        f.flush()
        self._lock_f = f

    def unlock(self) -> None:
        if self._lock_f is not None:
            fcntl.flock(self._lock_f, fcntl.LOCK_UN)
            self._lock_f.close()
            self._lock_f = None

    async def run(self, duration_s: float | None = None) -> None:
        self.lock()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        self.started_ms = now_ms()
        # 运行标记还在 = 上次没走到正常停止（崩溃、被强制结束、断电、关机）
        prev = load_json(self.marker_path)
        save_json(self.marker_path, {"pid": os.getpid(), "started_utc": iso_utc(self.started_ms)})
        self.guard.start()
        self.agg.set_down("startup", self._est_now())
        if prev:
            log.warning("上次运行（%s 启动）没有正常停止", prev.get("started_utc"))
            last = iso_utc(self.last_start + self.w)[:19] if self.last_start is not None else "-"
            self._notify("已重新启动（上次没有正常停止）",
                         f"上次 {str(prev.get('started_utc'))[:19]} 启动，数据记录到 {last}。可能是崩溃、被强制结束、"
                         "断电或关机。停机期间的桶标为 downtime。", None)
        elif self.cfg.notify.on_start:
            self._notify("已启动", f"{iso_utc(now_ms())} 开始记录 {self.inst_id}", None)
        tasks = [asyncio.create_task(self.feed.run(self.stop), name="feed"),
                 asyncio.create_task(self._ticker(), name="ticker"),
                 asyncio.create_task(self._health(), name="health"),
                 asyncio.create_task(self._power(), name="power"),
                 asyncio.create_task(self._heartbeat(), name="heartbeat")]
        if duration_s:
            tasks.append(asyncio.create_task(self._stop_after(duration_s), name="timer"))
        reason = "收到停止信号"
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for t in done:
                if t.exception() is not None:
                    reason = f"{t.get_name()} 异常：{t.exception()!r}"
                    raise t.exception()
        finally:
            self.stop.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.close()
            self.guard.stop()
            self._write_health()
            log.info("监控器停止：%s", reason)
            await asyncio.to_thread(self.notifier.send, "已停止", reason, None)
            # 走到这里都算有交代的停止（异常也推送了原因）；只有崩溃、强杀、断电才会留下运行标记
            try:
                self.marker_path.unlink()
            except FileNotFoundError:
                pass
            self.unlock()

    async def _stop_after(self, s: float) -> None:
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=s)
        except asyncio.TimeoutError:
            log.info("到达设定的运行时长 %.0f 秒", s)
            self.stop.set()

    def close(self) -> None:
        save_json(self.state_path, self.events.state())
        for wr in (self.w_buckets, self.w_events, self.w_trades, self.w_books, self.w_misc):
            wr.close()

    # ---------- 杂项 ----------

    def _misc(self, ts: int, obj: dict) -> None:
        self.w_misc.write(day_of(ts), obj)

    def _spawn(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _notify(self, title: str, body: str, key: str | None) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.notifier.send(title, body, key)
            return
        self._spawn(asyncio.to_thread(self.notifier.send, title, body, key))
