"""爆仓模块的配置、数据结构和落盘。"""
import dataclasses
import tomllib

import pytest

from conftest import ROOT
from flowmon.config import ConfigError
from flowmon.liq import config as lconf
from flowmon.liq.store import LiqBusy, LiqStore, day_of
from flowmon.liq.types import (ABOVE, BELOW, DAY_MS, HOUR_MS, Candle, Heatmap, Liq, Point, SecBar, Zone,
                               bin_center, bin_edges, bin_index, covered, hour_floor, liq_side, merge_intervals)

T0 = 1_790_640_000_000  # 2026-09-29T00:00:00Z
BTC, ETH = "BTC-USDT-SWAP", "ETH-USDT-SWAP"


def raw_cfg() -> dict:
    return tomllib.loads((ROOT / "liq.example.toml").read_text(encoding="utf-8"))


def liq(ts, side="long", px=60000.0, sz=3.0, src="ws", inst=BTC):
    qty = sz * 0.01
    return Liq(ts=ts, inst=inst, side=side, bk_px=px, sz=sz, qty=qty, usd=qty * px, src=src, recv=ts + 5)


# ---------- 配置 ----------

def test_example_config_loads(tmp_path):
    c = lconf.load(ROOT / "liq.example.toml")
    assert c.insts == [BTC, ETH, "SOL-USDT-SWAP"]
    assert c.trade.risk_usdt == 3 and c.trade.stop_atr == 1.5 and c.trade.target_atr == 3 and c.trade.max_hours == 8
    assert c.heatmap.leverages == [25, 50, 100] and c.heatmap.bin_pct == 0.1 and c.heatmap.lookback_hours == 72
    assert c.signal.ratio_pct == 10 and c.signal.liq_mult == 2
    assert (c.filters.f2_from_hour, c.filters.f2_to_hour) == (16, 21)
    assert lconf.family(BTC) == "BTC-USDT" and lconf.ccy("SOL-USDT-SWAP") == "SOL"


def test_config_is_strict():
    raw = raw_cfg()
    del raw["trade"]["risk_usdt"]
    with pytest.raises(ConfigError, match="risk_usdt"):
        lconf.from_dict(raw, ROOT / "liq.toml")
    raw = raw_cfg()
    raw["trade"]["leverage"] = 3
    with pytest.raises(ConfigError, match="leverage"):
        lconf.from_dict(raw, ROOT / "liq.toml")
    raw = raw_cfg()
    del raw["filters"]
    with pytest.raises(ConfigError, match="filters"):
        lconf.from_dict(raw, ROOT / "liq.toml")
    raw = raw_cfg()
    raw["signal"]["ratio_pct"] = "10"
    with pytest.raises(ConfigError, match="数字"):
        lconf.from_dict(raw, ROOT / "liq.toml")


@pytest.mark.parametrize("section,key,value,msg", [
    ("collect", "insts", ["BTC-USDT"], "永续合约"),
    ("collect", "insts", [BTC, BTC], "重复"),
    ("alerts", "single_usd", [1.0, 2.0], "一一对应"),
    ("heatmap", "leverage_weights", [1.0, 1.0], "个数要相同"),
    ("heatmap", "mmr", 0.05, "mmr"),
    ("trade", "fade_frac", 1.0, "fade_frac"),
    ("filters", "f2_to_hour", 16, "f2"),
    ("baseline", "min_coverage", 0.0, "min_coverage"),
])
def test_config_validation(section, key, value, msg):
    raw = raw_cfg()
    raw[section][key] = value
    with pytest.raises(ConfigError, match=msg):
        lconf.from_dict(raw, ROOT / "liq.toml")


def test_find_next_to_config(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("", encoding="utf-8")
    assert lconf.find(cfg) is None                       # 没有 liq.toml：不采集
    (tmp_path / "liq.toml").write_text((ROOT / "liq.example.toml").read_text(encoding="utf-8"), encoding="utf-8")
    assert lconf.find(cfg).insts[0] == BTC
    with pytest.raises(ConfigError, match="找不到"):
        lconf.find(cfg, tmp_path / "nope.toml")          # 明确指定的必须存在


# ---------- 数据结构 ----------

def test_liq_side_mapping():
    assert liq_side("sell", "long") == "long"
    assert liq_side("buy", "short") == "short"
    assert liq_side("sell", "net") == "long"     # 单向持仓：系统卖出 = 平多
    assert liq_side("buy", "net") == "short"
    assert liq_side("buy", None) == "short"


def test_intervals():
    iv = merge_intervals([(5, 10), (1, 3), (3, 4), (9, 12), (20, 19)])
    assert iv == [(1, 4), (5, 12)]
    assert covered(iv, 5, 12) and covered(iv, 1, 2)
    assert not covered(iv, 3, 6)                  # 跨过 4–5 的缺口
    assert not covered([], 0, 1)


def test_bins_and_zones():
    k = bin_index(60000.0, 0.1)
    lo, hi = bin_edges(k, 0.1)
    assert lo <= 60000.0 < hi and hi / lo == pytest.approx(1.001)
    assert lo < bin_center(k, 0.1) < hi
    assert bin_index(hi * 1.0000001, 0.1) == k + 1
    up = Zone(ABOVE, 61000.0, 61200.0, 61100.0, 5e6)
    dn = Zone(BELOW, 58800.0, 59000.0, 58900.0, 9e6)
    assert up.near == 61000.0 and dn.near == 59000.0
    assert up.dist_pct(60000.0) == pytest.approx(1000 / 60000 * 100)
    assert dn.contains(58900.0) and not dn.contains(59000.1)
    hm = Heatmap(px=60000.0, above=[up, Zone(ABOVE, 62000.0, 62100.0, 62050.0, 8e6)], below=[dn])
    assert hm.nearest(ABOVE) is up and hm.nearest(BELOW) is dn
    assert [z.usd for z in hm.top(ABOVE, 2)] == [8e6, 5e6]
    assert Heatmap(px=1.0).nearest(ABOVE) is None
    assert hour_floor(T0 + HOUR_MS + 5) == T0 + HOUR_MS


# ---------- 落盘 ----------

def test_liq_dedupe_survives_restart(tmp_path):
    s = LiqStore(tmp_path)
    a, b = liq(T0 + 1000), liq(T0 + 1000, side="short")
    assert s.add("liq", [a, a, b]) == [a, b]
    s.close()
    s2 = LiqStore(tmp_path)
    # 同一笔从接口再来一次（来源不同、收到时间不同）不重复写；不同数量的算另一笔
    again = dataclasses.replace(a, src="rest", recv=a.recv + 60_000)
    other = liq(T0 + 1000, sz=4.0)
    assert s2.add("liq", [again, other]) == [other]
    s2.flush()
    got = s2.read("liq", BTC, T0, T0 + DAY_MS)
    assert [(r.side, r.sz, r.src) for r in got] == [("long", 3.0, "ws"), ("short", 3.0, "ws"), ("long", 4.0, "ws")]


def test_read_range_across_days_and_insts(tmp_path):
    s = LiqStore(tmp_path)
    rows = [Candle(BTC, T0 + k * HOUR_MS, 1, 2, 0.5, 1.5, 10, 0.1, 6000, None) for k in range(30)]
    rows += [Candle(ETH, T0 + 5 * HOUR_MS, 3, 4, 2, 3, 1, 0.1, 300, 123)]
    assert len(s.add("c1h", rows)) == 31
    assert s.add("c1h", rows[:3]) == []
    s.flush()
    assert {p.name for p in (tmp_path / "candles_1h").glob("*.csv")} == {day_of(T0) + ".csv",
                                                                         day_of(T0 + DAY_MS) + ".csv"}
    got = s.read("c1h", BTC, T0 + 20 * HOUR_MS, T0 + 26 * HOUR_MS)
    assert [r.ts for r in got] == [T0 + k * HOUR_MS for k in range(20, 26)]
    assert s.read("c1h", ETH, T0, T0 + DAY_MS)[0].seen == 123
    assert len(s.read("c1h", None, T0, T0 + 2 * DAY_MS)) == 31
    assert s.bounds("c1h", BTC) == (T0, T0 + 29 * HOUR_MS)
    assert s.bounds("c1h", "SOL-USDT-SWAP") is None


def test_torn_line_is_skipped(tmp_path):
    s = LiqStore(tmp_path)
    s.add("oi1h", [Point(BTC, T0, 100.0, None)])
    s.close()
    p = tmp_path / "oi_1h" / f"{day_of(T0)}.csv"
    with p.open("a", encoding="utf-8") as f:
        f.write(f"{T0 + HOUR_MS},{BTC},10")          # 断电时只写了半行，没有换行
    s2 = LiqStore(tmp_path)
    s2.add("oi1h", [Point(BTC, T0 + 2 * HOUR_MS, 102.0, None)])
    s2.flush()
    assert [(r.ts, r.value) for r in s2.read("oi1h", BTC, T0, T0 + DAY_MS)] == [(T0, 100.0), (T0 + 2 * HOUR_MS, 102.0)]


def test_live_kinds_only_move_forward(tmp_path):
    s = LiqStore(tmp_path)
    bars = [SecBar(BTC, T0 + k * 1000, 1, 1, 1, 1, 1) for k in (0, 1, 1, 0, 2)]
    assert [r.ts for r in s.add("sec", bars)] == [T0, T0 + 1000, T0 + 2000]
    assert len(s.add("sec", [SecBar(ETH, T0, 2, 2, 2, 2, 1)])) == 1     # 每个合约各算各的
    s.flush()
    assert [r.ts for r in s.sec_window(BTC, T0, T0 + 2000)] == [T0, T0 + 1000]
    assert s.sec_window(ETH, T0, T0 + 5000)[0].o == 2
    pts = [Point(BTC, T0 + k, float(k), T0 + k) for k in (10, 5, 20)]
    assert [p.ts for p in s.add("oilive", pts)] == [T0 + 10, T0 + 20]


def test_load_inst_and_state(tmp_path):
    s = LiqStore(tmp_path)
    s.add("liq", [liq(T0 + 10), liq(T0 + 20, inst=ETH)])
    s.add("ratio1h", [Point(BTC, T0, 1.7, None)])
    s.save_meta({BTC: {"ctVal": "0.01"}})
    s.save_state({"coverage": {BTC: [[T0 + 5, T0 + 100], [T0, T0 + 10]]}})
    s.flush()
    d = s.load_inst(BTC, T0, T0 + DAY_MS)
    assert [x.ts for x in d.liqs] == [T0 + 10] and d.ratio[0].value == 1.7
    assert d.meta == {"ctVal": "0.01"} and d.coverage == [(T0, T0 + 100)]
    assert d.c1m == [] and d.sec(T0, T0 + 1000) == []


def test_forget_before_keeps_dedupe_correct(tmp_path):
    s = LiqStore(tmp_path)
    old = liq(T0)
    s.add("liq", [old])
    s.forget_before(T0 + 2 * DAY_MS)
    # 忘掉内存里的键后，再写同一笔仍然能从硬盘查出重复
    assert s.add("liq", [old]) == []


def test_lock_blocks_second_writer(tmp_path):
    a, b = LiqStore(tmp_path), LiqStore(tmp_path)
    a.lock()
    with pytest.raises(LiqBusy):
        b.lock()
    a.unlock()
    b.lock()
    b.unlock()
