"""力量分数：手算核对 + 和一份逐字照规格写的朴素实现逐桶比对。"""
import math
import random

import pytest

from flowmon.score import ScoreEngine, score_series

W = 15_000
# 40 个桶 = 10 分钟的基准窗口，测试里好算
BASE_H = 40 * 15 / 3600


def bucket(i, buy, sell, close, oi, complete=True):
    return {"start_ms": i * W, "complete": complete, "buy_vol": buy, "sell_vol": sell,
            "close": close, "oi": oi}


@pytest.fixture
def scfg(cfg_factory):
    return cfg_factory(score={"baseline_hours": BASE_H}).score


def test_flow_volume_hand_calc(scfg):
    # 0–39 号桶：买 1 卖 1；40–43 号桶：买 3 卖 1
    rows = [bucket(i, 1, 1, 100, 1000 + (i % 3)) for i in range(40)]
    rows += [bucket(i, 3, 1, 100 + i - 39, 1000 + (i % 3) + 10 * (i - 39)) for i in range(40, 44)]
    r = score_series(rows, scfg, 15)[-1]
    # F = (12 − 4) / 16
    assert r.F == pytest.approx(0.5)
    # 基准窗口 = 4–43 号桶：36×2 + 4×4 = 88，共 10 分钟 → 每分钟 8.8；最近 1 分钟 16 → M = 16 / 8.8
    assert r.M == pytest.approx(16 / 8.8)
    assert r.A == pytest.approx(0.5 * 16 / 8.8)
    # 价格变化：43 号收盘 104 − 39 号收盘 100
    assert r.price_chg == pytest.approx(4)


def _flat(n, oi_fn=lambda i: 1000 + (i % 3) * 5):
    return [bucket(i, 1, 1, 100, oi_fn(i)) for i in range(n)]


def test_volume_multiple_capped(scfg):
    rows = _flat(40) + [bucket(i, 100, 0, 101 + i, 2000 + i) for i in range(40, 44)]
    r = score_series(rows, scfg, 15)[-1]
    assert r.M == scfg.volume_multiple_cap
    assert r.A == 1.0  # F=1, M=3 → 截到 1


def _spike(scfg, buy, sell, dprice, doi):
    """44 个平稳桶后，最后一个桶放量并让持仓量跳变；只有它的 5 分钟变化量是离群值。"""
    rows = _flat(47) + [bucket(47, buy, sell, 100 + dprice, 1000 + (47 % 3) * 5 + doi)]
    return score_series(rows, scfg, 15)


def test_strong_long_full_score(scfg):
    # 主动买压倒、放量、持仓大增、价格上涨 → A=1、B=1 → R=100
    res = _spike(scfg, 100, 0, 1, 500)
    r = res[-1]
    assert r.A == 1.0 and r.Z > scfg.oi_z_divisor and r.B == 1.0
    assert r.R_raw == pytest.approx(100)
    assert not r.fix1 and not r.fix2
    assert r.R == pytest.approx(100)
    assert r.S == pytest.approx(sum(x.R for x in res[-4:]) / 4)
    assert r.valid


def test_fix1_no_new_money_caps_at_30(scfg):
    # 主动买很强但持仓量减少：B=0，R = 50，被限制到 30
    r = _spike(scfg, 100, 0, 1, -500)[-1]
    assert r.oi_chg < 0 and r.B == 0
    assert r.R_raw == pytest.approx(50)
    assert r.fix1
    assert r.R == pytest.approx(scfg.no_new_money_cap)


def test_fix2_price_disagrees_halves(scfg):
    r = _spike(scfg, 100, 0, -1, 500)[-1]
    assert r.price_chg < 0 and r.R_raw == pytest.approx(100)
    assert r.fix2
    assert r.R == pytest.approx(100 * scfg.price_disagree_factor)


def test_fix1_then_fix2_order(scfg):
    # 持仓减少先限到 30，价格反向再减半 → 15
    r = _spike(scfg, 100, 0, -1, -500)[-1]
    assert r.fix1 and r.fix2
    assert r.R == pytest.approx(scfg.no_new_money_cap * scfg.price_disagree_factor)


def test_short_side_symmetric(scfg):
    r = _spike(scfg, 0, 100, -1, 500)[-1]
    assert r.A == -1.0 and r.B == -1.0
    assert r.R == pytest.approx(-100)


def test_warmup_until_full_baseline(scfg):
    res = score_series(_flat(60), scfg, 15)
    # 第 39 号桶时历史刚好铺满 40 个桶
    assert all(not r.valid for r in res[:39])
    assert "warmup" in res[30].note
    assert res[39].S is not None
    # 平滑窗口里 4 个 R 都要在基准有效之后
    assert not res[41].valid and res[42].valid


def test_incomplete_bucket_breaks_windows(scfg):
    rows = _flat(60)
    rows[50]["complete"] = False
    res = score_series(rows, scfg, 15)
    assert res[50].R is None and "incomplete" in res[50].note
    # 50 号在后面 3 个桶的成交窗口里
    assert all(res[i].F is None for i in range(50, 54))
    assert res[54].F is not None
    # 平滑窗口要 4 个有效 R
    assert all(res[i].S is None for i in range(50, 57))
    # 持仓量变化：70 号桶回看 50 号
    rows += [bucket(i, 1, 1, 100, 1000 + (i % 3) * 5) for i in range(60, 75)]
    res = score_series(rows, scfg, 15)
    assert res[70].oi_chg is None and "oi_window_gap" in res[70].note


def test_time_gap_is_not_bridged(scfg):
    rows = _flat(50) + [bucket(i, 1, 1, 100, 1000) for i in range(52, 60)]  # 缺 50、51 号
    res = score_series(rows, scfg, 15)
    first_after = res[50]  # 52 号桶
    assert first_after.F is None


def test_rejects_out_of_order(scfg):
    eng = ScoreEngine(scfg, 15)
    eng.update(bucket(5, 1, 1, 100, 1000))
    with pytest.raises(ValueError):
        eng.update(bucket(5, 1, 1, 100, 1000))


# ---------- 朴素实现：逐字照第 6 节，每个桶都把窗口从头扫一遍 ----------

def naive(rows, c, width_s):
    w = width_s * 1000
    n = round(c.baseline_hours * 3600 / width_s)
    by = {r["start_ms"]: r for r in rows}
    out = []
    R = {}
    doi_hist = []  # (t, 变化量)

    def ok(t):
        return t in by and by[t]["complete"]

    for r in rows:
        s = r["start_ms"]
        res = {}
        lo = s - n * w
        win = [x for x in rows if lo < x["start_ms"] <= s]
        comp = [x for x in win if x["complete"]]
        span = win and win[0]["start_ms"] <= s - (n - 1) * w
        ready = bool(span) and len(comp) >= c.baseline_min_coverage * n
        fw = [s - k * w for k in range(c.flow_window_buckets)]
        F = M = A = Z = B = Rv = None
        if all(ok(t) for t in fw):
            b = sum(by[t]["buy_vol"] for t in fw)
            se = sum(by[t]["sell_vol"] for t in fw)
            F = (b - se) / (b + se) if b + se > 0 else 0.0
            per_min = sum(x["buy_vol"] + x["sell_vol"] for x in comp) / (len(comp) * width_s / 60)
            if per_min > 0:
                M = min(c.volume_multiple_cap, ((b + se) / (len(fw) * width_s / 60)) / per_min)
                A = max(-1.0, min(1.0, F * M))
        t0 = s - c.oi_window_buckets * w
        d = None
        if r["complete"] and ok(t0):
            d = r["oi"] - by[t0]["oi"]
            doi_hist.append((s, d))
            vals = [v for t, v in doi_hist if lo < t <= s]
            if len(vals) >= 2:
                m = sum(vals) / len(vals)
                sd = math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))
                if sd > 0:
                    Z = d / sd
                    if F is not None:
                        B = max(0.0, min(1.0, max(Z, 0) / c.oi_z_divisor)) * ((F > 0) - (F < 0))
        tp = s - c.flow_window_buckets * w
        pc = r["close"] - by[tp]["close"] if ok(tp) else None
        if A is not None and B is not None and pc is not None:
            Rv = 100 * (c.weight_flow * A + c.weight_oi * B)
            if d <= 0:
                Rv = max(-c.no_new_money_cap, min(c.no_new_money_cap, Rv))
            if pc != 0 and Rv != 0 and (pc > 0) != (Rv > 0):
                Rv *= c.price_disagree_factor
        R[s] = (Rv, Rv is not None and ready and r["complete"])
        sw = [s - k * w for k in range(c.smooth_buckets)]
        xs = [R.get(t) for t in sw]
        S = valid = None
        if all(x is not None and x[0] is not None for x in xs):
            S = sum(x[0] for x in xs) / len(xs)
            valid = all(x[1] for x in xs)
        res.update(F=F, M=M, A=A, Z=Z, B=B, R=Rv, S=S, valid=bool(valid))
        out.append(res)
    return out


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_matches_naive_reference(scfg, seed):
    rng = random.Random(seed)
    rows, oi, px = [], 10_000.0, 100.0
    i = 0
    while len(rows) < 260:
        i += 1
        if rng.random() < 0.03:
            continue  # 偶尔缺桶
        oi += rng.gauss(0, 20) + (30 if 120 < i < 140 else 0)
        px += rng.gauss(0, 0.3)
        rows.append(bucket(i, rng.random() * 5, rng.random() * 5, px, oi, rng.random() > 0.05))
    got = score_series(rows, scfg, 15)
    ref = naive(rows, scfg, 15)
    for g, r in zip(got, ref):
        for k in ("F", "M", "A", "Z", "B", "R", "S"):
            a, b = getattr(g, k), r[k]
            assert (a is None) == (b is None), k
            if a is not None:
                assert a == pytest.approx(b, rel=1e-9, abs=1e-9), k
        assert g.valid == r["valid"]
    assert sum(g.valid for g in got) > 50  # 确实覆盖到了有效分数
