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


def setup_logging(cfg: config_mod.Config | None, name: str, console: bool = True) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fmt.converter = __import__("time").gmtime  # 日志时间统一 UTC
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    if console:
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
    from .monitor import AlreadyRunning, Monitor
    from .okx import load_instrument

    cfg = config_mod.load(a.config)
    # 后台服务（launchd）下不往标准输出写日志，免得 launchd 的输出文件和 logs/flowmon.log 重复、无限变大
    setup_logging(cfg, "flowmon", console=not a.no_console_log)
    log = logging.getLogger("flowmon")
    log.info("启动：%s，数据目录 %s", cfg.exchange.inst_id, cfg.data_dir)
    inst = load_instrument(cfg)
    m = Monitor(cfg, inst)
    try:
        m.lock()
    except AlreadyRunning as e:
        log.error("%s", e)
        print(f"没有启动：{e}", file=sys.stderr)
        return 3
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


def cmd_evaluate(a) -> int:
    from .evaluate import evaluate
    from .replay import days_between

    cfg = config_mod.load(a.config)
    src = Path(a.src).resolve() if a.src else cfg.data_dir
    days = days_between(a.frm, a.to or a.frm) if a.frm else None
    text, verdict = evaluate(cfg, src, days)
    if a.write:
        p = src / "reports" / "evaluation.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    print(text)
    return 0 if verdict else 1


def cmd_status(a) -> int:
    from .schema import bucket_columns
    from .storage import day_files, load_json, read_csv

    cfg = config_mod.load(a.config)
    # 同一天可能有多个文件（表头变了会另起 D.1.csv），按日期取最后一天，再读这一天的全部文件
    bdir, edir = cfg.data_dir / "buckets", cfg.data_dir / "events"
    days = sorted({p.name[:10] for p in bdir.glob("*.csv")})
    if not days:
        print("还没有任何桶数据")
        return 1
    rows = sorted(read_csv(day_files(bdir, days[-1:], ".csv"), dict(bucket_columns(cfg))),
                  key=lambda r: r["start_ms"])
    last = rows[-a.n:]
    print(f"{'时间(UTC)':24} {'完整':4} {'收盘':>10} {'买量':>8} {'卖量':>8} {'F':>6} {'M':>5} {'Z':>6} {'S':>7} 有效 不交易")
    for r in last:
        def f(x, p=2):
            return "-" if x is None else f"{x:.{p}f}"
        print(f"{r['time_utc']:24} {int(r['complete']):4} {f(r['close'], 1):>10} {f(r['buy_vol'], 3):>8} "
              f"{f(r['sell_vol'], 3):>8} {f(r['F']):>6} {f(r['M']):>5} {f(r['Z']):>6} {f(r['S'], 1):>7} "
              f"{int(r['score_valid']):4} {r['no_trade_reason'] or '-'}")
    n_inc = sum(1 for r in rows if not r["complete"])
    print(f"\n{days[-1]}：{len(rows)} 个桶，不完整 {n_inc} 个")
    edays = sorted({p.name[:10] for p in edir.glob("*.csv")})
    if edays:
        n = sum(1 for _ in read_csv(day_files(edir, edays[-1:], ".csv")))
        print(f"{edays[-1]}：{n} 条已完成的事件")
    st = load_json(cfg.data_dir / "state" / "events.json")
    if st:
        print(f"正在跟踪的事件：{len(st.get('pending', []))} 条")
    return 0


def cmd_check(a) -> int:
    from .selfcheck import run_check

    cfg = config_mod.load(a.config)
    text, ok = run_check(cfg, a.minutes)
    print(text)
    return 0 if ok else 1


def cmd_service(a) -> int:
    from . import macos, power

    cfg = config_mod.load(a.config)
    if a.action == "print":
        sys.stdout.write(macos.render_plist(macos.venv_python(), Path(a.config).resolve(), macos.package_dir(),
                                            cfg.log_dir).decode())
        return 0
    if not power.is_macos():
        print("后台服务只支持 macOS（launchd）。用 service print 可以看服务定义。", file=sys.stderr)
        return 2
    try:
        if a.action == "install":
            msgs = macos.install(Path(a.config), cfg.log_dir)
        elif a.action == "status":
            st = macos.status()
            msgs = ([f"{k}：{v}" for k, v in st.items() if k != "loaded"] if st["loaded"]
                    else ["服务没有加载（没安装，或者已经 stop）"])
            msgs.append(f"服务定义 {macos.plist_path()}（{'存在' if macos.plist_path().exists() else '不存在'}）")
        else:
            msgs = getattr(macos, a.action)()
    except RuntimeError as e:
        print(f"失败：{e}", file=sys.stderr)
        return 1
    print("\n".join(msgs))
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
    r.add_argument("--no-console-log", action="store_true", help="日志只写文件，不打印到屏幕（后台服务用）")
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

    r = sub.add_parser("evaluate", help="按 §11 判定阶段一是否通过")
    r.add_argument("--config", required=True)
    r.add_argument("--from", dest="frm", default=None, help="起始日期，默认全部事件")
    r.add_argument("--to", default=None)
    r.add_argument("--src", default=None, help="数据目录，默认配置里的 data_dir")
    r.add_argument("--write", action="store_true", help="同时写到 data/reports/evaluation.md")
    r.set_defaults(fn=cmd_evaluate)

    r = sub.add_parser("status", help="看最近的桶和事件")
    r.add_argument("--config", required=True)
    r.add_argument("-n", type=int, default=20)
    r.set_defaults(fn=cmd_status)

    r = sub.add_parser("check", help="自检：确认在正常录数据、分数在正常计算，结果同时存到 data/reports/")
    r.add_argument("--config", required=True)
    r.add_argument("--minutes", type=float, default=60, help="检查最近多少分钟，默认 60")
    r.set_defaults(fn=cmd_check)

    r = sub.add_parser("service", help="macOS 后台服务：登录后自动启动、崩溃后自动拉起")
    r.add_argument("action", choices=["install", "start", "stop", "restart", "status", "uninstall", "print"])
    r.add_argument("--config", required=True)
    r.set_defaults(fn=cmd_service)

    r = sub.add_parser("notify", help="发一条测试通知")
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
