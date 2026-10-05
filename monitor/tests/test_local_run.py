"""在自己的电脑上长期运行要用到的几样：停机占位桶、跨天补日报、睡眠检测、心跳报到、进程锁、异常退出提醒。"""
import asyncio
import csv
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import make_cfg
from flowmon.bucket import BookCapture, Bucket
from flowmon.heartbeat import ping, redact
from flowmon.monitor import DOWN_ALL, AlreadyRunning, Monitor
from flowmon.selfcheck import monitor_running
from flowmon.sim import FakeOkx, instrument, serve_rest
from flowmon.storage import save_json

W = 15_000
DAY = 86_400_000
T0 = 1_790_725_200_000  # 2026-09-29T23:40:00Z


def live_row(s, i=0):
    """一个完整的桶：成交、持仓量都有，量和持仓量随 i 变化，分数能算出来。"""
    b = Bucket(s, W)
    b.add_trade(s + 1000, 60000.0 + (i % 7), 10 + (i % 5) * 3, "buy", 1)
    b.add_trade(s + 2000, 60000.0 + (i % 5), 8 + (i % 3) * 2, "sell", 1)
    b.latencies.append(40.0)
    b.capture = BookCapture(ts=s + W - 100, seq=i, bids=[["59999.9", "5", "0", "1"]], asks=[["60000.1", "5", "0", "1"]],
                            bid1=59999.9, ask1=60000.1, bid1_sz=5, ask1_sz=5, near_bid=50, near_ask=50,
                            near_truncated=False)
    row = b.finish(60000.0, (s + W - 1000, 5000.0 + (i % 11) * 3), 0.0001, 0.01, 30_000, judge=False)
    return row, b


def read_rows(data, sub="buckets"):
    rows = []
    for p in sorted((data / sub).glob("*.csv")):
        with p.open(encoding="utf-8") as f:
            rows += list(csv.DictReader(f))
    return rows


def misc_records(data):
    out = []
    for p in sorted((data / "raw" / "misc").glob("*.jsonl")):
        out += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    return out


def small_cfg(tmp_path, **kw):
    sections = {"score": {"baseline_hours": 0.1}, "events": {"followup_minutes": 5, "price_horizons_s": [15, 60, 300]},
                "notify": {"on_start": False}}
    for k, v in kw.items():
        sections.setdefault(k, {}).update(v)
    return make_cfg(tmp_path, **sections)


# ---------- 停机占位桶 ----------

def test_restart_fills_downtime_and_writes_missed_reports(tmp_path):
    cfg = small_cfg(tmp_path)
    m1 = Monitor(cfg, instrument())
    m1.restore()
    for i in range(40):  # 23:40 – 23:50
        m1._on_bucket(*live_row(T0 + i * W, i))
    m1.close()

    m2 = Monitor(cfg, instrument())
    m2.restore()
    assert m2.last_start == T0 + 39 * W
    T1 = T0 + 2 * DAY + 3_600_000  # 两天零一小时后重启：2026-10-02T00:40Z
    m2._on_bucket(*live_row(T1, 0))
    m2.close()

    rows = read_rows(cfg.data_dir)
    starts = [int(r["start_ms"]) for r in rows]
    assert starts == list(range(T0, T1 + W, W)), "停机那段每个桶都要有一行"
    down = [r for r in rows if r["incomplete_reason"] == "downtime"]
    assert len(down) == (T1 - (T0 + 40 * W)) // W
    assert all(r["complete"] == "0" and r["close"] == "" and r["buy_vol"] == "" and r["score_valid"] == "0"
               for r in down)
    recs = [m for m in misc_records(cfg.data_dir) if m.get("type") == "bucket" and m["why"] == "downtime"]
    assert len(recs) == len(down) and not any(m["ok"] for m in recs)
    # 停机前、停机中、重启当天之前的每一天都补了日报
    for d in ("2026-09-29", "2026-09-30", "2026-10-01"):
        assert (cfg.data_dir / "reports" / f"{d}.md").exists(), d
    rep = (cfg.data_dir / "reports" / "2026-09-30.md").read_text(encoding="utf-8")
    assert "| 运行时长 | 0.00 小时" in rep and "| 停机 | 24.00 小时" in rep


def test_downtime_fill_is_capped_at_restore_days(tmp_path):
    cfg = small_cfg(tmp_path, storage={"restore_days": 2})
    m1 = Monitor(cfg, instrument())
    for i in range(10):
        m1._on_bucket(*live_row(T0 + i * W, i))
    m1.close()
    m2 = Monitor(cfg, instrument())
    m2.restore()
    T1 = T0 + 10 * W + 3 * DAY
    m2._on_bucket(*live_row(T1))
    m2.close()
    down = [int(r["start_ms"]) for r in read_rows(cfg.data_dir) if r["incomplete_reason"] == "downtime"]
    assert len(down) == 2 * DAY // W and down[0] == T1 - 2 * DAY and down[-1] == T1 - W


# ---------- 睡眠检测 ----------

def test_clock_jump_is_detected_as_sleep(tmp_path):
    m = Monitor(small_cfg(tmp_path), instrument())
    clock = {"wall": T0 / 1000, "mono": 100.0}
    m._wall = lambda: clock["wall"]
    m._mono = lambda: clock["mono"]
    m._check_clock()
    clock["wall"] += 5
    clock["mono"] += 5
    m._check_clock()
    assert not m.agg.down and m.feed.reconnect_reason is None
    m.agg._bucket(T0)  # 让聚合器从 T0 开始
    clock["wall"] += 600  # 系统时钟走了 10 分钟，进程计时只走了 1 秒：睡眠了
    clock["mono"] += 1
    m._check_clock()
    assert "sleep" in m.agg.down and "sleep" in DOWN_ALL
    assert m.feed.reconnect_reason == "sleep"
    assert len(m.sleeps) == 1 and m.sleeps[0]["seconds"] == pytest.approx(599)
    out = m.agg.advance(T0 + 40 * W, m.book)
    assert len(out) == 39 and all("sleep" in row["incomplete_reason"] for row, _ in out)
    # 重连拿到新快照后恢复（快照时刻所在的桶仍算不完整，之后的桶恢复）
    m.agg.clear_down(DOWN_ALL, T0 + 40 * W)
    later = [row for row, _ in m.agg.advance(T0 + 60 * W, m.book) if row["start_ms"] > T0 + 40 * W]
    assert later and all("sleep" not in row["incomplete_reason"] for row in later)


def test_sleep_forces_reconnect_and_recovers(tmp_path):
    """实时链路：模拟醒来后，强制重连、拿到新快照，之后的桶恢复完整。"""
    async def main():
        rest = serve_rest("127.0.0.1", 0)
        stop, ready = asyncio.Event(), asyncio.Event()
        fake = FakeOkx(speed=60, seed=5)
        srv = asyncio.create_task(fake.serve("127.0.0.1", 0, stop, ready))
        await ready.wait()
        cfg = small_cfg(tmp_path, exchange={"ws_public_url": f"ws://127.0.0.1:{fake.port}/ws/v5/public",
                                            "rest_base_url": f"http://127.0.0.1:{rest.server_address[1]}"})
        m = Monitor(cfg, instrument())
        m.restore()

        async def doze():
            await asyncio.sleep(3)
            now = time.time()
            m._on_sleep(now - 1, now, 30.0)

        t = asyncio.create_task(doze())
        await m.run(duration_s=7)
        await t
        stop.set()
        await srv
        rest.shutdown()
        return cfg

    cfg = asyncio.run(main())
    closes = [x for x in misc_records(cfg.data_dir) if x.get("type") == "conn" and x.get("event") == "close"]
    assert any(x.get("reason") == "sleep" for x in closes)
    rows = read_rows(cfg.data_dir)
    i = next(k for k, r in enumerate(rows) if "sleep" in r["incomplete_reason"])
    assert any(r["complete"] == "1" for r in rows[i + 1:]), "重连后桶要恢复完整"


# ---------- 心跳 ----------

class _Hits(BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):
        _Hits.hits.append(self.path)
        code = 200 if self.path.startswith("/ok") else 500
        self.send_response(code)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *a):
        pass


@pytest.fixture
def hb_server():
    _Hits.hits = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Hits)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_ping_reports_errors(hb_server):
    assert ping(hb_server + "/ok", 2) is None
    assert ping(hb_server + "/bad", 2) == "HTTP 500"
    assert ping("http://127.0.0.1:9/x", 1) is not None
    assert redact("https://hc-ping.com/1234-secret") == "https://hc-ping.com/…"


def test_heartbeat_only_while_data_flows(tmp_path, hb_server):
    cfg = small_cfg(tmp_path, heartbeat={"url": hb_server + "/ok", "interval_s": 0.2, "timeout_s": 0.1,
                                         "max_data_age_s": 30})
    m = Monitor(cfg, instrument())

    async def main():
        m.last_complete_wall = time.time()
        t = asyncio.create_task(m._heartbeat())
        await asyncio.sleep(1.1)
        n_fresh = len(_Hits.hits)
        m.last_complete_wall = time.time() - 100  # 行情断了 100 秒
        await asyncio.sleep(0.3)
        n_mark = len(_Hits.hits)
        await asyncio.sleep(0.8)
        m.stop.set()
        await t
        return n_fresh, n_mark

    n_fresh, n_mark = asyncio.run(main())
    assert n_fresh >= 3
    assert len(_Hits.hits) == n_mark, "行情断了就不再报到"
    assert m.hb["paused_since_ms"] is not None and m.hb["last_ok_ms"] is not None
    health = json.loads((cfg.data_dir / "state" / "health.json").read_text(encoding="utf-8"))
    assert health["heartbeat"]["configured"] and health["heartbeat"]["last_ok_utc"]
    assert health["heartbeat"]["paused_since_utc"]


def test_heartbeat_failures_are_counted(tmp_path, hb_server):
    cfg = small_cfg(tmp_path, heartbeat={"url": hb_server + "/bad", "interval_s": 0.2, "timeout_s": 0.1,
                                         "max_data_age_s": 30})
    m = Monitor(cfg, instrument())

    async def main():
        m.last_complete_wall = time.time()
        t = asyncio.create_task(m._heartbeat())
        await asyncio.sleep(0.7)
        m.stop.set()
        await t

    asyncio.run(main())
    assert m.hb["fail_streak"] >= 2 and m.hb["last_err"] == "HTTP 500" and m.hb["last_ok_ms"] is None


# ---------- 进程锁、异常退出提醒 ----------

def test_lock_prevents_second_monitor(tmp_path):
    cfg = small_cfg(tmp_path)
    m1, m2 = Monitor(cfg, instrument()), Monitor(cfg, instrument())
    assert not monitor_running(cfg)
    m1.lock()
    assert monitor_running(cfg)
    with pytest.raises(AlreadyRunning):
        m2.lock()
    m1.unlock()
    assert not monitor_running(cfg)
    m2.lock()
    m2.unlock()


def _run_briefly(cfg, sent):
    m = Monitor(cfg, instrument())
    m.notifier.send = lambda title, body, key=None: sent.append(title) or True
    m.restore()
    asyncio.run(m.run(duration_s=1))
    return m


def test_unclean_previous_exit_is_reported(tmp_path):
    # 连不上交易所也没关系，这里只看启动和停止
    cfg = small_cfg(tmp_path, exchange={"ws_public_url": "ws://127.0.0.1:9/ws"}, notify={"on_start": True},
                    connection={"reconnect_backoff_min_s": 0.2})
    marker = cfg.data_dir / "state" / "running.json"
    save_json(marker, {"pid": 1, "started_utc": "2026-10-01T00:00:00.000Z"})
    sent = []
    _run_briefly(cfg, sent)
    assert sent[0].startswith("已重新启动（上次没有正常停止）") and "已停止" in sent
    assert not marker.exists(), "正常停止后删掉运行标记"
    sent2 = []
    _run_briefly(cfg, sent2)
    assert sent2[0] == "已启动"
