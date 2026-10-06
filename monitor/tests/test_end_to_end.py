"""端到端：假交易所（加速 60 倍）→ 实时监控器（中途重启一次，停机那段补占位桶）→ 回放 → 与实时逐桶比对。

注入三种故障：盘口 seqId 断档、服务端断线、停推。约 35 秒。
"""
import asyncio
import csv

from conftest import make_cfg
from flowmon.monitor import Monitor
from flowmon.okx import load_instrument
from flowmon.replay import Replayer, compare
from flowmon.sim import FakeOkx, serve_rest

SPEED = 60


def days_of(data):
    return sorted(p.stem for p in (data / "buckets").glob("*.csv"))


def test_live_restart_replay_consistency(tmp_path):
    async def main():
        rest = serve_rest("127.0.0.1", 0)
        stop = asyncio.Event()
        ready = asyncio.Event()
        # 故障时间是模拟秒（真实秒 × 60）：第一次运行里断档；重启后断线、停推（真实 8 秒，超过 5 秒判定断流）
        fake = FakeOkx(speed=SPEED, seed=11, faults=[(300, "seq_gap"), (1100, "disconnect"), (1400, "silence")])
        srv = asyncio.create_task(fake.serve("127.0.0.1", 0, stop, ready))
        await ready.wait()
        cfg = make_cfg(
            tmp_path,
            exchange={"ws_public_url": f"ws://127.0.0.1:{fake.port}/ws/v5/public",
                      "rest_base_url": f"http://127.0.0.1:{rest.server_address[1]}"},
            score={"baseline_hours": 0.1},  # 24 个桶 = 6 分钟
            events={"followup_minutes": 5, "price_horizons_s": [15, 60, 300]},
            notify={"on_start": False},
        )
        inst = load_instrument(cfg)
        assert inst["ctVal"] == "0.01"
        m1 = Monitor(cfg, inst)
        m1.restore()
        await m1.run(duration_s=14)
        await asyncio.sleep(1)  # 停机 1 秒 = 模拟 1 分钟
        m2 = Monitor(cfg, inst)
        m2.restore()
        assert m2.agg.min_start > 0
        await m2.run(duration_s=16)
        stop.set()
        await srv
        rest.shutdown()
        return cfg, m1, m2

    cfg, m1, m2 = asyncio.run(main())
    data = cfg.data_dir
    days = days_of(data)
    rows = []
    for d in days:
        for p in sorted((data / "buckets").glob(f"{d}*.csv")):
            rows += list(csv.DictReader(p.open()))
    starts = [int(r["start_ms"]) for r in rows]
    assert starts == sorted(set(starts)), "桶不能重复、不能乱序"
    w = cfg.bucket.width_s * 1000
    assert all(b - a == w for a, b in zip(starts, starts[1:])), "停机那段也要有占位桶，桶表连续"
    down = [r for r in rows if r["incomplete_reason"] == "downtime"]
    assert len(down) >= 2, "停机 1 秒 = 模拟 1 分钟，至少 2 个占位桶"
    assert all(r["complete"] == "0" and r["close"] == "" and r["score_valid"] == "0" for r in down)
    reasons = "|".join(r["incomplete_reason"] for r in rows)
    for k in ("startup", "book_invalid", "disconnect", "stale"):
        assert k in reasons, k
    # 约 120 个桶里，预热、停推（模拟 8 分钟）、断线、重启以及它们之后的窗口都没有有效分数；
    # 这里只确认有效分数存在，比对才有意义
    assert sum(r["score_valid"] == "1" for r in rows) >= 10
    assert m1.book_errors >= 1
    assert m1.n_signals + m2.n_signals > 0

    out = tmp_path / "replay"
    stats = Replayer(cfg, data, out).run(days[0], days[-1])
    assert stats["skipped"] == 0  # 两次运行之间的空档有占位桶，回放也生成同样的占位桶
    cmp = compare(cfg, data, out, days)
    assert cmp["buckets_common"] == len(rows)
    assert cmp["score_mismatch"] == 0, cmp
    assert cmp["events_only_live"] == [], cmp
    # 停机那一刻没封的桶（原始数据里有它的部分成交）：实时是空的占位桶，回放也要是
    rep = {}
    for p in sorted((out / "buckets").glob("*.csv")):
        rep.update({r["start_ms"]: r for r in csv.DictReader(p.open())})
    for r in down:
        assert rep[r["start_ms"]]["incomplete_reason"] == "downtime" and rep[r["start_ms"]]["close"] == "", r


def test_replay_from_second_day_matches_live(tmp_path):
    """从中间某天开始回放：先用之前存下的实时桶预热分数，和实时逐桶对得上。

    行情从 UTC 零点前 16 分钟开始（加速 60 倍），第一天里停推 8 分钟，让第二天开头的基准值要回看到第一天。
    """
    import time as _time
    from datetime import datetime, timedelta, timezone

    now = datetime.fromtimestamp(_time.time() + 600, tz=timezone.utc)
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) + timedelta(days=1)
    start = int((midnight - timedelta(minutes=16)).timestamp() * 1000)  # 模拟时间要走在真实时间前面

    async def main():
        rest = serve_rest("127.0.0.1", 0)
        stop, ready = asyncio.Event(), asyncio.Event()
        fake = FakeOkx(speed=SPEED, seed=21, start_ms=start, faults=[(240, "silence")])
        srv = asyncio.create_task(fake.serve("127.0.0.1", 0, stop, ready))
        await ready.wait()
        cfg = make_cfg(
            tmp_path,
            exchange={"ws_public_url": f"ws://127.0.0.1:{fake.port}/ws/v5/public",
                      "rest_base_url": f"http://127.0.0.1:{rest.server_address[1]}"},
            score={"baseline_hours": 0.1, "baseline_lookback_hours": 0.3},  # 24 个桶，回看 72 个桶
            events={"followup_minutes": 5, "price_horizons_s": [15, 60, 300]},
            notify={"on_start": False},
        )
        m = Monitor(cfg, load_instrument(cfg))
        m.restore()
        await m.run(duration_s=30)
        stop.set()
        await srv
        rest.shutdown()
        return cfg

    cfg = asyncio.run(main())
    data = cfg.data_dir
    days = days_of(data)
    assert len(days) == 2, days
    day2 = days[1]

    out = tmp_path / "replay"
    r0 = Replayer(cfg, data, out)
    stats = r0.run(day2, day2)
    assert stats["warmup"] > 0
    # 预热时带上最后一个收盘：回放第一个桶没有成交时价格和实时一样沿用它
    from flowmon.score import ScoreEngine
    from flowmon.conditions import Calendar, Conditions
    c = cfg.conditions
    _, close = r0.warm_up(ScoreEngine(cfg.score, 15), Conditions(c, 15, Calendar(None, 0, 0)), day2)
    day1_rows = list(csv.DictReader((data / "buckets" / f"{days[0]}.csv").open()))
    assert close == float([x["close"] for x in day1_rows if x["close"]][-1])
    cmp = compare(cfg, data, out, [day2])
    assert cmp["buckets_common"] == cmp["buckets_live"] > 0
    assert cmp["score_mismatch"] == 0, cmp

    # 不预热就对不上：确认这个测试确实测到了预热
    cold = tmp_path / "replay_cold"
    r = Replayer(cfg, data, cold)
    r.warm_up = lambda *a: (0, None)
    r.run(day2, day2)
    assert compare(cfg, data, cold, [day2])["score_mismatch"] > 0
