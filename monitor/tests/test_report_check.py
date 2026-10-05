"""日报里的磁盘占用、停机时长；自检命令；重启后分数多久有效的估算。"""
import json

import pytest

from conftest import make_cfg
from flowmon import __main__ as cli
from flowmon.bucket import downtime_row, iso_utc
from flowmon.monitor import Monitor
from flowmon.report import build_daily, fmt_bytes
from flowmon.schema import bucket_columns
from flowmon.selfcheck import baseline_eta, run_check
from flowmon.sim import instrument
from flowmon.storage import DailyCsv
from test_local_run import T0, W, live_row

DAY = "2026-09-30"
D0 = 1_790_726_400_000  # 2026-09-30T00:00:00Z
H = 3_600_000


def test_fmt_bytes():
    assert fmt_bytes(1000) == "1000 B"
    assert fmt_bytes(2048) == "2.0 KB"
    assert fmt_bytes(5 * 1024 ** 2) == "5.0 MB"
    assert fmt_bytes(3 * 1024 ** 3) == "3.0 GB"


def test_daily_report_disk_and_downtime(tmp_path):
    cfg = make_cfg(tmp_path)
    d = cfg.data_dir
    w = DailyCsv(d / "buckets", [c for c, _ in bucket_columns(cfg)])
    for i in range(10):
        row, _ = live_row(D0 + i * W, i)
        w.write(DAY, {**row, "score_valid": False, "no_trade": False})
    for i in range(10, 14):
        w.write(DAY, {**downtime_row(D0 + i * W, W), "score_valid": False, "no_trade": True})
    w.close()
    for sub, n in (("raw/trades", 1000), ("raw/books", 2048), ("raw/misc", 300)):
        (d / sub).mkdir(parents=True, exist_ok=True)
        (d / sub / f"{DAY}.{'csv' if 'trades' in sub else 'jsonl'}").write_bytes(b"x" * n)
    (d / "raw" / "trades" / "2026-09-29.csv").write_bytes(b"x" * 999)  # 别的日子不算
    cfg.log_dir.mkdir(parents=True)
    (cfg.log_dir / f"flowmon.log.{DAY}").write_bytes(b"x" * 512)

    text, summary = build_daily(cfg, DAY)
    assert "| 运行时长 | 0.04 小时（记录 10 / 5760 个桶） |" in text
    assert "| 停机 | 0.02 小时（4 个占位桶，标为 downtime） |" in text
    bsize = (d / "buckets" / f"{DAY}.csv").stat().st_size
    total = 1000 + 2048 + 300 + bsize + 512
    assert (f"| 当天数据占用磁盘 | {fmt_bytes(total)}（逐笔成交 1000 B，盘口快照 2.0 KB，持仓量等原文 300 B，"
            f"桶表 {fmt_bytes(bsize)}，事件 0 B，日志 512 B） |") in text
    assert "数据目录合计 / 磁盘剩余" in text
    assert summary.endswith(f"当天数据 {fmt_bytes(total)}")


# ---------- 重启后分数多久有效 ----------

def _rows(spec):
    """spec: [(小时数, 完整?)] 依次接起来的桶。"""
    rows, t = [], 0
    for hours, ok in spec:
        for _ in range(int(hours * H // W)):
            rows.append({"start_ms": t, "complete": ok, "score_valid": False})
            t += W
    return rows


@pytest.fixture
def cfg24(tmp_path):
    return make_cfg(tmp_path)  # 起步值：基准 24 小时、回看 48 小时、80%


def test_eta_first_start(cfg24):
    rows = _rows([(1, True)])
    wu = baseline_eta(rows, cfg24)
    n = 24 * H // W
    assert wu.eta == (n - 1) * W + 4 * W  # 第一个桶算起满 24 小时，再等平滑窗口
    assert not wu.ready
    assert wu.fill == pytest.approx(len(rows) / n) == wu.recent


@pytest.mark.parametrize("outage_h", [3, 10, 28])
def test_eta_after_outage_up_to_28h_is_minutes(cfg24, outage_h):
    rows = _rows([(30, True), (outage_h, False)])
    wu = baseline_eta(rows, cfg24)
    last = rows[-1]["start_ms"]
    # 基准值马上就够；只等持仓量变化窗口（20 个桶）和平滑窗口（4 个）重新填满：重启后 24 个桶，即 6 分钟
    assert wu.ready and wu.fill == pytest.approx(min(1, (48 - outage_h) / 24))  # 回看 48 小时里停机前的完整桶
    assert wu.recent == pytest.approx(max(0, 1 - outage_h / 24))
    assert wu.eta == last + W + 24 * W


def test_eta_after_long_outage(cfg24):
    rows = _rows([(30, True), (36, False)])
    eta = baseline_eta(rows, cfg24).eta
    last = rows[-1]["start_ms"]
    # 停机超过 28.8 小时：回看 48 小时里只剩停机前 12 小时的完整桶，不够 19.2 小时（24 × 80%）。
    # 之后每进来一个新桶，就有一个停机前的桶滑出回看范围，要等新数据自己攒够 19.2 小时
    assert eta - last == pytest.approx(19.2 * H, abs=6 * W)


def test_eta_strict_lookback(tmp_path):
    cfg = make_cfg(tmp_path, score={"baseline_lookback_hours": 24})
    rows = _rows([(30, True), (10, False)])
    eta = baseline_eta(rows, cfg).eta
    # 严格的「过去 24 小时」：停机超过 4.8 小时，要等它滑出 24 小时窗口到只剩 4.8 小时，约 19.2 小时
    assert eta - rows[-1]["start_ms"] == pytest.approx(19.2 * H, abs=6 * W)


# ---------- 自检 ----------

def _first_hour(tmp_path, **cfg_kw):
    """模拟启动后 70 分钟：真实的分数引擎、持仓量推送、日志、心跳状态。"""
    cfg = make_cfg(tmp_path, heartbeat={"url": "https://hc-ping.com/1111-2222-secret"}, **cfg_kw)
    m = Monitor(cfg, instrument())
    n = 280
    for i in range(n):
        s = T0 + i * W
        m._on_bucket(*live_row(s, i))
        for k in range(5):
            ts = s + k * 3000
            m._misc(ts, {"type": "oi", "ts": ts, "recv": ts + 40, "oi": "5000"})
    end = T0 + n * W
    m.hb["last_ok_ms"] = end - 20_000
    m.started_ms = T0
    m._write_health()
    m.close()
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    (cfg.log_dir / "flowmon.log").write_text(
        f"{iso_utc(end - 600_000)[:19].replace('T', ' ')},000 WARNING flowmon.okx: 连接断开：stale\n"
        f"{iso_utc(T0 - 7200_000)[:19].replace('T', ' ')},000 ERROR flowmon.monitor: 一小时前的错误不算\n",
        encoding="utf-8")
    return cfg, end


def test_check_first_hour(tmp_path):
    cfg, end = _first_hour(tmp_path)
    now = end + 5_000
    text, ok = run_check(cfg, 60, now_ms=now)
    assert not ok  # 没有进程锁 = 监控器没在运行
    assert "[不通过] 监控器没在运行" in text
    assert "[通过] 最新的桶 2026-09-30T00:49:45，5 秒前结束" in text
    assert "[通过] 最近 60 分钟：240 / 240 个桶，完整 240 个（100.0%）" in text
    assert "[通过] 成交：240 / 240 个完整桶有成交" in text
    assert "[通过] 盘口：240 / 240 个完整桶有买一卖一" in text
    assert "[通过] 持仓量：1200 条推送（约每 3.0 秒一条）" in text
    assert "[通过] 分数在计算：最近 10 分钟 41 / 41 个完整桶算出了 S" in text
    n = 24 * H // W
    eta = T0 + (n - 1) * W + 4 * W
    assert f"预热中，基准值够了 5%；之后数据都完整的话最早 {iso_utc(eta)[:16]}Z 有效" in text
    assert "过去 24 小时里完整的桶占 4.9%" in text
    assert "[通过] 数据延迟：中位 40 ms" in text
    assert "[通过] 日志：错误 0 条，警告 1 条" in text
    assert "[通过] 心跳：最近一次报到成功在 25 秒前" in text
    assert "secret" not in text and "https://hc-ping.com/…" in text
    assert "S 范围" in text and "最近 20 个桶" in text
    saved = sorted((cfg.data_dir / "reports").glob("check-*.txt"))
    assert len(saved) == 1 and saved[0].read_text(encoding="utf-8").startswith("flowmon 自检")


def test_check_flags_stale_data_and_missing_heartbeat(tmp_path):
    cfg, end = _first_hour(tmp_path)
    h = cfg.data_dir / "state" / "health.json"
    st = json.loads(h.read_text(encoding="utf-8"))
    st["heartbeat"]["last_ok_utc"] = None
    st["heartbeat"]["last_err"] = "URLError: timed out"
    h.write_text(json.dumps(st), encoding="utf-8")
    text, ok = run_check(cfg, 60, now_ms=end + 300_000)
    assert "[不通过] 最新的桶" in text and "300 秒前结束" in text
    assert "[不通过] 心跳：还没有报到成功过；最近一次失败：URLError: timed out" in text


def test_check_command_without_data(tmp_path, capsys):
    p = tmp_path / "c.toml"
    p.write_text((__import__("conftest").ROOT / "config.example.toml").read_text(encoding="utf-8"), encoding="utf-8")
    assert cli.main(["check", "--config", str(p)]) == 1
    assert "还没有任何桶数据" in capsys.readouterr().out


@pytest.mark.parametrize("outage_h", [10, 36])
def test_eta_matches_score_engine(cfg24, outage_h):
    """估算和真实的分数引擎对得上：按起步值（24 小时基准、回看 48 小时、80%），停机后接着喂完整的桶，
    看引擎第一次给出有效分数的时刻。"""
    from flowmon.score import score_series

    def full(t, i):
        return {"start_ms": t, "complete": True, "buy_vol": 1.0 + (i % 5), "sell_vol": 1.0 + (i % 3),
                "close": 100.0 + (i % 7), "oi": 1000.0 + (i % 11) * 3, "score_valid": False}

    rows, t, i = [], 0, 0
    for _ in range(30 * H // W):
        rows.append(full(t, i))
        t, i = t + W, i + 1
    for _ in range(outage_h * H // W):
        rows.append({"start_ms": t, "complete": False, "buy_vol": None, "sell_vol": None, "close": None, "oi": None,
                     "score_valid": False})
        t += W
    eta = baseline_eta(rows, cfg24).eta
    restart = t
    for _ in range(21 * H // W):
        rows.append(full(t, i))
        t, i = t + W, i + 1
    res = score_series(rows, cfg24.score, 15)
    first_valid = next(r["start_ms"] + W for r, x in zip(rows, res) if r["start_ms"] >= restart and x.valid)
    assert eta == first_valid, (iso_utc(eta), iso_utc(first_valid))


@pytest.mark.parametrize("seed", range(150))
def test_eta_is_exact_on_random_histories(cfg_factory, seed):
    """随机的历史（零星不完整、成段停机、缺行），之后都完整：估算和分数引擎给出的第一个有效时刻完全一致。"""
    import random

    from flowmon.score import score_series

    rng = random.Random(seed)
    base_h = 40 * 15 / 3600  # 40 个桶的基准，回看 1–3 倍
    cfg = cfg_factory(score={"baseline_hours": base_h, "baseline_lookback_hours": base_h * rng.choice([1, 2, 3])})

    def full(t):
        return {"start_ms": t, "complete": True, "buy_vol": rng.random() * 5 + 0.1, "sell_vol": rng.random() * 5 + 0.1,
                "close": 100 + rng.gauss(0, 1), "oi": 1000 + rng.gauss(0, 20), "score_valid": False}

    rows, t = [], 0
    for _ in range(rng.randint(10, 200)):
        x = rng.random()
        if x < 0.04:
            t += W * rng.randint(1, 3)  # 缺行
            continue
        if x < 0.06:  # 一段停机占位
            for _ in range(rng.randint(5, 60)):
                rows.append({"start_ms": t, "complete": False, "buy_vol": None, "sell_vol": None, "close": None,
                             "oi": None, "score_valid": False})
                t += W
            continue
        r = full(t)
        r["complete"] = rng.random() > 0.05
        rows.append(r)
        t += W
    if not rows:
        return
    eta = baseline_eta(rows, cfg).eta
    last = rows[-1]["start_ms"]
    future = [full(last + k * W) for k in range(1, 400)]
    res = score_series(rows + future, cfg.score, 15)
    got = next((r["start_ms"] + W for r, x in zip(rows + future, res) if r["start_ms"] > last and x.valid), None)
    assert eta == got
