"""命令行入口：python -m flowmon <命令> --config config.toml"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as config_mod


def setup_logging(cfg: config_mod.Config | None, name: str) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fmt.converter = __import__("time").gmtime  # 日志时间统一 UTC
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if cfg is not None:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.TimedRotatingFileHandler(cfg.log_dir / f"{name}.log", when="midnight",
                                                       backupCount=cfg.storage.log_keep_days,
                                                       utc=True, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def cmd_run(a) -> int:
    from .monitor import Monitor
    from .okx import load_instrument

    cfg = config_mod.load(a.config)
    setup_logging(cfg, "flowmon")
    log = logging.getLogger("flowmon")
    log.info("启动：%s，数据目录 %s", cfg.exchange.inst_id, cfg.data_dir)
    inst = load_instrument(cfg)
    m = Monitor(cfg, inst)
    m.restore()
    asyncio.run(m.run(a.duration))
    return 0


def cmd_replay(a) -> int:
    from .replay import Replayer, compare, days_between

    cfg = config_mod.load(a.config)
    setup_logging(None, "replay")
    src = Path(a.src).resolve() if a.src else cfg.data_dir
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(a.out).resolve() if a.out else src / "replay" / stamp
    to = a.to or a.frm
    stats = Replayer(cfg, src, out).run(a.frm, to)
    print(f"回放完成，输出 {out}")
    print(json.dumps(stats, ensure_ascii=False))
    if a.compare:
        print(json.dumps(compare(cfg, src, out, days_between(a.frm, to)), ensure_ascii=False, indent=1))
    return 0


def cmd_report(a) -> int:
    from .report import build_daily

    cfg = config_mod.load(a.config)
    day = a.date or (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    out = build_daily(cfg, day)
    if out is None:
        print(f"{day} 没有数据")
        return 1
    text, _ = out
    if a.write:
        p = cfg.data_dir / "reports" / f"{day}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    print(text)
    return 0


def cmd_status(a) -> int:
    from .schema import BUCKET_COLUMNS
    from .storage import load_json, read_csv

    cfg = config_mod.load(a.config)
    files = sorted((cfg.data_dir / "buckets").glob("*.csv"))
    if not files:
        print("还没有任何桶数据")
        return 1
    rows = list(read_csv([files[-1]], dict(BUCKET_COLUMNS)))
    last = rows[-a.n:]
    print(f"{'时间(UTC)':24} {'完整':4} {'收盘':>10} {'买量':>8} {'卖量':>8} {'F':>6} {'M':>5} {'Z':>6} {'S':>7} 有效 不交易")
    for r in last:
        def f(x, p=2):
            return "-" if x is None else f"{x:.{p}f}"
        print(f"{r['time_utc']:24} {int(r['complete']):4} {f(r['close'], 1):>10} {f(r['buy_vol'], 3):>8} "
              f"{f(r['sell_vol'], 3):>8} {f(r['F']):>6} {f(r['M']):>5} {f(r['Z']):>6} {f(r['S'], 1):>7} "
              f"{int(r['score_valid']):4} {r['no_trade_reason'] or '-'}")
    n_inc = sum(1 for r in rows if not r["complete"])
    print(f"\n{files[-1].name}：{len(rows)} 个桶，不完整 {n_inc} 个")
    ev = sorted((cfg.data_dir / "events").glob("*.csv"))
    if ev:
        n = sum(1 for _ in read_csv([ev[-1]]))
        print(f"{ev[-1].name}：{n} 条已完成的事件")
    st = load_json(cfg.data_dir / "state" / "events.json")
    if st:
        print(f"正在跟踪的事件：{len(st.get('pending', []))} 条")
    return 0


def cmd_notify(a) -> int:
    from .notify import Notifier

    cfg = config_mod.load(a.config)
    setup_logging(None, "notify")
    ok = Notifier(cfg.notify, f"[flowmon {cfg.exchange.inst_id}]").send(a.title, a.body or a.title)
    return 0 if ok or cfg.notify.kind == "none" else 1


def cmd_sim(a) -> int:
    from .sim import FakeOkx, serve_rest

    setup_logging(None, "sim")
    faults = []
    for f in a.fault or []:
        t, kind = f.split(":")
        faults.append((float(t), kind))
    start = None
    if a.start:
        start = int(datetime.fromisoformat(a.start.replace("Z", "+00:00")).timestamp() * 1000)
    serve_rest(a.host, a.rest_port)
    fake = FakeOkx(speed=a.speed, seed=a.seed, start_ms=start, faults=faults)

    async def main():
        stop = asyncio.Event()
        await fake.serve(a.host, a.port, stop)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="flowmon", description="资金流跟随策略 · 阶段一监控器")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="启动监控器")
    r.add_argument("--config", required=True)
    r.add_argument("--duration", type=float, default=None, help="运行多少秒后自动停（默认一直运行）")
    r.set_defaults(fn=cmd_run)

    r = sub.add_parser("replay", help="从原始数据重新生成桶和信号事件")
    r.add_argument("--config", required=True)
    r.add_argument("--from", dest="frm", required=True, help="起始日期 YYYY-MM-DD（UTC）")
    r.add_argument("--to", default=None, help="结束日期，默认同起始日期")
    r.add_argument("--src", default=None, help="原始数据目录，默认配置里的 data_dir")
    r.add_argument("--out", default=None, help="输出目录，默认 <data_dir>/replay/<时间>")
    r.add_argument("--compare", action="store_true", help="和实时结果逐桶比对")
    r.set_defaults(fn=cmd_replay)

    r = sub.add_parser("report", help="生成某天的日报")
    r.add_argument("--config", required=True)
    r.add_argument("--date", default=None, help="YYYY-MM-DD（UTC），默认昨天")
    r.add_argument("--write", action="store_true", help="同时写到 data/reports/")
    r.set_defaults(fn=cmd_report)

    r = sub.add_parser("status", help="看最近的桶和事件")
    r.add_argument("--config", required=True)
    r.add_argument("-n", type=int, default=20)
    r.set_defaults(fn=cmd_status)

    r = sub.add_parser("notify", help="发一条测试通知（也给 systemd 的失败钩子用）")
    r.add_argument("--config", required=True)
    r.add_argument("title")
    r.add_argument("body", nargs="?", default="")
    r.set_defaults(fn=cmd_notify)

    r = sub.add_parser("sim", help="本地假交易所，用来离线试跑")
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=18765)
    r.add_argument("--rest-port", type=int, default=18766)
    r.add_argument("--speed", type=float, default=1.0, help="时间加速倍数")
    r.add_argument("--seed", type=int, default=7)
    r.add_argument("--start", default=None, help="模拟起始时间（UTC ISO），默认现在")
    r.add_argument("--fault", action="append", help="注入故障，格式 模拟秒数:类型，类型为 seq_gap/disconnect/silence")
    r.set_defaults(fn=cmd_sim)

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except config_mod.ConfigError as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
