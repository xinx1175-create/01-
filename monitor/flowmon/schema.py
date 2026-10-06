"""两张表的列定义：15 秒桶（§5、§6、§8）和信号事件（§10）。

列名里的 F、M、A、Z、B、R、S 与规格第 6 节的符号一一对应。
"""
from __future__ import annotations

from .config import Config

# (列名, 类型)。类型只用于读回时解析：int / float / bool / str
BUCKET_COLUMNS: list[tuple[str, type]] = [
    ("time_utc", str),           # 桶起始时间
    ("start_ms", int),
    ("width_s", int),
    ("open", float), ("high", float), ("low", float), ("close", float),
    ("high_ms", int), ("low_ms", int),  # 桶内最高、最低成交出现的交易所时间
    ("buy_vol", float),          # 主动买入量（基础币，BTC）
    ("sell_vol", float),         # 主动卖出量
    ("trade_count", int),        # 成交笔数（交易所撮合次数，聚合推送里的 count 之和）
    ("trade_msgs", int),         # 收到的成交推送条数
    ("oi", float),               # 桶结束时的持仓量（BTC）
    ("oi_ms", int),              # 该持仓量自身的时间戳
    ("funding_rate", float),
    ("bid1", float), ("ask1", float), ("spread", float),
    ("book_ms", int), ("book_seq", int),
    ("near_bid_vol", float), ("near_ask_vol", float),  # 中间价上下 near_depth_pct 内的挂单（BTC）
    ("near_truncated", bool),    # 1 = 盘口深度没铺满这个范围，数字偏小
    ("cancel_bid_vol", float), ("cancel_ask_vol", float),  # 估算撤单量（BTC）
    ("liq_long_vol", float), ("liq_short_vol", float),     # 多头、空头被强平量（BTC）
    ("latency_ms", float),       # 本地接收时间 − 交易所时间戳，桶内中位数
    ("late_trades", int),        # 封桶后才到的成交条数（未计入任何桶，原始数据里有）
    ("complete", bool),
    ("incomplete_reason", str),
    # §6 力量分数
    ("F", float), ("M", float), ("A", float),
    ("oi_chg", float),           # 持仓量变化窗口内的变化量
    ("Z", float), ("B", float),
    ("R_raw", float),            # 两条修正之前的原始分
    ("fix1", bool),              # 修正一（没有新资金）条件成立
    ("fix2", bool),              # 修正二（价格不配合）条件成立
    ("R", float),
    ("price_chg", float),        # 成交方向窗口内的价格变化
    ("S", float),                # 力量分数；预热期也会算，但 score_valid=0
    ("score_valid", bool),
    ("score_note", str),         # 分数无效的原因
    # §8 不交易条件（市场侧可在阶段一判断的几条）
    ("range_pct", float),        # 最近 low_vol_window_minutes 的 (最高−最低)/收盘，百分比
    ("range_rank", float),       # 它在过去 lookback 天同类数值里的分位（0–100）
    ("range_hist_h", float),     # 算分位用了多少小时的历史
    ("flips", int),              # 最近 flip_window_minutes 内的翻转次数（按 flip_threshold 判定，§8 用这个）
    ("nt_low_vol", bool), ("nt_flips", bool), ("nt_calendar", bool), ("nt_data", bool),
    ("no_trade", bool),
    ("no_trade_reason", str),
]


def bucket_columns(cfg: Config) -> list[tuple[str, type]]:
    """桶表的列：固定列 + 每个记录门槛一列翻转次数（flips_0、flips_10 …），紧跟在 flips 后面。"""
    cols = list(BUCKET_COLUMNS)
    i = [n for n, _ in cols].index("flips") + 1
    cols[i:i] = [(flip_col(t), int) for t in cfg.conditions.flip_record_thresholds]
    return cols


def flip_col(t: float) -> str:
    return f"flips_{_num(t)}"


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)


def event_columns(cfg: Config) -> list[tuple[str, type]]:
    ev, ru = cfg.events, cfg.rules
    cols: list[tuple[str, type]] = [
        ("event_id", str),
        ("kind", str),           # signal / control
        ("time_utc", str),
        ("ts_ms", int),          # 信号时刻 = 触发信号那个桶的结束时间
        ("direction", int),      # 1 多 / -1 空；对照组为随机方向
        ("tier", float),         # 穿越的档位；对照组为空
        ("S", float), ("F", float), ("M", float), ("A", float),
        ("Z", float), ("B", float), ("R", float),
        ("price", float), ("bid1", float), ("ask1", float),
        ("mkt_fill_px", float),  # 按当时前 N 档估算的市价单成交均价
        ("mkt_slip_bps", float),  # 相对中间价的不利滑点，基点
        ("limit_px", float),     # 挂在买一（多）或卖一（空）的价格
        ("limit_queue", float),  # 当时这一档前面排着的量（BTC）
    ]
    cols += [(f"limit_fill_{w}s", bool) for w in ev.limit_fill_windows_s]
    cols += [("no_trade", bool), ("no_trade_reason", str)]
    cols += [(f"px_{h}s", float) for h in ev.price_horizons_s]
    cols += [
        ("mfe_pct", float), ("mfe_after_s", float),  # 之后朝信号方向最多走了多少、何时
        ("mae_pct", float), ("mae_after_s", float),  # 朝反方向最多走了多少、何时
        ("max_score", float), ("max_score_after_s", float),  # 同向分数最高值
    ]
    for a in ru.add_thresholds:
        cols.append((f"t_ge_{_num(a)}_s", float))     # 同向分数首次 ≥ 加仓门槛
    cols.append((f"t_lt_{_num(ru.halve_threshold)}_s", float))  # 首次 < 减半门槛
    cols.append((f"t_lt_{_num(ru.exit_threshold)}_s", float))   # 首次跌破清仓门槛
    cols += [
        ("latency_ms", float),
        ("followup_complete", bool),  # 跟踪期间所有桶都完整
    ]
    return cols


def threshold_col(kind: str, v: float) -> str:
    return f"t_{kind}_{_num(v)}_s"
