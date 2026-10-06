"""本地假交易所：按 OKX 公开频道的格式推送合成行情，用来端到端测试，不碰真实网络。

- trades / books / open-interest / funding-rate / liquidation-orders 五个频道
- 盘口先推快照（prevSeqId=-1）再推增量，seqId 连续；checksum 固定 0（与 OKX 现状一致）
- 文本 "ping" 回 "pong"
- 时间可加速：speed=60 表示真实 1 秒 = 行情 1 分钟，推送里的 ts 都是模拟时间
- 可注入故障：盘口 seqId 断档、主动断线、停推几秒

合成行情只用来验证链路和落盘，不能拿来评价策略。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time

import websockets

log = logging.getLogger(__name__)

TICK = 0.1
LOT = 0.01


class Market:
    def __init__(self, seed: int, start_ms: int, mid: float = 60000.0, depth: int = 400):
        self.rng = random.Random(seed)
        self.now = start_ms
        self.mid = mid
        self.depth = depth
        self.bids: dict[int, float] = {}  # 价格按 tick 整数存，张数
        self.asks: dict[int, float] = {}
        self.seq = 1000
        self.trade_id = 1
        self.oi = 2_000_000.0  # 张
        self.funding = 0.0001
        self.burst_dir = 0
        self.burst_until = 0
        self._last_oi = start_ms
        self._last_funding = start_ms
        self._rebuild()

    def _rebuild(self):
        m = round(self.mid / TICK)
        self.bids = {m - 1 - i: self._size() for i in range(self.depth)}
        self.asks = {m + 1 + i: self._size() for i in range(self.depth)}

    def _size(self) -> float:
        return round(max(LOT, self.rng.lognormvariate(1.5, 1.0)), 2)

    def best_bid(self) -> int:
        return max(self.bids)

    def best_ask(self) -> int:
        return min(self.asks)

    def step(self, dt_ms: int):
        """推进 dt_ms，返回 (成交列表, 盘口变化 {"bids": {...}, "asks": {...}}, 其它推送列表)。"""
        rng = self.rng
        self.now += dt_ms
        t = self.now
        if self.burst_dir == 0 and rng.random() < dt_ms / 1000 / 400:  # 平均约 7 分钟一次资金涌入
            self.burst_dir = rng.choice((1, -1))
            self.burst_until = t + rng.randint(60, 240) * 1000
        elif self.burst_dir and t >= self.burst_until:
            self.burst_dir = 0
        burst = self.burst_dir

        changed_b: dict[int, float] = {}
        changed_a: dict[int, float] = {}
        trades = []
        lam = (5.0 if burst else 1.6) * dt_ms / 200
        n = self._poisson(lam)
        p_buy = 0.5 + 0.3 * burst
        for _ in range(n):
            side = "buy" if rng.random() < p_buy else "sell"
            sz = round(max(LOT, rng.lognormvariate(0.3 if burst else 0.0, 1.0)), 2)
            book = self.asks if side == "buy" else self.bids
            changed = changed_a if side == "buy" else changed_b
            left = sz
            while left > 1e-9 and book:
                px = min(book) if side == "buy" else max(book)
                take = min(left, book[px])
                book[px] = round(book[px] - take, 2)
                left -= take
                ts = t - rng.randint(0, dt_ms - 1)
                trades.append({"instId": "", "tradeId": str(self.trade_id), "px": f"{px * TICK:.1f}",
                               "sz": f"{take:.2f}", "side": side, "ts": str(ts), "count": "1"})
                self.trade_id += 1
                if book[px] <= 1e-9:
                    del book[px]
                    changed[px] = 0.0
                else:
                    changed[px] = book[px]
        trades.sort(key=lambda x: int(x["ts"]))

        # 中间价随机游走；涌入期间有漂移
        drift = burst * 0.6 * dt_ms / 200
        self.mid += (drift + rng.gauss(0, 1.2 * math.sqrt(dt_ms / 200))) * TICK * 10
        m = round(self.mid / TICK)
        for px in [p for p in self.asks if p <= m]:
            del self.asks[px]
            changed_a[px] = 0.0
        for px in [p for p in self.bids if p >= m]:
            del self.bids[px]
            changed_b[px] = 0.0
        bb = max(self.bids) if self.bids else m - 1
        ba = min(self.asks) if self.asks else m + 1
        for px in range(bb + 1, m):
            self.bids[px] = changed_b[px] = self._size()
        for px in range(m + 1, ba):
            self.asks[px] = changed_a[px] = self._size()
        # 近处挂撤单
        for _ in range(rng.randint(2, 8)):
            side_b = rng.random() < 0.5
            book, changed = (self.bids, changed_b) if side_b else (self.asks, changed_a)
            if not book:
                continue
            base = max(book) if side_b else min(book)
            px = base - rng.randint(0, 20) if side_b else base + rng.randint(0, 20)
            if px in book and rng.random() < 0.4:
                del book[px]
                changed[px] = 0.0
            else:
                book[px] = changed[px] = self._size()
        # 补足、裁掉远端，保持 depth 档
        for book, changed, sign in ((self.bids, changed_b, -1), (self.asks, changed_a, 1)):
            while len(book) < self.depth:
                edge = (min(book) if sign < 0 else max(book)) if book else m
                px = edge + sign
                book[px] = changed[px] = self._size()
            if len(book) > self.depth:
                far = sorted(book, reverse=(sign > 0))[: len(book) - self.depth]
                for px in far:
                    del book[px]
                    changed[px] = 0.0

        others = []
        if t - self._last_oi >= 3000:
            self._last_oi = t
            self.oi += rng.gauss(0, 400) + abs(burst) * 2500 - (self.oi - 2_000_000) * 0.002
            others.append(("open-interest", {"instType": "SWAP", "instId": "", "oi": f"{self.oi:.2f}",
                                             "oiCcy": f"{self.oi * 0.01:.4f}", "oiUsd": "", "ts": str(t)}))
        if t - self._last_funding >= 60_000:
            self._last_funding = t
            self.funding += rng.gauss(0, 0.00001)
            others.append(("funding-rate", {"instType": "SWAP", "instId": "",
                                            "fundingRate": f"{self.funding:.8f}",
                                            "nextFundingRate": "", "fundingTime": str(t), "ts": str(t)}))
        if burst and rng.random() < dt_ms / 1000 / 20:
            pos = "short" if burst > 0 else "long"
            others.append(("liquidation-orders", {
                "instType": "SWAP", "instId": "", "instFamily": "BTC-USDT", "uly": "BTC-USDT",
                "details": [{"side": "buy" if pos == "short" else "sell", "posSide": pos,
                             "bkPx": f"{self.mid:.1f}", "sz": f"{rng.randint(1, 50)}",
                             "bkLoss": "0", "ccy": "", "ts": str(t)}]}))
        return trades, {"bids": changed_b, "asks": changed_a}, others

    def _poisson(self, lam: float) -> int:
        l, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= l:
                return k
            k += 1

    @staticmethod
    def _rows(levels: dict[int, float], reverse: bool):
        return [[f"{px * TICK:.1f}", f"{sz:.2f}", "0", "1"] for px, sz in sorted(levels.items(), reverse=reverse)]

    def snapshot(self) -> dict:
        return {"asks": self._rows(self.asks, False), "bids": self._rows(self.bids, True),
                "ts": str(self.now), "checksum": 0, "prevSeqId": -1, "seqId": self.seq}

    def update(self, changed) -> dict:
        prev = self.seq
        self.seq += 1 if (changed["bids"] or changed["asks"]) else 0
        return {"asks": self._rows(changed["asks"], False), "bids": self._rows(changed["bids"], True),
                "ts": str(self.now), "checksum": 0, "prevSeqId": prev, "seqId": self.seq}


class FakeOkx:
    def __init__(self, inst_id: str = "BTC-USDT-SWAP", speed: float = 1.0, seed: int = 7,
                 start_ms: int | None = None, step_ms: int = 200, faults: list[tuple[float, str]] | None = None):
        self.inst_id = inst_id
        self.speed = speed
        self.step_ms = step_ms
        self.market = Market(seed, start_ms if start_ms is not None else int(time.time() * 1000))
        self.t_start = self.market.now
        self.faults = sorted(faults or [])  # [(模拟秒, "seq_gap"|"disconnect"|"silence")]
        self.clients: dict = {}  # ws -> set(channel)
        self.silent_until = 0.0
        self.stats = {"connections": 0, "book_msgs": 0, "trade_msgs": 0}

    async def handler(self, ws):
        self.stats["connections"] += 1
        subs: set[str] = set()
        self.clients[ws] = subs
        try:
            async for raw in ws:
                if raw == "ping":
                    await ws.send("pong")
                    continue
                msg = json.loads(raw)
                op = msg.get("op")
                for arg in msg.get("args", []):
                    ch = arg.get("channel")
                    if op == "subscribe":
                        subs.add(ch)
                        await ws.send(json.dumps({"event": "subscribe", "arg": arg, "connId": "sim"}))
                        if ch == "books":
                            await ws.send(json.dumps({"arg": arg, "action": "snapshot",
                                                      "data": [self.market.snapshot()]}))
                    elif op == "unsubscribe":
                        subs.discard(ch)
                        await ws.send(json.dumps({"event": "unsubscribe", "arg": arg, "connId": "sim"}))
        except websockets.ConnectionClosed:
            pass
        finally:
            self.clients.pop(ws, None)

    async def _broadcast(self, ch: str, payload: str):
        for ws, subs in list(self.clients.items()):
            if ch in subs:
                try:
                    await ws.send(payload)
                except websockets.ConnectionClosed:
                    pass

    async def market_loop(self, stop: asyncio.Event):
        mk = self.market
        t0 = time.monotonic()
        k = 0
        while not stop.is_set():
            k += 1
            target = t0 + k * self.step_ms / 1000 / self.speed
            delay = target - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                await asyncio.sleep(0)
            trades, changed, others = mk.step(self.step_ms)
            upd = mk.update(changed)  # 紧接着生成，中间不能让出控制权，否则新订阅拿到的快照会和增量错位
            sim_s = (mk.now - self.t_start) / 1000
            gap = False
            while self.faults and self.faults[0][0] <= sim_s:
                _, kind = self.faults.pop(0)
                log.info("注入故障 %s @ 模拟第 %.0f 秒", kind, sim_s)
                if kind == "seq_gap":
                    gap = True
                elif kind == "disconnect":
                    for ws in list(self.clients):
                        await ws.close()
                elif kind == "silence":
                    self.silent_until = time.monotonic() + 8
            if time.monotonic() < self.silent_until:
                continue
            for tr in trades:
                tr["instId"] = self.inst_id
            if trades:
                self.stats["trade_msgs"] += 1
                await self._broadcast("trades", json.dumps(
                    {"arg": {"channel": "trades", "instId": self.inst_id}, "data": trades}))
            if gap:
                upd["prevSeqId"] = upd["prevSeqId"] - 5
            self.stats["book_msgs"] += 1
            await self._broadcast("books", json.dumps(
                {"arg": {"channel": "books", "instId": self.inst_id}, "action": "update", "data": [upd]}))
            for ch, d in others:
                d["instId"] = self.inst_id
                arg = {"channel": ch, "instType": "SWAP"} if ch == "liquidation-orders" else \
                      {"channel": ch, "instId": self.inst_id}
                await self._broadcast(ch, json.dumps({"arg": arg, "data": [d]}))

    async def serve(self, host: str, port: int, stop: asyncio.Event, ready: asyncio.Event | None = None):
        async with websockets.serve(self.handler, host, port, max_size=None) as srv:
            self.port = srv.sockets[0].getsockname()[1]
            log.info("假交易所在 ws://%s:%d 运行，加速 %.0f 倍", host, self.port, self.speed)
            if ready is not None:
                ready.set()
            await self.market_loop(stop)


def instrument(inst_id: str = "BTC-USDT-SWAP") -> dict:
    """合成行情用的合约信息，字段与 OKX 公共接口一致。"""
    return {"instType": "SWAP", "instId": inst_id, "ctVal": "0.01", "ctMult": "1", "ctValCcy": "BTC",
            "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1", "ctType": "linear", "settleCcy": "USDT"}


def serve_rest(host: str, port: int, inst_id: str = "BTC-USDT-SWAP"):
    """在后台线程起一个只回答合约信息接口的 HTTP 服务。"""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    body = json.dumps({"code": "0", "msg": "", "data": [instrument(inst_id)]}).encode()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api/v5/public/instruments"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((host, port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
