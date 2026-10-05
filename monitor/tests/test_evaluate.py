"""§11 阶段一判定：收益口径、去重、自助区间、三条标准。"""
import random

import pytest

from flowmon.bucket import day_of, iso_utc
from flowmon.evaluate import bootstrap_diff_ci, dedupe, evaluate, same_dir_return
from flowmon.schema import bucket_columns, event_columns
from flowmon.storage import DailyCsv

DAY = 86_400_000
T0 = 1_790_640_000_000  # 2026-09-29T00:00:00Z


def ev(cols, ts, kind, d, r_pct, tier=40.0, entry=100.0, no_trade=False):
    e = {c: None for c in cols}
    e.update(event_id=f"{kind}-{ts}-{d}-{tier}", kind=kind, time_utc=iso_utc(ts), ts_ms=ts, direction=d,
             tier=tier if kind == "signal" else None, mkt_fill_px=entry, no_trade=no_trade,
             px_300s=entry * (1 + r_pct * d / 100), px_60s=entry * (1 + r_pct * d / 200))
    return e


def write(cfg, events, days, warmup_days=0):
    cols = [c for c, _ in event_columns(cfg)]
    w = DailyCsv(cfg.data_dir / "events", cols)
    for e in sorted(events, key=lambda x: x["ts_ms"]):
        w.write(day_of(e["ts_ms"]), e)
    w.close()
    b = DailyCsv(cfg.data_dir / "buckets", [c for c, _ in bucket_columns(cfg)])
    rows = [(T0, True), (T0 + days * DAY, True)]
    if warmup_days:  # 预热期：比 T0 早，分数无效
        rows.insert(0, (T0 - warmup_days * DAY, False))
    for t, valid in rows:
        b.write(day_of(t), {"start_ms": t, "width_s": 15, "complete": True, "score_valid": valid})
    b.close()


def build(cfg, n_sig, sig_mean, ctrl_mean=0.0, days=15, seed=1, bad_week=False, shorts_bad=False,
          n_no_trade=0, no_trade_mean=-0.3, bad_quarter=None, warmup_days=0):
    rng = random.Random(seed)
    cols = [c for c, _ in event_columns(cfg)]
    out = []
    step = days * DAY // n_sig
    for i in range(n_sig):
        ts = T0 + i * step
        d = 1 if i % 2 else -1
        m = sig_mean
        if bad_week and ts < T0 + 7 * DAY:
            m = -sig_mean
        if shorts_bad and d < 0:
            m = -0.05
        if bad_quarter is not None and (ts - T0) * 4 // (days * DAY) == bad_quarter:
            m = -sig_mean
        out.append(ev(cols, ts, "signal", d, m + rng.gauss(0, 0.05)))
    for i in range(n_no_trade):  # 不交易条件下的信号，错开 1 分钟以内
        ts = T0 + i * step + 30_000
        out.append(ev(cols, ts, "signal", 1, no_trade_mean, no_trade=True))
        out[-1]["no_trade_reason"] = "flips" if i % 2 else "low_vol|flips"
    for h in range(days * 24):
        out.append(ev(cols, T0 + h * 3_600_000 + 1_800_000, "control", rng.choice((1, -1)),
                      ctrl_mean + rng.gauss(0, 0.1)))
    write(cfg, out, days, warmup_days)


@pytest.fixture
def cfg(cfg_factory):
    return cfg_factory(evaluation={"bootstrap_reps": 2000})


def test_return_uses_fill_price_and_direction():
    e = {"mkt_fill_px": 100.05, "px_300s": 100.15, "direction": 1}
    assert same_dir_return(e, 300) == pytest.approx((100.15 / 100.05 - 1) * 100)
    e = {"mkt_fill_px": 99.95, "px_300s": 99.85, "direction": -1}
    assert same_dir_return(e, 300) == pytest.approx((1 - 99.85 / 99.95) * 100)
    assert same_dir_return({"mkt_fill_px": None, "px_300s": 1, "direction": 1}, 300) is None


def test_dedupe_against_last_kept():
    es = [{"ts_ms": t * 1000} for t in (0, 200, 400, 700, 999)]
    assert [e["ts_ms"] // 1000 for e in dedupe(es, 300_000)] == [0, 400, 700]


def test_bootstrap_deterministic_and_sensible():
    rng = random.Random(0)
    a = [0.2 + rng.gauss(0, 0.05) for _ in range(200)]
    b = [rng.gauss(0, 0.05) for _ in range(200)]
    ci1 = bootstrap_diff_ci(a, b, 2000, 0.95, "s")
    assert ci1 == bootstrap_diff_ci(a, b, 2000, 0.95, "s")
    assert ci1[0] < 0.2 < ci1[1] and ci1[0] > 0.15


def test_pass(cfg):
    build(cfg, 320, 0.2)
    text, verdict = evaluate(cfg, cfg.data_dir)
    assert verdict is True, text
    assert "**通过**" in text


def test_fee_not_covered_fails_but_reference_row_shown(cfg):
    build(cfg, 320, 0.08)  # 高于对照组，但不够 0.10%；够 0.07%
    text, verdict = evaluate(cfg, cfg.data_dir)
    assert verdict is False
    assert "| 1 统计上有差别 |" in text and "| 2 够付手续费 | 信号均值 ≥ 0.10%" in text
    assert "0.07%：信号均值不低于它" in text


def test_short_side_negative_fails(cfg):
    build(cfg, 320, 0.3, shorts_bad=True)
    text, verdict = evaluate(cfg, cfg.data_dir)
    assert verdict is False and "| 3a 多空分开 |" in text


def test_time_segments_split(cfg):
    # 15 天等分 4 段，第 1 段为负 → 3 / 4 段为正，通过
    build(cfg, 320, 0.3, bad_quarter=0)
    text, verdict = evaluate(cfg, cfg.data_dir)
    assert "3 / 4 段为正" in text and verdict is True, text


def test_segments_start_at_first_valid_score(cfg):
    # 前面多 3 天分数无效的预热期：分段从第一个有效分数算起，4 段都有信号
    build(cfg, 320, 0.3, warmup_days=3)
    text, verdict = evaluate(cfg, cfg.data_dir)
    assert "4 / 4 段为正" in text and verdict is True, text
    assert "无信号" not in text
    assert "| 覆盖天数 | 18.0 天" in text  # 覆盖天数仍从开始记录算


def test_time_segments_two_negative_fails(cfg):
    build(cfg, 320, 0.4, bad_week=True)  # 前 7 天为负，覆盖第 1、2 段
    text, verdict = evaluate(cfg, cfg.data_dir)
    assert verdict is False
    assert "2 / 4 段为正" in text


def test_no_trade_signals_excluded_but_reported(cfg):
    # 320 条正常信号 + 100 条处于不交易条件的坏信号（均值 −0.3%）。
    # 坏信号与正常信号相距 30 秒：先筛选再去重，正常信号不会被它们挤掉
    build(cfg, 320, 0.2, n_no_trade=100)
    text, verdict = evaluate(cfg, cfg.data_dir)
    assert verdict is True, text
    assert "其中处于不交易条件 100 条（不进判定）" in text
    assert "| 参与判定的信号 | 320 条" in text
    assert "| 处于不交易条件的 | 100 | -0.3000% |" in text
    assert "| 　其中 分数翻转 | 100 |" in text and "| 　其中 低波动 | 50 |" in text


def test_min_signals_counted_after_filtering(cfg):
    build(cfg, 280, 0.3, n_no_trade=40)  # 共 320 条，但筛掉 40 条后只剩 280
    text, verdict = evaluate(cfg, cfg.data_dir)
    assert verdict is None and "数据量不够" in text


def test_not_enough_data_gives_no_verdict(cfg):
    build(cfg, 120, 0.3)
    text, verdict = evaluate(cfg, cfg.data_dir)
    assert verdict is None and "数据量不够" in text


def test_only_entry_tier_counts_and_dedupe_applies(cfg):
    cols = [c for c, _ in event_columns(cfg)]
    es = [ev(cols, T0, "signal", 1, 0.2), ev(cols, T0 + 60_000, "signal", 1, 5.0),     # 1 分钟后，去掉
          ev(cols, T0 + 10, "signal", 1, 9.0, tier=30.0),                                # 30 档，不计
          ev(cols, T0 + 3_600_000, "control", 1, 0.0), ev(cols, T0 + 7_200_000, "control", -1, 0.0)]
    write(cfg, es, 15)
    text, _ = evaluate(cfg, cfg.data_dir)
    assert "| 筛选、去重后 | 1 条" in text and "| 信号组均值 | 0.2000% |" in text
