"""配置：一份 TOML，对应规格第 13 节。

代码里不放任何默认值 —— 少一项、多一项、类型不对都直接报错，
免得某个阈值悄悄用了写死的数。
"""
from __future__ import annotations

import dataclasses
import tomllib
import typing
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ExchangeCfg:
    ws_public_url: str
    rest_base_url: str
    inst_id: str
    inst_type: str
    rest_timeout_s: float


@dataclass(frozen=True)
class ConnectionCfg:
    ping_interval_s: float
    pong_timeout_s: float
    stale_reconnect_s: float
    reconnect_backoff_min_s: float
    reconnect_backoff_max_s: float
    reconnect_fail_notify: int
    subscribe_timeout_s: float


@dataclass(frozen=True)
class BucketCfg:
    width_s: int
    close_grace_ms: int
    fallback_close_lag_ms: int
    oi_stale_s: float
    near_depth_pct: float
    cancel_range_pct: float
    snapshot_levels: int


@dataclass(frozen=True)
class ScoreCfg:
    flow_window_buckets: int
    oi_window_buckets: int
    baseline_hours: float
    baseline_min_coverage: float
    volume_multiple_cap: float
    oi_z_divisor: float
    weight_flow: float
    weight_oi: float
    no_new_money_cap: float
    price_disagree_factor: float
    smooth_buckets: int


@dataclass(frozen=True)
class RulesCfg:
    entry_threshold: float
    add_thresholds: list[float]
    halve_threshold: float
    exit_threshold: float
    position_tiers: list[float]
    full_margin_usdt: float
    leverage: float
    max_loss_usdt_by_tier: list[float]
    time_stop_minutes: float
    time_stop_profit_pct: float
    cooldown_after_win_minutes: float
    cooldown_after_loss_minutes: float


@dataclass(frozen=True)
class ConditionsCfg:
    low_vol_window_minutes: float
    low_vol_lookback_days: float
    low_vol_percentile: float
    flip_window_minutes: float
    flip_count: int
    flip_threshold: float
    flip_record_thresholds: list[float]
    calendar_file: str
    calendar_before_minutes: float
    calendar_after_minutes: float
    loss_streak_count: int
    loss_streak_pause_minutes: float


@dataclass(frozen=True)
class RiskCfg:
    capital_usdt: float
    max_margin_usdt: float
    max_loss_half_usdt: float
    max_loss_full_usdt: float
    max_consecutive_losses: int
    consecutive_loss_pause_hours: float
    max_daily_loss_usdt: float
    max_weekly_loss_usdt: float
    min_equity_usdt: float


@dataclass(frozen=True)
class FeesCfg:
    maker_rate: float
    taker_rate: float


@dataclass(frozen=True)
class EventsCfg:
    tiers: list[float]
    followup_minutes: float
    price_horizons_s: list[int]
    limit_fill_windows_s: list[int]
    market_order_notional_usdt: float
    control_per_hour: int
    control_seed: str


@dataclass(frozen=True)
class EvaluationCfg:
    horizon_s: int
    dedupe_minutes: float
    bootstrap_reps: int
    bootstrap_seed: str
    confidence: float
    segments: int
    segments_positive_min: int
    min_signals: int
    min_weeks: float
    exclude_no_trade: bool


@dataclass(frozen=True)
class StorageCfg:
    data_dir: str
    log_dir: str
    restore_days: int
    raw_flush_s: float
    log_keep_days: int


@dataclass(frozen=True)
class HealthCfg:
    disk_check_interval_s: float
    disk_min_free_gb: float
    daily_report: bool


@dataclass(frozen=True)
class NotifyCfg:
    kind: str
    url: str
    on_start: bool
    daily_summary: bool
    min_interval_s: float


@dataclass(frozen=True)
class Config:
    exchange: ExchangeCfg
    connection: ConnectionCfg
    bucket: BucketCfg
    score: ScoreCfg
    rules: RulesCfg
    conditions: ConditionsCfg
    risk: RiskCfg
    fees: FeesCfg
    events: EventsCfg
    evaluation: EvaluationCfg
    storage: StorageCfg
    health: HealthCfg
    notify: NotifyCfg
    base_dir: Path  # 配置文件所在目录，相对路径都从这里算

    def path(self, p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else self.base_dir / q

    @property
    def data_dir(self) -> Path:
        return self.path(self.storage.data_dir)

    @property
    def log_dir(self) -> Path:
        return self.path(self.storage.log_dir)


def _coerce(name: str, tp, value):
    origin = typing.get_origin(tp)
    if origin is list:
        (item_tp,) = typing.get_args(tp)
        if not isinstance(value, list):
            raise ConfigError(f"{name} 应该是列表")
        return [_coerce(f"{name}[{i}]", item_tp, v) for i, v in enumerate(value)]
    if tp is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{name} 应该是 true/false")
        return value
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{name} 应该是整数")
        return value
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{name} 应该是数字")
        return float(value)
    if tp is str:
        if not isinstance(value, str):
            raise ConfigError(f"{name} 应该是字符串")
        return value
    raise ConfigError(f"{name}: 不支持的类型 {tp}")


def _build(section: str, cls, raw: dict):
    hints = typing.get_type_hints(cls)
    fields = [f.name for f in dataclasses.fields(cls)]
    missing = [f for f in fields if f not in raw]
    extra = [k for k in raw if k not in fields]
    if missing:
        raise ConfigError(f"[{section}] 缺少：{', '.join(missing)}")
    if extra:
        raise ConfigError(f"[{section}] 不认识：{', '.join(extra)}")
    return cls(**{f: _coerce(f"{section}.{f}", hints[f], raw[f]) for f in fields})


def from_dict(raw: dict, base_dir: Path) -> Config:
    hints = typing.get_type_hints(Config)
    sections = [f.name for f in dataclasses.fields(Config) if f.name != "base_dir"]
    missing = [s for s in sections if s not in raw]
    extra = [s for s in raw if s not in sections]
    if missing:
        raise ConfigError(f"缺少段落：{', '.join('[' + s + ']' for s in missing)}")
    if extra:
        raise ConfigError(f"不认识的段落：{', '.join('[' + s + ']' for s in extra)}")
    built = {s: _build(s, hints[s], raw[s]) for s in sections}
    cfg = Config(base_dir=base_dir, **built)
    _validate(cfg)
    return cfg


def load(path: str | Path) -> Config:
    p = Path(path).resolve()
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"找不到配置文件 {p}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"配置文件格式错误：{e}") from None
    return from_dict(raw, p.parent)


def _validate(c: Config) -> None:
    def need(ok: bool, msg: str):
        if not ok:
            raise ConfigError(msg)

    need(c.bucket.width_s > 0, "bucket.width_s 必须大于 0")
    need(c.score.flow_window_buckets >= 1, "score.flow_window_buckets 至少 1")
    need(c.score.oi_window_buckets >= 1, "score.oi_window_buckets 至少 1")
    need(c.score.smooth_buckets >= 1, "score.smooth_buckets 至少 1")
    need(0 < c.score.baseline_min_coverage <= 1, "score.baseline_min_coverage 应在 (0, 1]")
    need(c.score.oi_z_divisor > 0, "score.oi_z_divisor 必须大于 0")
    need(c.score.baseline_hours * 3600 >= c.bucket.width_s * (c.score.oi_window_buckets + 2),
         "score.baseline_hours 太短，装不下持仓量变化窗口")
    need(all(h > 0 and h % c.bucket.width_s == 0 for h in c.events.price_horizons_s),
         "events.price_horizons_s 必须是桶宽的整数倍")
    need(max(c.events.price_horizons_s) <= c.events.followup_minutes * 60,
         "events.price_horizons_s 不能超过 followup_minutes")
    need(all(w > 0 for w in c.events.limit_fill_windows_s), "events.limit_fill_windows_s 必须大于 0")
    need(c.events.control_per_hour >= 0, "events.control_per_hour 不能为负")
    need(c.notify.kind in ("none", "ntfy", "bark", "webhook"), "notify.kind 只能是 none/ntfy/bark/webhook")
    need(c.notify.kind == "none" or c.notify.url, "notify.url 不能为空")
    need(len(set(c.events.tiers)) == len(c.events.tiers) and all(t > 0 for t in c.events.tiers),
         "events.tiers 必须是不重复的正数")
    need(c.rules.entry_threshold in c.events.tiers, "events.tiers 必须包含进场门槛 rules.entry_threshold")
    ev = c.evaluation
    need(ev.horizon_s in c.events.price_horizons_s, "evaluation.horizon_s 必须在 events.price_horizons_s 里")
    need(0 < ev.confidence < 1, "evaluation.confidence 应在 (0, 1)")
    need(ev.bootstrap_reps >= 1, "evaluation.bootstrap_reps 至少 1")
    need(ev.segments >= 1, "evaluation.segments 至少 1")
    need(1 <= ev.segments_positive_min <= ev.segments, "evaluation.segments_positive_min 应在 [1, segments]")
    cd = c.conditions
    need(cd.flip_threshold >= 0 and all(t >= 0 for t in cd.flip_record_thresholds),
         "conditions.flip_threshold 和 flip_record_thresholds 不能为负")
    need(len(set(cd.flip_record_thresholds)) == len(cd.flip_record_thresholds),
         "conditions.flip_record_thresholds 不能重复")
