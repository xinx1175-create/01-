"""阶段一监控器主循环：接行情 → 聚合 15 秒桶 → 算分 → 记不交易条件和信号事件 → 落盘。"""
from __future__ import annotations

import asyncio
import logging
import shutil
import signal
import time

from .bucket import Aggregator, Bucket, day_of, iso_utc
from .conditions import Calendar, Conditions
from .config import Config
from .events import EventEngine, public
from .notify import Notifier
from .okx import Feed, contract_size, now_ms
from .orderbook import BookInvalid, OrderBook
from .report import write_daily
from .schema import BUCKET_COLUMNS, event_columns
from .score import ScoreEngine
from .storage import (TRADE_COLUMNS, DailyCsv, DailyJsonl, day_files, load_json, read_csv,
                      save_json)

log = logging.getLogger(__name__)

# 进程内下线原因：启动、断线、行情停了、盘口校验失败
DOWN_ALL = {"startup", "disconnect", "stale", "book_invalid"}


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

        d = cfg.data_dir
        self.w_buckets = DailyCsv(d / "buckets", [n for n, _ in BUCKET_COLUMNS])
        self.w_events = DailyCsv(d / "events", self.ev_cols)
        self.w_trades = DailyCsv(d / "raw" / "trades", TRADE_COLUMNS)
        self.w_books = DailyJsonl(d / "raw" / "books")
        self.w_misc = DailyJsonl(d / "raw" / "misc")
        self.state_path = d / "state" / "events.json"

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

    # ---------- 重启恢复 ----------

    def restore(self) -> None:
        """读回最近几天的桶，恢复基准值、7 天分位和翻转计数；再接上没跟踪完的事件。"""
        # 按文件名取最近几天，不按本机日期算，免得本机时钟和交易所时间对不上时漏读
        bdir = self.cfg.data_dir / "buckets"
        days = sorted({p.name[:10] for p in bdir.glob("*.csv")})[-(self.cfg.storage.restore_days + 1):]
        rows = list(read_csv(day_files(bdir, days, ".csv"), dict(BUCKET_COLUMNS)))
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
        self.n_buckets += 1
        self.last_row = full
        s_txt = f"{sc.S:.1f}" if sc.S is not None else "-"
        log.info("桶 %s 完整=%s S=%s%s 买=%.3f 卖=%.3f OI=%s 延迟=%s",
                 row["time_utc"], int(row["complete"]), s_txt, "" if sc.valid else f"（无效:{sc.note}）",
                 row["buy_vol"], row["sell_vol"], row["oi"], row["latency_ms"])
        self._maybe_report(end)

    def _maybe_report(self, end: int) -> None:
        # 某天的事件要在次日 0 点再过跟踪时长后才全部落盘，那时再写这一天的日报
        target = day_of(end - self.follow_ms)
        if self.report_day is not None and target != self.report_day and self.cfg.health.daily_report:
            summary = write_daily(self.cfg, self.report_day)
            if summary:
                log.info("日报：%s", summary)
                if self.cfg.notify.daily_summary:
                    self._notify("日报", summary, None)
        self.report_day = target

    # ---------- 循环 ----------

    def _est_now(self) -> int:
        return int(now_ms() - self.offset_ms)

    def _watermark(self) -> int:
        return max(self.max_ts, self._est_now() - self.cfg.bucket.fallback_close_lag_ms)

    async def _ticker(self) -> None:
        tick = self.cfg.bucket.close_grace_ms / 2000
        last_flush = time.monotonic()
        while not self.stop.is_set():
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
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=h.disk_check_interval_s)
            except asyncio.TimeoutError:
                pass

    async def run(self, duration_s: float | None = None) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        self.agg.set_down("startup", self._est_now())
        if self.cfg.notify.on_start:
            self._notify("已启动", f"{iso_utc(now_ms())} 开始记录 {self.inst_id}", None)
        tasks = [asyncio.create_task(self.feed.run(self.stop), name="feed"),
                 asyncio.create_task(self._ticker(), name="ticker"),
                 asyncio.create_task(self._health(), name="health")]
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
            log.info("监控器停止：%s", reason)
            await asyncio.to_thread(self.notifier.send, "已停止", reason, None)

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
