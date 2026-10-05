"""OKX 公开行情接入：WebSocket 订阅、心跳、断线重连，以及合约信息接口。

只用公开频道，不登录，不读取任何密钥，没有任何下单、撤单、查询账户的代码。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Protocol

import websockets

from .config import Config
from .storage import load_json, save_json

log = logging.getLogger(__name__)


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------- 合约信息 ----------

def fetch_instrument(cfg: Config) -> dict:
    ex = cfg.exchange
    q = urllib.parse.urlencode({"instType": ex.inst_type, "instId": ex.inst_id})
    url = f"{ex.rest_base_url}/api/v5/public/instruments?{q}"
    with urllib.request.urlopen(url, timeout=ex.rest_timeout_s) as r:
        body = json.load(r)
    if str(body.get("code")) != "0" or not body.get("data"):
        raise RuntimeError(f"合约信息接口返回异常：{body}")
    return body["data"][0]


def load_instrument(cfg: Config) -> dict:
    """先读接口；读不到就用上次存下的那份。两样都没有就无法换算张数，直接报错。"""
    path = cfg.data_dir / "meta" / "instrument.json"
    try:
        inst = fetch_instrument(cfg)
        save_json(path, {"fetched_ms": now_ms(), "instrument": inst})
        log.info("合约信息：ctVal=%s %s ctMult=%s lotSz=%s minSz=%s tickSz=%s",
                 inst.get("ctVal"), inst.get("ctValCcy"), inst.get("ctMult"),
                 inst.get("lotSz"), inst.get("minSz"), inst.get("tickSz"))
        return inst
    except Exception as e:
        cached = load_json(path)
        if cached and cached.get("instrument", {}).get("instId") == cfg.exchange.inst_id:
            log.warning("合约信息接口失败（%s），使用 %s 里的缓存", e, path)
            return cached["instrument"]
        raise RuntimeError(f"拿不到合约信息，且没有缓存：{e}") from e


def contract_size(inst: dict) -> float:
    """1 张合约对应多少个基础币。"""
    return float(inst["ctVal"]) * float(inst.get("ctMult") or 1)


# ---------- WebSocket ----------

class FeedHandler(Protocol):
    def on_open(self) -> None: ...
    def on_message(self, msg: dict, recv_ms: int) -> None: ...
    def on_close(self, reason: str) -> None: ...
    def on_fail_streak(self, n: int) -> None: ...


class Feed:
    def __init__(self, cfg: Config, handler: FeedHandler):
        self.cfg = cfg
        self.h = handler
        ex = cfg.exchange
        self.book_arg = {"channel": "books", "instId": ex.inst_id}
        self.args = [
            {"channel": "trades", "instId": ex.inst_id},
            self.book_arg,
            {"channel": "open-interest", "instId": ex.inst_id},
            {"channel": "funding-rate", "instId": ex.inst_id},
            {"channel": "liquidation-orders", "instType": ex.inst_type},
        ]
        self.ws = None
        self.healthy = False
        self.fail_streak = 0
        self.last_data = 0.0

    def mark_healthy(self) -> None:
        """收到盘口快照后由上层调用：这次连接算成功。"""
        self.healthy = True

    async def resubscribe_books(self) -> None:
        if self.ws is None:
            return
        try:
            await self.ws.send(json.dumps({"op": "unsubscribe", "args": [self.book_arg]}))
            await self.ws.send(json.dumps({"op": "subscribe", "args": [self.book_arg]}))
            log.info("盘口重新订阅")
        except Exception as e:
            log.warning("盘口重新订阅失败：%s", e)

    async def run(self, stop: asyncio.Event) -> None:
        c = self.cfg.connection
        backoff = c.reconnect_backoff_min_s
        while not stop.is_set():
            reason = "closed"
            self.healthy = False
            try:
                reason = await self._session(stop)
            except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as e:
                reason = f"{type(e).__name__}: {e}"
            except Exception as e:  # 解析或处理出错也不能让监控器停
                log.exception("行情连接异常")
                reason = f"{type(e).__name__}: {e}"
            finally:
                self.ws = None
                self.h.on_close(reason)
            if stop.is_set():
                break
            if self.healthy:
                self.fail_streak = 0
                backoff = c.reconnect_backoff_min_s
            else:
                self.fail_streak += 1
                self.h.on_fail_streak(self.fail_streak)
            log.warning("连接断开：%s；%.0f 秒后重连（连续失败 %d 次）", reason, backoff, self.fail_streak)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, c.reconnect_backoff_max_s)

    async def _session(self, stop: asyncio.Event) -> str:
        c = self.cfg.connection
        url = self.cfg.exchange.ws_public_url
        async with websockets.connect(url, ping_interval=None, max_size=None,
                                      open_timeout=c.subscribe_timeout_s, close_timeout=2) as ws:
            self.ws = ws
            await ws.send(json.dumps({"op": "subscribe", "args": self.args}))
            log.info("已连接 %s，发送订阅", url)
            self.h.on_open()
            t0 = time.monotonic()
            self.last_data = t0
            last_ping = t0
            ping_wait: float | None = None
            tick = min(0.5, c.stale_reconnect_s / 4)
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=tick)
                except asyncio.TimeoutError:
                    raw = None
                now = time.monotonic()
                if raw is not None:
                    if raw == "pong":
                        ping_wait = None
                    else:
                        msg = json.loads(raw)
                        if "data" in msg:
                            self.last_data = now
                        self.h.on_message(msg, now_ms())
                if now - last_ping >= c.ping_interval_s:
                    await ws.send("ping")
                    last_ping = now
                    if ping_wait is None:
                        ping_wait = now
                if ping_wait is not None and now - ping_wait > c.pong_timeout_s:
                    return "pong_timeout"
                if now - self.last_data > c.stale_reconnect_s:
                    return "stale"
                if not self.healthy and now - t0 > c.subscribe_timeout_s:
                    return "no_snapshot"
            return "stopped"
