"""命令行：status 在同一天有多个桶文件（表头变了另起 D.1.csv）时要显示真正最新的桶。"""
from conftest import ROOT
from flowmon import __main__ as cli
from flowmon import config as config_mod
from flowmon.schema import bucket_columns
from flowmon.storage import DailyCsv

T0 = 1_790_640_000_000  # 2026-09-29T00:00:00Z


def _cfg_file(tmp_path, thresholds):
    raw = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    raw = raw.replace("flip_record_thresholds = [0, 10, 20, 30]", f"flip_record_thresholds = {thresholds}")
    p = tmp_path / f"c{len(thresholds)}.toml"
    p.write_text(raw, encoding="utf-8")
    return p


def _write(cfgp, start_i, n):
    cfg = config_mod.load(cfgp)
    w = DailyCsv(cfg.data_dir / "buckets", [c for c, _ in bucket_columns(cfg)])
    for i in range(start_i, start_i + n):
        t = T0 + i * 15_000
        w.write("2026-09-29", {"time_utc": f"t{i:04d}", "start_ms": t, "width_s": 15, "complete": True,
                               "close": float(i), "score_valid": False})
    w.close()


def test_status_reads_all_files_of_last_day(tmp_path, capsys):
    a = _cfg_file(tmp_path, [0, 10, 20, 30])
    b = _cfg_file(tmp_path, [0, 10, 15, 20, 30])  # 改了记录门槛，表头不同
    _write(a, 0, 100)
    _write(b, 100, 40)
    names = sorted(p.name for p in (tmp_path / "data" / "buckets").glob("*.csv"))
    assert names == ["2026-09-29.1.csv", "2026-09-29.csv"]
    assert cli.main(["status", "--config", str(b), "-n", "2"]) == 0
    out = capsys.readouterr().out
    assert "t0138" in out and "t0139" in out and "t0099" not in out
    assert "2026-09-29：140 个桶" in out
