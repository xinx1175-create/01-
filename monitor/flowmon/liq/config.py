"""爆仓模块的配置：liq.toml，和 config.toml 放在同一个目录。

没有这个文件就不采集爆仓数据，监控器照常运行。规则同 config.toml：
每一项都必须写，少写、多写、类型不对都直接报错，代码里不放默认值。
"""
from __future__ import annotations

import dataclasses
import re
import tomllib
import typing
from dataclasses import dataclass
from pathlib import Path

from ..config import ConfigError, _build

FILE_NAME = "liq.toml"
INST_RE = re.compile(r"^([A-Z0-9]+)-([A-Z0-9]+)-SWAP$")


@dataclass(frozen=True)
class CollectCfg:
    insts: list[str]               # 永续合约，如 BTC-USDT-SWAP
    backfill_days: float           # 启动时回补多少天（交易所给不了这么久就补到给的为止）
    rest_poll_s: float             # 爆仓单 REST 补全的间隔
    rest_overlap_s: float          # 每次往回多翻这么久，接住晚到的记录
    candle_poll_s: float           # 1 分钟 K 线的更新间隔
    rubik_poll_s: float            # 整点后持仓量、多空人数比的小时数据多久查一次
    rubik_wait_max_s: float        # 整点后最多等多久
    second_bars: bool              # 用逐笔成交合成 1 秒 K 线（算止损滑点用）
    oi_snapshot_s: float           # 实时持仓量最多多久记一条


@dataclass(frozen=True)
class RestCfg:
    timeout_s: float
    retries: int
    retry_wait_s: float
    rate_limit_wait_s: float       # 被限频（HTTP 429 或 code 50011）后等多久再试
    max_pages: int                 # 一次往回翻页的上限，防止死循环
    interval_liq_s: float          # 同一类接口两次请求的最小间隔（限频）
    interval_candles_s: float
    interval_history_candles_s: float
    interval_rubik_s: float
    interval_instruments_s: float


@dataclass(frozen=True)
class AlertsCfg:
    enabled: bool
    single_usd: list[float]        # 单笔爆仓 ≥ 这个金额就推送（顺序同 insts）
    window_minutes: float
    window_usd: list[float]        # window_minutes 分钟内同一方向累计 ≥ 这个金额就推送（顺序同 insts）
    hour_mult: float               # 某小时多头或空头爆仓量 ≥ 过去 7 天小时均值的这么多倍就推送
    signal: bool                   # 出信号时推送（只记录，不下单）


@dataclass(frozen=True)
class BaselineCfg:
    days: float                    # 「过去 7 天」：爆仓倍数、多空人数比分位、平均小时波动都用它
    min_coverage: float            # 窗口里至少这个比例的小时有数据才算
    rubik_avail_s: float           # 回补来的交易所小时统计（持仓量、多空人数比），按「整点后多少秒能拿到」处理
    ratio_max_age_s: float         # 多空人数比最多用多旧的
    oi_live_tol_s: float           # 自己录的持仓量离整点多近才用


@dataclass(frozen=True)
class HeatmapCfg:
    lookback_hours: int            # 过去 72 小时
    leverages: list[float]         # 25 / 50 / 100 倍
    leverage_weights: list[float]  # 新增持仓在各杠杆之间怎么分
    mmr: float                     # 维持保证金率
    bin_pct: float                 # 价格格子 0.1%
    zone_frac: float               # 一侧最大格子的这个比例以上算密集
    zone_merge_bins: int           # 密集格子之间隔着不超过这么多格就并成一个区
    top_n: int                     # 上方、下方各输出几个


@dataclass(frozen=True)
class SignalCfg:
    ratio_pct: float               # 多空人数比在过去 7 天最高（最低）的 10%
    liq_mult: float                # 加强版：被挤一方的爆仓量 > 过去 7 天小时均值的 2 倍
    decision_delay_s: float        # 整点收盘后多少秒做决定（等小时数据和 REST 补全）
    entry_max_wait_s: float        # 决策后这么久还没有价格就放弃这次信号


@dataclass(frozen=True)
class TradeCfg:
    risk_usdt: float               # 每笔固定亏损额：仓位 = risk_usdt ÷ 止损距离
    max_notional_usdt: float       # 仓位名义价值上限
    stop_atr: float                # 止损 = 1.5 倍平均小时波动
    target_atr: float              # 出场 a 的目标 = 3 倍
    max_hours: float               # 最长持有
    zone_gap_pct: float            # 出场 b：目标在前方最近的爆仓密集区之前 0.1%
    fade_frac: float               # 出场 b：被挤一方的小时爆仓量回落到本轮高峰的 30% 以下离场


@dataclass(frozen=True)
class FiltersCfg:
    """实时信号记录里打开哪几条（报告里每一条都给出开和关两种结果）。"""
    f1: bool                       # 上一小时波动 ≥ 2 倍均值且持仓下降 > 1% → 之后 2 小时不出信号
    f1_vol_mult: float
    f1_oi_drop_pct: float
    f1_block_hours: float
    f2: bool                       # 北京时间 16:00–21:00 不出信号
    f2_from_hour: int
    f2_to_hour: int
    f3: bool                       # 上下最近的爆仓密集区离现价都 < 1 倍平均小时波动 → 不出信号
    f3_atr_mult: float
    f4: bool                       # 止损落在密集区内 → 移到该区外侧 0.1%，按新距离重算仓位
    f4_gap_pct: float
    f5: bool                       # 同一币种被止损后 4 小时内不出反向信号
    f5_hours: float


@dataclass(frozen=True)
class ReportCfg:
    double_kill_hours: float       # 出场后多久内价格回到进场价算「被双杀」
    slippage_window_s: float       # 止损触发后多长时间内的最差成交价
    bootstrap_reps: int
    seed: str


@dataclass(frozen=True)
class LiqConfig:
    collect: CollectCfg
    rest: RestCfg
    alerts: AlertsCfg
    baseline: BaselineCfg
    heatmap: HeatmapCfg
    signal: SignalCfg
    trade: TradeCfg
    filters: FiltersCfg
    report: ReportCfg
    path: Path

    @property
    def insts(self) -> list[str]:
        return self.collect.insts


def family(inst: str) -> str:
    """BTC-USDT-SWAP → BTC-USDT（REST 的 instFamily）。"""
    return inst.rsplit("-", 1)[0]


def ccy(inst: str) -> str:
    """BTC-USDT-SWAP → BTC（多空人数比接口按币种查）。"""
    return inst.split("-", 1)[0]


def from_dict(raw: dict, path: Path) -> LiqConfig:
    hints = typing.get_type_hints(LiqConfig)
    sections = [f.name for f in dataclasses.fields(LiqConfig) if f.name != "path"]
    missing = [s for s in sections if s not in raw]
    extra = [s for s in raw if s not in sections]
    if missing:
        raise ConfigError(f"{path.name} 缺少段落：{', '.join('[' + s + ']' for s in missing)}")
    if extra:
        raise ConfigError(f"{path.name} 不认识的段落：{', '.join('[' + s + ']' for s in extra)}")
    try:
        built = {s: _build(s, hints[s], raw[s]) for s in sections}
    except ConfigError as e:
        raise ConfigError(f"{path.name}：{e}") from None
    c = LiqConfig(path=path, **built)
    _validate(c)
    return c


def load(path: str | Path) -> LiqConfig:
    p = Path(path).resolve()
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"找不到爆仓模块配置 {p}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{p.name} 格式错误：{e}") from None
    return from_dict(raw, p)


def find(config_path: str | Path, explicit: str | Path | None = None) -> LiqConfig | None:
    """explicit 给了就必须存在；没给就找 config.toml 旁边的 liq.toml，没有返回 None（不采集爆仓数据）。"""
    if explicit is not None:
        return load(explicit)
    p = Path(config_path).resolve().parent / FILE_NAME
    return load(p) if p.exists() else None


def _validate(c: LiqConfig) -> None:
    def need(ok: bool, msg: str):
        if not ok:
            raise ConfigError(f"{c.path.name}：{msg}")

    co = c.collect
    need(len(co.insts) >= 1, "collect.insts 至少一个合约")
    need(len(set(co.insts)) == len(co.insts), "collect.insts 不能重复")
    for i in co.insts:
        need(INST_RE.match(i) is not None, f"collect.insts 里的 {i} 不是永续合约名（形如 BTC-USDT-SWAP）")
    need(co.backfill_days > 0, "collect.backfill_days 必须大于 0")
    for k in ("rest_poll_s", "candle_poll_s", "rubik_poll_s", "oi_snapshot_s"):
        need(getattr(co, k) > 0, f"collect.{k} 必须大于 0")
    need(co.rest_overlap_s >= 0, "collect.rest_overlap_s 不能为负")
    need(co.rubik_wait_max_s >= co.rubik_poll_s, "collect.rubik_wait_max_s 不能小于 rubik_poll_s")

    r = c.rest
    need(r.timeout_s > 0 and r.retry_wait_s >= 0 and r.rate_limit_wait_s >= 0, "rest 的超时和等待时间不能为负")
    need(r.retries >= 1, "rest.retries 至少 1")
    need(r.max_pages >= 1, "rest.max_pages 至少 1")
    for k in ("interval_liq_s", "interval_candles_s", "interval_history_candles_s", "interval_rubik_s",
              "interval_instruments_s"):
        need(getattr(r, k) > 0, f"rest.{k} 必须大于 0")

    a = c.alerts
    n = len(co.insts)
    need(len(a.single_usd) == n and len(a.window_usd) == n,
         "alerts.single_usd、window_usd 要和 collect.insts 一一对应（个数相同）")
    need(all(x > 0 for x in a.single_usd + a.window_usd), "alerts 的金额必须大于 0")
    need(a.window_minutes > 0, "alerts.window_minutes 必须大于 0")
    need(a.hour_mult > 0, "alerts.hour_mult 必须大于 0")

    b = c.baseline
    need(b.days > 0, "baseline.days 必须大于 0")
    need(0 < b.min_coverage <= 1, "baseline.min_coverage 应在 (0, 1]")
    need(b.rubik_avail_s >= 0 and b.ratio_max_age_s > 0 and b.oi_live_tol_s > 0,
         "baseline 的时间参数不能为负")

    h = c.heatmap
    need(h.lookback_hours >= 1, "heatmap.lookback_hours 至少 1")
    need(len(h.leverages) >= 1 and len(h.leverages) == len(h.leverage_weights),
         "heatmap.leverages 和 leverage_weights 个数要相同")
    need(all(x > 1 for x in h.leverages), "heatmap.leverages 必须大于 1")
    need(all(w >= 0 for w in h.leverage_weights) and sum(h.leverage_weights) > 0,
         "heatmap.leverage_weights 不能为负，且不能全为 0")
    need(all(0 <= h.mmr < 1 / x for x in h.leverages), "heatmap.mmr 必须小于 1 / 杠杆")
    need(h.bin_pct > 0, "heatmap.bin_pct 必须大于 0")
    need(0 < h.zone_frac <= 1, "heatmap.zone_frac 应在 (0, 1]")
    need(h.zone_merge_bins >= 0, "heatmap.zone_merge_bins 不能为负")
    need(h.top_n >= 1, "heatmap.top_n 至少 1")

    s = c.signal
    need(0 < s.ratio_pct < 50, "signal.ratio_pct 应在 (0, 50)")
    need(s.liq_mult > 0, "signal.liq_mult 必须大于 0")
    need(s.decision_delay_s >= 0, "signal.decision_delay_s 不能为负")
    need(s.entry_max_wait_s > 0, "signal.entry_max_wait_s 必须大于 0")

    t = c.trade
    need(t.risk_usdt > 0 and t.max_notional_usdt > 0, "trade.risk_usdt、max_notional_usdt 必须大于 0")
    need(t.stop_atr > 0 and t.target_atr > 0, "trade.stop_atr、target_atr 必须大于 0")
    need(t.max_hours > 0, "trade.max_hours 必须大于 0")
    need(t.zone_gap_pct >= 0, "trade.zone_gap_pct 不能为负")
    need(0 < t.fade_frac < 1, "trade.fade_frac 应在 (0, 1)")

    f = c.filters
    need(f.f1_vol_mult > 0 and f.f1_oi_drop_pct >= 0 and f.f1_block_hours > 0, "filters.f1_* 取值不对")
    need(0 <= f.f2_from_hour < 24 and 0 < f.f2_to_hour <= 24 and f.f2_from_hour != f.f2_to_hour,
         "filters.f2_from_hour 应在 [0, 24)，f2_to_hour 应在 (0, 24]，且两者不同")
    need(f.f3_atr_mult > 0, "filters.f3_atr_mult 必须大于 0")
    need(f.f4_gap_pct >= 0, "filters.f4_gap_pct 不能为负")
    need(f.f5_hours > 0, "filters.f5_hours 必须大于 0")

    rp = c.report
    need(rp.double_kill_hours > 0 and rp.slippage_window_s > 0, "report 的时长必须大于 0")
    need(rp.bootstrap_reps >= 1, "report.bootstrap_reps 至少 1")
