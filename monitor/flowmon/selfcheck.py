"""自检：确认监控器在正常录数据、分数在正常计算。启动一小时后运行，把输出文件发回来核对。

只读数据，不影响正在运行的监控器。结果打印出来，同时写到 data/reports/check-<时间>.txt。
"""
from __future__ import annotations

import bisect
import fcntl
import platform
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import macos, power
from .bucket import iso_utc
from .config import Config
from .heartbeat import redact
from .report import day_disk_usage, dir_size, fmt_bytes
from .schema import bucket_columns
from .storage import day_files, load_json, read_csv, read_jsonl

LOG_NAME = "flowmon.log"


@dataclass
class Item:
    status: str   # 通过 / 不通过 / 等待 / 提示 / 跳过
    text: str


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


def _days_back(now_ms: int, n: int) -> list[str]:
    d = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).date()
    return [(d - timedelta(days=k)).isoformat() for k in range(n, -1, -1)]


def _git(*args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(macos.package_dir()), *args], capture_output=True, text=True,
                           timeout=5, check=False)
        return (r.stdout.strip() or None) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def monitor_running(cfg: Config) -> bool:
    """看进程锁有没有被占着：被占着说明有监控器在写这个数据目录。"""
    p = cfg.data_dir / "state" / "run.lock"
    if not p.exists():
        return False
    with p.open("a+", encoding="utf-8") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


def baseline_eta(rows: list[dict], cfg: Config) -> tuple[float, int | None]:
    """返回 (最后一个桶时，基准用的完整桶够了几成, 假设之后数据都完整时分数最早有效的时刻)。

    规则同 score.ScoreEngine.baseline_ready：从第一个桶算起已经记录满 baseline_hours，且回看
    baseline_lookback_hours 内的完整桶至少有 baseline_hours 的 baseline_min_coverage。
    之后持仓量变化窗口和平滑窗口还要填满，所以再加这两段。rows 要按时间排好、覆盖回看范围。
    """
    sc = cfg.score
    w = cfg.bucket.width_s * 1000
    n = max(1, round(sc.baseline_hours * 3600 / cfg.bucket.width_s))
    look = max(n, round(sc.baseline_lookback_hours * 3600 / cfg.bucket.width_s))
    if not rows:
        return 0.0, None
    first = rows[0]["start_ms"]
    last = rows[-1]["start_ms"]
    done = sorted(r["start_ms"] for r in rows if r["complete"])

    def count(t: int) -> int:
        # 回看范围 (t − look·w, t] 里的完整桶：已有的 + 假设 last 之后的桶都完整；最多用 n 个
        lo = t - look * w
        have = bisect.bisect_right(done, t) - bisect.bisect_right(done, lo)
        future = max(0, (t - max(last, lo)) // w)
        return min(n, have + future)

    def ready(t: int) -> bool:
        return t - (n - 1) * w >= first and count(t) >= sc.baseline_min_coverage * n

    cov_now = count(last) / n
    if ready(last):
        # 基准已经够了，分数还没有效的话是在等持仓量变化窗口和平滑窗口重新填满（重启后的头几分钟），按上限估
        return cov_now, last + (sc.oi_window_buckets + sc.smooth_buckets) * w
    t = last
    for _ in range(look + n + 2):
        t += w
        if ready(t):
            # 平滑窗口里的 R 都要在基准有效之后；桶结束时才算出分数
            return cov_now, t + sc.smooth_buckets * w
    return cov_now, None


def _log_lines(cfg: Config, since_ms: int, now_ms: int) -> list[str]:
    paths = [cfg.log_dir / f"{LOG_NAME}.{d}" for d in _days_back(now_ms, 1)] + [cfg.log_dir / LOG_NAME]
    since = iso_utc(since_ms)[:19].replace("T", " ")
    out = []
    for p in paths:
        if not p.exists():
            continue
        with p.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if line[:19] >= since:
                    out.append(line.rstrip("\n"))
    return out


def _f(x, p=2) -> str:
    return "-" if x is None else f"{x:.{p}f}"


def run_check(cfg: Config, minutes: float = 60, now_ms: int | None = None) -> tuple[str, bool]:
    now_ms = now_ms or int(time.time() * 1000)
    w = cfg.bucket.width_s * 1000
    items: list[Item] = []
    detail: list[str] = []

    # ---------- 读数据 ----------
    n_days = int(max(cfg.score.baseline_hours, cfg.score.baseline_lookback_hours) // 24) + 2
    btypes = dict(bucket_columns(cfg))
    bdir = cfg.data_dir / "buckets"
    rows = sorted(read_csv(day_files(bdir, _days_back(now_ms, n_days), ".csv"), btypes),
                  key=lambda r: r["start_ms"])
    win_lo = now_ms - int(minutes * 60_000)
    win = [r for r in rows if r["start_ms"] + w > win_lo]   # 最近 N 分钟内结束的桶
    lo = win[0]["start_ms"] if win else win_lo
    health = load_json(cfg.data_dir / "state" / "health.json") or {}

    # ---------- 版本和环境 ----------
    osname = f"macOS {platform.mac_ver()[0]}" if power.is_macos() else platform.platform()
    head = [
        f"flowmon 自检 {iso_utc(now_ms)[:19]}Z（UTC），检查最近 {minutes:g} 分钟",
        f"代码 {_git('rev-parse', '--abbrev-ref', 'HEAD') or '?'} @ {_git('rev-parse', '--short', 'HEAD') or '?'}"
        f" · Python {platform.python_version()} · {osname}",
        f"配置 {cfg.exchange.inst_id}，桶 {cfg.bucket.width_s} 秒，基准 {cfg.score.baseline_hours:g} 小时"
        f"（完整桶 ≥ {cfg.score.baseline_min_coverage:.0%}），翻转门槛 {cfg.conditions.flip_threshold:g}，"
        f"心跳 {redact(cfg.heartbeat.url) if cfg.heartbeat.url else '未配置'}，通知 {cfg.notify.kind}",
        f"数据目录 {cfg.data_dir}",
    ]

    # ---------- 1. 进程 ----------
    running = monitor_running(cfg)
    svc = macos.status() if power.is_macos() else None
    svc_txt = ""
    if svc is not None:
        svc_txt = ("；后台服务：" + (f"{svc.get('state', '?')}，启动过 {svc.get('runs', '?')} 次，"
                                   f"上次退出码 {svc.get('last exit code', '-')}" if svc["loaded"] else "没有安装或没有加载"))
    items.append(Item("通过" if running else "不通过",
                      (f"监控器在运行（pid {health.get('pid', '?')}，{health.get('started_utc', '?')} 启动）"
                       if running else "监控器没在运行") + svc_txt))

    # ---------- 2. 最新的桶 ----------
    if not rows:
        items.append(Item("不通过", "还没有任何桶数据"))
        return _render(cfg, head, items, detail, now_ms)
    last = rows[-1]
    age = (now_ms - (last["start_ms"] + w)) / 1000
    items.append(Item("通过" if age < 60 else "不通过", f"最新的桶 {last['time_utc'][:19]}，{age:.0f} 秒前结束"))

    # ---------- 3. 桶的数量和完整性 ----------
    if win:
        # 应有的桶数从窗口里第一个桶算起：刚启动不到 N 分钟时不要求更早的桶
        expected = max(1, (last["start_ms"] + w - lo) // w)
        n_ok = sum(1 for r in win if r["complete"])
        pct = n_ok / len(win) * 100
        items.append(Item("通过" if len(win) >= expected * 0.98 and pct >= 95 else "不通过",
                          f"最近 {minutes:g} 分钟：{len(win)} / {expected} 个桶，完整 {n_ok} 个（{pct:.1f}%）"))
        reasons = Counter(x for r in win if not r["complete"] for x in (r["incomplete_reason"] or "").split("|") if x)
        detail.append("不完整原因（一个桶可能有多个）：" + ("，".join(f"{k} {v}" for k, v in reasons.most_common()) or "无"))
    comp = [r for r in win if r["complete"]]

    # ---------- 4. 成交、持仓量、盘口 ----------
    if comp:
        with_tr = sum(1 for r in comp if (r["trade_msgs"] or 0) > 0)
        msgs = statistics.fmean(r["trade_msgs"] or 0 for r in comp)
        vol = sum((r["buy_vol"] or 0) + (r["sell_vol"] or 0) for r in comp)
        items.append(Item("通过" if with_tr >= len(comp) * 0.95 else "不通过",
                          f"成交：{with_tr} / {len(comp)} 个完整桶有成交，平均每桶 {msgs:.1f} 条推送，合计 {vol:.2f} BTC"))
        with_book = sum(1 for r in comp if r["bid1"] is not None and r["ask1"] is not None)
        spreads = [r["spread"] for r in comp if r["spread"] is not None]
        trunc = sum(1 for r in comp if r["near_truncated"])
        items.append(Item("通过" if with_book == len(comp) and not health.get("parse_errors") else "不通过",
                          f"盘口：{with_book} / {len(comp)} 个完整桶有买一卖一，价差中位 "
                          f"{_f(statistics.median(spreads) if spreads else None)}，盘口校验失败 "
                          f"{health.get('book_errors', '?')} 次，解析失败 {health.get('parse_errors') or 0}"))
        detail.append(f"近处挂单没铺满（near_truncated=1）：{trunc} / {len(comp)} 个桶（README「还没定的」第 1 条要看这个）")
    misc_days = sorted({_day(lo), _day(now_ms)})
    misc = [m for m in read_jsonl(day_files(cfg.data_dir / "raw" / "misc", misc_days, ".jsonl"))
            if int(m.get("ts") or m.get("s") or 0) >= lo]
    kinds = Counter(m.get("type") for m in misc)
    span_min = max(1e-9, (now_ms - lo) / 60_000)
    n_oi_bad = sum(1 for r in win if any(x in (r["incomplete_reason"] or "") for x in ("oi_stale", "oi_missing")))
    oi_txt = (f"持仓量：{kinds['oi']} 条推送（约每 {span_min * 60 / kinds['oi']:.1f} 秒一条），"
              f"持仓量过期或缺失的桶 {n_oi_bad} 个" if kinds["oi"] else "持仓量：没有收到推送")
    items.append(Item("通过" if kinds["oi"] > 0 and n_oi_bad <= len(win) * 0.05 else "不通过", oi_txt))
    detail.append(f"原始推送（最近 {minutes:g} 分钟）：持仓量 {kinds['oi']}，资金费率 {kinds['funding']}，"
                  f"强平 {kinds['liq']}，连接事件 {kinds['conn']}，睡眠 {kinds['sleep']}")

    # ---------- 5. 分数 ----------
    started = _parse_iso(health["started_utc"]) if health.get("started_utc") else rows[0]["start_ms"]
    run_min = (now_ms - started) / 60_000
    has = {k: sum(1 for r in comp if r[k] is not None) for k in ("F", "M", "A", "Z", "R", "S")}
    tail = [r for r in comp if r["start_ms"] >= last["start_ms"] - 10 * 60_000]
    s_tail = sum(1 for r in tail if r["S"] is not None)
    valid = sum(1 for r in win if r["score_valid"])
    cov, eta = baseline_eta(rows, cfg)
    if last["score_valid"]:
        valid_txt = f"分数有效 {valid} 个桶"
    elif eta is not None:
        valid_txt = f"有效分数 {valid} 个（预热中，假设之后数据都完整，最早 {iso_utc(eta)[:16]}Z 有效）"
    else:
        valid_txt = f"有效分数 {valid} 个"
    seen = "，".join(f"{k} {v}" for k, v in has.items())
    if run_min < 10:
        items.append(Item("等待", f"分数：刚启动 {run_min:.0f} 分钟，S 要 7 分钟左右才开始有数（各项已算出：{seen}）"))
    else:
        items.append(Item("通过" if tail and s_tail >= len(tail) * 0.9 else "不通过",
                          f"分数在计算：最近 10 分钟 {s_tail} / {len(tail)} 个完整桶算出了 S；{valid_txt}"))
    detail.append(f"各项算出的桶数（最近 {minutes:g} 分钟的完整桶 {len(comp)} 个）：{seen}")
    s_vals = [r["S"] for r in comp if r["S"] is not None]
    if s_vals:
        detail.append(f"S 范围 {min(s_vals):.1f} ~ {max(s_vals):.1f}，|S| 中位 {statistics.median(abs(x) for x in s_vals):.1f}")
    notes = Counter(x for r in win for x in (r["score_note"] or "").split("|") if x)
    detail.append("分数无效原因：" + ("，".join(f"{k} {v}" for k, v in notes.most_common()) or "无"))
    detail.append(f"基准值：最后一个桶时，过去 {cfg.score.baseline_hours:g} 小时完整桶占 {cov:.1%}"
                  f"（要 ≥ {cfg.score.baseline_min_coverage:.0%}，并且历史铺满 {cfg.score.baseline_hours:g} 小时）")

    # ---------- 6. 延迟 ----------
    lats = sorted(r["latency_ms"] for r in win if r["latency_ms"] is not None)
    if lats:
        med = statistics.median(lats)
        p95 = lats[int(0.95 * (len(lats) - 1))]
        ok = 0 <= med <= 1000
        items.append(Item("通过" if ok else "不通过",
                          f"数据延迟：中位 {med:.0f} ms，95% {p95:.0f} ms，最大 {lats[-1]:.0f} ms"
                          + ("" if med >= 0 else "（为负：本机时钟比交易所快，打开「自动设置日期与时间」）")))

    # ---------- 7. 日志 ----------
    logs = _log_lines(cfg, win_lo, now_ms)
    errs = [ln for ln in logs if " ERROR " in ln]
    warns = [ln for ln in logs if " WARNING " in ln]
    items.append(Item("通过" if not errs else "不通过", f"日志：错误 {len(errs)} 条，警告 {len(warns)} 条"))

    # ---------- 8. 睡眠 ----------
    pw = health.get("power") or {}
    sleeps = health.get("sleeps") or []
    if power.is_macos():
        asserts = power.sleep_assertions()
        ok = bool(asserts) and pw.get("caffeinate_running") and not sleeps
        items.append(Item("通过" if ok else "不通过",
                          f"阻止睡眠：caffeinate {'在运行' if pw.get('caffeinate_running') else '没在运行'}，"
                          f"系统里 {len(asserts)} 条相关断言，供电 {power.power_source() or '?'}，"
                          f"睡眠过 {len(sleeps)} 次"))
        detail += [f"  {a}" for a in asserts]
    else:
        items.append(Item("跳过", f"阻止睡眠：不是 macOS（{osname}）；睡眠过 {len(sleeps)} 次"))
    for s in sleeps[-5:]:
        detail.append(f"睡眠：{s['from_utc']} → {s['to_utc']}，{s['seconds']} 秒")

    # ---------- 9. 心跳 ----------
    hb = health.get("heartbeat") or {}
    if not cfg.heartbeat.url:
        items.append(Item("提示", "心跳：没有配置 heartbeat.url，断电、断网时不会有人通知你"))
    elif hb.get("last_ok_utc"):
        ok_age = (now_ms - _parse_iso(hb["last_ok_utc"])) / 1000
        items.append(Item("通过" if ok_age <= 3 * cfg.heartbeat.interval_s else "不通过",
                          f"心跳：最近一次报到成功在 {ok_age:.0f} 秒前"
                          + (f"；最近一次失败 {hb['last_err_utc']}：{hb['last_err']}" if hb.get("last_err") else "")))
    else:
        items.append(Item("不通过", f"心跳：还没有报到成功过；最近一次失败：{hb.get('last_err') or '无记录'}"))

    # ---------- 磁盘、最近的桶、日志摘录 ----------
    today = _day(now_ms)
    usage = day_disk_usage(cfg, today)
    try:
        free = fmt_bytes(shutil.disk_usage(cfg.data_dir).free)
    except OSError:
        free = "?"
    detail.append(f"磁盘：今天（UTC {today}）数据 {fmt_bytes(sum(b for _, b in usage))}（"
                  + "，".join(f"{n} {fmt_bytes(b)}" for n, b in usage)
                  + f"）；数据目录合计 {fmt_bytes(dir_size(cfg.data_dir))}；磁盘剩余 {free}")
    detail += ["", "最近 20 个桶：",
               f"{'时间(UTC)':20} 完整 {'收盘':>9} {'买量':>7} {'卖量':>7} {'OI':>10} {'F':>6} {'M':>5} {'Z':>6} "
               f"{'S':>6} 有效 {'延迟':>5} 原因"]
    for r in rows[-20:]:
        detail.append(f"{r['time_utc'][:19]:20} {int(bool(r['complete'])):4} {_f(r['close'], 1):>9} "
                      f"{_f(r['buy_vol'], 2):>7} {_f(r['sell_vol'], 2):>7} {_f(r['oi'], 1):>10} {_f(r['F']):>6} "
                      f"{_f(r['M']):>5} {_f(r['Z']):>6} {_f(r['S'], 1):>6} {int(bool(r['score_valid'])):4} "
                      f"{_f(r['latency_ms'], 0):>5} {r['incomplete_reason'] or r['score_note'] or ''}")
    if errs or warns:
        detail += ["", "日志里最近的警告和错误（最多 20 条）："] + [ln[:300] for ln in (errs + warns)[-20:]]
    return _render(cfg, head, items, detail, now_ms)


def _parse_iso(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


def _render(cfg: Config, head: list[str], items: list[Item], detail: list[str], now_ms: int) -> tuple[str, bool]:
    ok = all(i.status in ("通过", "提示", "跳过", "等待") for i in items)
    n_bad = sum(1 for i in items if i.status == "不通过")
    lines = head + ["", "== 结论 ==", "全部通过" if ok else f"有 {n_bad} 项不通过", ""]
    lines += [f"[{i.status}] {i.text}" for i in items]
    lines += ["", "== 详细 =="] + detail
    text = "\n".join(lines) + "\n"
    out = cfg.data_dir / "reports" / f"check-{iso_utc(now_ms)[:19].replace('-', '').replace(':', '')}Z.txt"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        text += f"\n已保存到 {out}\n"
    except OSError as e:
        print(f"保存自检结果失败：{e}", file=sys.stderr)
    return text, ok

