"""阶段二：按规则模拟持仓、算盈亏。

两套仓位管理跑同一批进场信号，比较哪套更好：

  规格第 7 节  进场一档；同向 S 到 60、75 加到二档、三档；跌破 35 减半；跌破 15 清仓。
              止损按该档最大亏损定：整笔仓位亏到 3 / 6 / 10 USDT 的价位（按对 §7 的理解，见 README）。
  资金流带    进场同上。之后看资金流带的位置 z（顺着持仓方向）：
              到外带之外、而且有浮盈 → 加一档；连续几个桶回到内带以内 → 减一档，一档时再持续几个桶 → 平仓；
              到反向内带之外（资金掉头）→ 立刻平仓。
              止损距离 = 若干倍价格噪声（不低于下限）；仓位 = 这一档的最大亏损 ÷ 止损距离，不超过该档上限。
              加仓后止损只往有利方向挪。

两套共用：进场条件（S 穿过进场门槛、分数有效、不在 §8 不交易条件里）、时间止损、冷却、§8 风控表、§9 手续费。

成交假设（偏保守）：
- 决策都在桶结束时做，用桶结束时截下的买一卖一成交：买在卖一、卖在买一，按吃单收费。
- 进场挂单变体：挂在买一（多）/ 卖一（空），之后几个桶内价格「穿过」挂单价才算成交（刚好碰到不算），
  按挂单收费；没成交就放弃这次信号，不追。成交那个桶里如果又碰到止损，照样止损。
- 止损：桶内最低（多）/ 最高（空）碰到止损价就算触发，按止损价成交；桶开盘就已越过止损价时按开盘价；
  再让半个价差。
- 价格未知的桶（断线、睡眠、停机）里没法判断止损：这笔交易标记「经过数据缺口」，单独统计。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .band import BandCfg, FlowBand
from .config import Config

TAKER, MAKER = "taker", "maker"


@dataclass(slots=True)
class Bar:
    """一个 15 秒桶，所有字段都是桶结束时已知的。"""
    t: int                 # 桶起点（毫秒）
    end: int               # 桶结束
    complete: bool
    valid: bool            # 分数有效
    S: float | None
    no_trade: bool
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    bid: float | None
    ask: float | None
    z: float | None        # 资金流带位置
    noise: float | None    # 价格噪声（小数）


def bars_from_rows(rows: list[dict], cfg: Config, band_cfg: BandCfg) -> list[Bar]:
    """桶表的行（按时间排好）→ 回测用的 Bar；顺便算资金流带。"""
    fb = FlowBand(cfg.score, band_cfg, cfg.bucket.width_s)
    w = cfg.bucket.width_s * 1000
    out = []
    for r in rows:
        b = fb.update(r)
        out.append(Bar(
            t=r["start_ms"], end=r["start_ms"] + w, complete=bool(r["complete"]), valid=bool(r.get("score_valid")),
            S=r.get("S"), no_trade=bool(r.get("no_trade")),
            open=r.get("open"), high=r.get("high"), low=r.get("low"), close=r.get("close"),
            bid=r.get("bid1"), ask=r.get("ask1"), z=b.z, noise=b.noise))
    return out


# ---------- 仓位和交易记录 ----------

@dataclass
class Position:
    d: int                 # 1 多 / -1 空
    qty: float = 0.0       # BTC
    avg: float = 0.0       # 持仓均价
    level: int = 1
    stop: float = 0.0
    opened: int = 0        # 开仓那个桶的结束时间
    halved: bool = False
    fade: int = 0          # 资金流带：连续回到内带以内的桶数
    max_level: int = 1
    realized: float = 0.0  # 已实现盈亏（不含手续费）
    fees: float = 0.0
    gap: bool = False
    entry_px: float = 0.0
    log: list[str] = field(default_factory=list)

    def ret(self, px: float) -> float:
        """浮动收益率（小数）。"""
        return self.d * (px - self.avg) / self.avg


@dataclass
class Trade:
    policy: str
    d: int
    entry_t: int
    exit_t: int
    entry_px: float
    exit_px: float
    max_level: int
    pnl: float             # 净盈亏（扣手续费，USDT）
    fees: float
    reason: str
    gap: bool
    actions: str

    @property
    def hold_s(self) -> float:
        return (self.exit_t - self.entry_t) / 1000


@dataclass
class Resize:
    qty: float             # 目标数量（BTC）
    level: int
    reason: str
    budget: float | None = None      # 整笔仓位止损时最多亏多少（USDT）
    dist_frac: float | None = None   # 止损距离（相对均价）
    ratchet: bool = False            # 止损只往有利方向挪
    halved: bool = False


@dataclass
class Exit:
    reason: str


# ---------- 两套仓位管理 ----------

class Spec7Policy:
    """规格第 7 节。"""
    name = "规格第 7 节"

    def __init__(self, cfg: Config):
        ru = cfg.rules
        self.tiers = [t * ru.full_margin_usdt * ru.leverage for t in ru.position_tiers]
        self.budget = list(ru.max_loss_usdt_by_tier)
        self.add = list(ru.add_thresholds)
        self.halve, self.exit = ru.halve_threshold, ru.exit_threshold

    def entry(self, bar: Bar, px: float) -> Resize:
        return Resize(qty=self.tiers[0] / px, level=1, reason="进场", budget=self.budget[0])

    def manage(self, pos: Position, bar: Bar, px: float) -> Resize | Exit | None:
        if bar.S is None:
            return None
        ds = pos.d * bar.S
        if ds < self.exit:
            return Exit("S 跌破清仓线")
        if ds < self.halve:
            if not pos.halved:
                return Resize(qty=pos.qty / 2, level=pos.level, reason="S 跌破减半线", halved=True)
            return None
        if pos.halved:
            return None
        target = pos.level
        for k, th in enumerate(self.add, start=2):
            if ds >= th:
                target = max(target, k)
        if target > pos.level:
            return Resize(qty=self.tiers[target - 1] / px, level=target, reason=f"S 到加仓线（{target} 档）",
                          budget=self.budget[target - 1])
        return None


@dataclass(frozen=True)
class BandPositionCfg:
    stop_noise_mult: float       # 止损距离 = 这个倍数 × 价格噪声
    min_stop_pct: float          # 止损距离下限（%）
    add_needs_profit: bool       # 只在有浮盈时加仓
    fade_buckets: int            # 读数连续这么多个桶回到内带以内 → 减一档
    exit_fade_buckets: int       # 一档时，读数连续这么多个桶在内带以内 → 平仓
    reverse_exit: bool           # 读数到反向内带之外 → 立刻平仓


class BandPolicy:
    """资金流带仓位法。"""
    name = "资金流带"

    def __init__(self, cfg: Config, band: BandCfg, pc: BandPositionCfg):
        ru = cfg.rules
        self.tiers = [t * ru.full_margin_usdt * ru.leverage for t in ru.position_tiers]
        self.budget = list(ru.max_loss_usdt_by_tier)
        self.inner, self.outer = band.inner, band.outer
        self.pc = pc

    def stop_frac(self, bar: Bar, level: int) -> float:
        floor = self.pc.min_stop_pct / 100
        if bar.noise is None:
            # 还没有噪声数据：退回按该档最大亏损定的止损距离
            return max(floor, self.budget[level - 1] / self.tiers[level - 1])
        return max(floor, self.pc.stop_noise_mult * bar.noise)

    def _size(self, bar: Bar, level: int, px: float) -> tuple[float, float]:
        d = self.stop_frac(bar, level)
        notional = min(self.tiers[level - 1], self.budget[level - 1] / d)
        return notional / px, d

    def entry(self, bar: Bar, px: float) -> Resize:
        qty, d = self._size(bar, 1, px)
        return Resize(qty=qty, level=1, reason="进场", budget=self.budget[0], dist_frac=d)

    def manage(self, pos: Position, bar: Bar, px: float) -> Resize | Exit | None:
        if bar.z is None:
            return None
        zd = pos.d * bar.z
        if self.pc.reverse_exit and zd <= -self.inner:
            return Exit("资金掉头")
        pos.fade = pos.fade + 1 if zd < self.inner else 0
        if zd >= self.outer and pos.level < len(self.tiers):
            if not self.pc.add_needs_profit or pos.ret(px) > 0:
                lv = pos.level + 1
                qty, d = self._size(bar, lv, px)
                if qty > pos.qty:
                    return Resize(qty=qty, level=lv, reason=f"资金到外带（{lv} 档）", budget=self.budget[lv - 1],
                                  dist_frac=d, ratchet=True)
        if pos.level >= 2 and pos.fade >= self.pc.fade_buckets:
            pos.fade = 0
            lv = pos.level - 1
            return Resize(qty=pos.qty * self.tiers[lv - 1] / self.tiers[lv], level=lv,
                          reason=f"资金回到内带（{lv} 档）")
        if pos.level == 1 and pos.fade >= self.pc.exit_fade_buckets:
            return Exit("资金停了")
        return None


# ---------- 账户和风控 ----------

def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


def _week(ms: int) -> str:
    y, wk, _ = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isocalendar()
    return f"{y}-W{wk:02d}"


@dataclass
class SimResult:
    policy: str
    entry_order: str
    trades: list[Trade]
    equity_start: float
    equity_end: float
    max_drawdown: float
    halted: str | None            # 触发了彻底停止（权益过低）
    missed_maker: int             # 挂单没成交、放弃的信号数
    signals: int                  # 满足进场条件的信号数（含挂单没成交的）
    blocked: int                  # 信号来时被冷却、暂停、日/周亏损上限挡住的次数


class Simulator:
    def __init__(self, cfg: Config, policy, entry_order: str = TAKER, maker_wait_buckets: int = 1):
        self.cfg = cfg
        self.policy = policy
        self.entry_order = entry_order
        self.maker_wait = maker_wait_buckets
        ru, rk, fe = cfg.rules, cfg.risk, cfg.fees
        self.w = cfg.bucket.width_s * 1000
        self.entry_th = ru.entry_threshold
        self.time_stop_ms = ru.time_stop_minutes * 60_000
        self.time_stop_ret = ru.time_stop_profit_pct / 100
        self.cool_win = ru.cooldown_after_win_minutes * 60_000
        self.cool_loss = ru.cooldown_after_loss_minutes * 60_000
        c = cfg.conditions
        self.streak_n, self.streak_pause = c.loss_streak_count, c.loss_streak_pause_minutes * 60_000
        self.max_losses, self.max_losses_pause = rk.max_consecutive_losses, rk.consecutive_loss_pause_hours * 3_600_000
        self.day_limit, self.week_limit = rk.max_daily_loss_usdt, rk.max_weekly_loss_usdt
        self.min_equity = rk.min_equity_usdt
        self.capital = rk.capital_usdt
        self.taker, self.maker = fe.taker_rate, fe.maker_rate

    # ---- 成交 ----

    def _fill(self, pos: Position, qty_delta: float, px: float, fee_rate: float) -> None:
        """qty_delta > 0 加仓、< 0 减仓（都是持仓方向上的数量）。"""
        fee = abs(qty_delta) * px * fee_rate
        pos.fees += fee
        if qty_delta > 0:
            pos.avg = (pos.avg * pos.qty + px * qty_delta) / (pos.qty + qty_delta)
            pos.qty += qty_delta
        else:
            q = -qty_delta
            pos.realized += pos.d * (px - pos.avg) * q
            pos.qty -= q

    def _set_stop(self, pos: Position, r: Resize) -> None:
        dist = math.inf
        if r.budget is not None and pos.qty > 0:
            dist = min(dist, r.budget / pos.qty)
        if r.dist_frac is not None:
            dist = min(dist, r.dist_frac * pos.avg)
        if math.isinf(dist):
            return
        stop = pos.avg - pos.d * dist
        if r.ratchet and pos.stop:
            stop = max(stop, pos.stop) if pos.d > 0 else min(stop, pos.stop)
        pos.stop = stop

    def _market_px(self, bar: Bar, buy: bool) -> float | None:
        if bar.bid is not None and bar.ask is not None:
            return bar.ask if buy else bar.bid
        return bar.close

    # ---- 主循环 ----

    def run(self, bars: list[Bar]) -> SimResult:
        name = self.policy.name
        trades: list[Trade] = []
        equity = peak = self.capital
        max_dd = 0.0
        pos: Position | None = None
        pending: tuple | None = None   # 挂单：(方向, 挂单价, 到期桶序号, 信号桶)
        pause_until = 0
        losses = 0
        day_pnl: dict[str, float] = {}
        week_pnl: dict[str, float] = {}
        halted = None
        missed = signals = blocked = 0
        prev: Bar | None = None

        def close(bar: Bar, px: float, reason: str, end: int) -> None:
            nonlocal pos, equity, peak, max_dd, pause_until, losses, halted
            self._fill(pos, -pos.qty, px, self.taker)
            pnl = pos.realized - pos.fees
            trades.append(Trade(name, pos.d, pos.opened, end, pos.entry_px, px, pos.max_level, pnl, pos.fees,
                                reason, pos.gap, "；".join(pos.log)))
            equity += pnl
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
            day_pnl[_day(end)] = day_pnl.get(_day(end), 0.0) + pnl
            week_pnl[_week(end)] = week_pnl.get(_week(end), 0.0) + pnl
            if pnl > 0:
                losses = 0
                pause_until = max(pause_until, end + self.cool_win)
            else:
                losses += 1
                pause_until = max(pause_until, end + self.cool_loss)
                if losses >= self.max_losses:
                    pause_until = max(pause_until, end + self.max_losses_pause)
                elif losses >= self.streak_n:
                    pause_until = max(pause_until, end + self.streak_pause)
            if equity < self.min_equity:
                halted = f"{_day(end)} 权益 {equity:.2f} 低于 {self.min_equity:g}"
            pos = None

        def open_pos(bar: Bar, d: int, px: float, fee_rate: float, end: int, how: str) -> None:
            nonlocal pos
            r = self.policy.entry(bar, px)
            pos = Position(d=d, opened=end, entry_px=px)
            self._fill(pos, r.qty, px, fee_rate)
            self._set_stop(pos, r)
            pos.log.append(f"{how}进场 {px:g}")

        def allowed(end: int) -> bool:
            if halted or end < pause_until:
                return False
            if day_pnl.get(_day(end), 0.0) <= -self.day_limit:
                return False
            if week_pnl.get(_week(end), 0.0) <= -self.week_limit:
                return False
            return True

        for i, bar in enumerate(bars):
            # 1. 挂单是否成交
            if pending is not None and pos is None:
                d, limit, expire, _ = pending
                if bar.low is not None and bar.high is not None and \
                        ((d > 0 and bar.low < limit) or (d < 0 and bar.high > limit)):
                    open_pos(bar, d, limit, self.maker, bar.t, "挂单")
                    pending = None
                elif i >= expire:
                    pending = None
                    missed += 1

            # 2. 止损（桶内）
            if pos is not None:
                if bar.low is None or bar.high is None:
                    pos.gap = True
                else:
                    hit = bar.low <= pos.stop if pos.d > 0 else bar.high >= pos.stop
                    if hit:
                        half = (bar.ask - bar.bid) / 2 if bar.bid is not None and bar.ask is not None else 0.0
                        o = bar.open if bar.open is not None else pos.stop
                        px = (min(pos.stop, o) - half) if pos.d > 0 else (max(pos.stop, o) + half)
                        pos.log.append(f"止损 {px:g}")
                        close(bar, px, "止损", bar.end)

            # 3. 时间止损
            if pos is not None and bar.close is not None and bar.end - pos.opened >= self.time_stop_ms \
                    and pos.ret(bar.close) < self.time_stop_ret:
                px = self._market_px(bar, buy=pos.d < 0)
                close(bar, px, "时间止损", bar.end)

            # 4. 持仓管理
            if pos is not None and bar.valid and bar.complete and bar.bid is not None and bar.ask is not None:
                act = self.policy.manage(pos, bar, bar.close)
                if isinstance(act, Exit):
                    px = self._market_px(bar, buy=pos.d < 0)
                    pos.log.append(f"{act.reason} {px:g}")
                    close(bar, px, act.reason, bar.end)
                elif isinstance(act, Resize):
                    delta = act.qty - pos.qty
                    if abs(delta) > 1e-12:
                        px = self._market_px(bar, buy=(delta > 0) == (pos.d > 0))
                        self._fill(pos, delta, px, self.taker)
                        pos.level = act.level
                        pos.max_level = max(pos.max_level, act.level)
                        pos.halved = pos.halved or act.halved
                        if delta > 0:
                            self._set_stop(pos, act)
                        pos.log.append(f"{act.reason} {px:g}")

            # 5. 进场
            if pos is None and pending is None and bar.valid and bar.complete and bar.S is not None \
                    and prev is not None and prev.t == bar.t - self.w and prev.valid and prev.S is not None:
                d = 1 if prev.S < self.entry_th <= bar.S else (-1 if prev.S > -self.entry_th >= bar.S else 0)
                if d and not bar.no_trade and bar.bid is not None and bar.ask is not None:
                    signals += 1
                    if not allowed(bar.end):
                        blocked += 1
                    elif self.entry_order == MAKER:
                        pending = (d, bar.bid if d > 0 else bar.ask, i + self.maker_wait, bar)
                    else:
                        open_pos(bar, d, bar.ask if d > 0 else bar.bid, self.taker, bar.end, "吃单")
            prev = bar

        # 数据结束时还没平的仓：按最后一个价格平掉，单独标记
        if pos is not None:
            last = next((b for b in reversed(bars) if b.close is not None), None)
            if last is not None:
                close(last, last.close, "数据结束", last.end)
        if pending is not None:
            missed += 1
        return SimResult(name, self.entry_order, trades, self.capital, equity, max_dd, halted, missed,
                         signals, blocked)


def summarize(r: SimResult) -> dict:
    t = r.trades
    wins = [x.pnl for x in t if x.pnl > 0]
    loss = [x.pnl for x in t if x.pnl <= 0]
    gross_w, gross_l = sum(wins), -sum(loss)
    reasons: dict[str, int] = {}
    for x in t:
        reasons[x.reason] = reasons.get(x.reason, 0) + 1
    return {
        "trades": len(t),
        "win_rate": len(wins) / len(t) if t else None,
        "avg_win": gross_w / len(wins) if wins else None,
        "avg_loss": -gross_l / len(loss) if loss else None,
        "profit_factor": gross_w / gross_l if gross_l > 0 else None,
        "pnl": sum(x.pnl for x in t),
        "fees": sum(x.fees for x in t),
        "pnl_per_trade": sum(x.pnl for x in t) / len(t) if t else None,
        "max_drawdown": r.max_drawdown,
        "avg_hold_s": sum(x.hold_s for x in t) / len(t) if t else None,
        "gap_trades": sum(1 for x in t if x.gap),
        "reasons": reasons,
        "missed_maker": r.missed_maker,
        "signals": r.signals,
        "blocked": r.blocked,
        "halted": r.halted,
    }
