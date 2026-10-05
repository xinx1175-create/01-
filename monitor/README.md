# 资金流跟随策略 · 阶段一监控器

接 OKX 公开行情，每 15 秒算一次力量分数，把数据和信号事件记下来。**不下单，不需要账户，也不读取任何接口密钥。**
依据的是《资金流跟随策略 · 开发规格 v0.1》，下文「§n」指规格第 n 节。

## 交付状态

| 交付物（§14） | 状态 |
| --- | --- |
| 源代码 | `flowmon/`，按 数据接入 / 数据桶 / 分数 / 不交易条件 / 事件 / 存储 分模块 |
| 配置文件样例 | `config.example.toml`，对应 §13，代码里没有任何默认值 |
| 单元测试 | `tests/`，47 个单元测试 + 1 个端到端测试，全部通过 |
| 阶段一判定工具 | `python -m flowmon evaluate`，按已定口径判定 §11 阶段一是否通过，见「阶段一判定」 |
| 说明文件 | 本文件 |
| 至少 1 小时的实际运行记录 | **未完成**。开发环境的网络策略拦截了 `www.okx.com` 和 `ws.okx.com`，连不上 OKX。改用本地假交易所做了 39.6 小时模拟时长的连续运行，见 [docs/sim-run.md](docs/sim-run.md)。真实运行记录要在能连上 OKX 的机器上跑，步骤见下面「拿到 1 小时实际运行记录」 |
| 对照官方文档核对 §4 | 官方文档站同样被拦截，只能通过搜索结果和二手资料核对。已核对和待核对的项见「与 OKX 官方文档的核对」 |

## 安装

需要 Python 3.11 或更高版本（配置用标准库 `tomllib` 读取）。运行时只依赖 `websockets`。

```bash
cd monitor
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt          # 跑测试再加：pip install -r requirements-dev.txt
cp config.example.toml config.toml
cp calendar.example.csv calendar.csv     # 经济数据日程，手工维护
```

服务器必须开时间同步（例如 `chrony`）。数据延迟 = 本地接收时间 − 交易所时间戳，本机时钟偏了，延迟就不准。

## 配置

`config.toml` 里每一项都必须写，少写、多写、类型不对都会在启动时报错。注释里标「实现」的是规格没给、实现时必须定的值，见「实现时补充的约定」。

§13 参数和配置项的对应：

| §13 参数 | 起步值 | 配置项 |
| --- | --- | --- |
| 数据桶宽度 | 15 秒 | `bucket.width_s` |
| 成交方向窗口 | 4 个桶 | `score.flow_window_buckets` |
| 持仓量变化窗口 | 20 个桶 | `score.oi_window_buckets` |
| 基准值回看时长 | 24 小时 | `score.baseline_hours` |
| 成交量倍数上限 | 3 | `score.volume_multiple_cap` |
| 持仓量 Z 值除数 | 3 | `score.oi_z_divisor` |
| 两个分项的权重 | 各 0.5 | `score.weight_flow`、`score.weight_oi` |
| 无新资金时的分数上限 | 30 | `score.no_new_money_cap` |
| 价格不配合时的系数 | 0.5 | `score.price_disagree_factor` |
| 平滑窗口 | 4 个桶 | `score.smooth_buckets` |
| 进场门槛 | 40 | `rules.entry_threshold`（信号档位 `events.tiers` 必须包含它） |
| 加仓门槛 | 60、75 | `rules.add_thresholds` |
| 减半门槛 | 35 | `rules.halve_threshold` |
| 清仓门槛 | 15 | `rules.exit_threshold` |
| 三档仓位 | 50%、80%、100% | `rules.position_tiers` |
| 时间止损 | 10 分钟、浮盈 0.1% | `rules.time_stop_minutes`、`rules.time_stop_profit_pct` |
| 冷却时间 | 盈利后 2 分钟、亏损后 5 分钟 | `rules.cooldown_after_win_minutes`、`rules.cooldown_after_loss_minutes` |
| 低波动判定 | 30 分钟波动处于 7 天最低 30% | `conditions.low_vol_window_minutes`、`low_vol_lookback_days`、`low_vol_percentile` |
| 分数翻转判定 | 30 分钟内 3 次 | `conditions.flip_window_minutes`、`conditions.flip_count`（另有 `flip_min_abs`，见「需要你确认的问题」第 4 条） |
| 近处挂单范围 | 现价上下 0.2% | `bucket.near_depth_pct` |
| 持仓量过期判定 | 30 秒未更新 | `bucket.oi_stale_s` |

§7 的仓位、时间止损、冷却，§8 的风控表和 §9 的手续费，阶段一不用，但已经放进配置（`[rules]` `[risk]` `[fees]`），阶段二直接读同一份文件。

通知（§12）在 `[notify]` 里选渠道：`ntfy`（`url` 填 `https://ntfy.sh/你的主题`，手机装 ntfy 订阅同一主题）、`bark`（iOS，`url` 填 `https://api.day.app/你的key`）、`webhook`（POST JSON `{"title","body"}`）。用 `python -m flowmon notify --config config.toml 测试` 试发一条。会推送的情况：启动、停止、异常退出（systemd 钩子）、连续重连失败 5 次、磁盘剩余不足 2 GB、每日简报。

经济数据日程 `calendar.csv` 两列：`time_utc,name`，时间写 UTC，例如 `2026-10-15T12:30:00Z,美国 CPI`。运行中改了文件会自动重读。

## 启动

```bash
python -m flowmon run --config config.toml                  # 一直运行，Ctrl-C 或 SIGTERM 正常停止
python -m flowmon run --config config.toml --duration 3600  # 跑 1 小时自动停
```

长期运行用 systemd：把 `deploy/flowmon.service` 里的路径改成实际位置，放到 `/etc/systemd/system/`，然后
`sudo systemctl enable --now flowmon`。崩溃后 5 秒自动拉起，并推送一条「异常退出」。

服务器地区：§12 要求选到 OKX 延迟最低的地区，实测后决定。可以在候选地区各跑 10 分钟，比较 `latency_ms` 列。

**启动后要先积累 24 小时数据，分数才有效**（§6）。这期间 `S` 照算，但 `score_valid=0`，不产生信号事件。
重启时会读回最近 7 天的桶数据，恢复基准值和 7 天分位，不用重新等 24 小时（停机超过 24 小时除外）。

### 拿到 1 小时实际运行记录

在能连上 OKX 的机器上：

```bash
python -m flowmon run --config config.toml --duration 3600
python -m flowmon status --config config.toml -n 30
```

把 `logs/flowmon.log` 和 `data/` 一起留存，就是 §14 要的运行记录。1 小时内基准值还在预热，`S` 有数但 `score_valid=0`，能证明数据在写、分数在算。

## 查看数据

```bash
python -m flowmon status --config config.toml          # 最近 20 个桶、当天事件数、正在跟踪的事件
python -m flowmon report --config config.toml --date 2026-10-05   # 某天日报
```

文件都按交易所时间（UTC）分天：

| 路径 | 内容 |
| --- | --- |
| `data/buckets/YYYY-MM-DD.csv` | 15 秒桶，一行一个桶，含分数和不交易条件 |
| `data/events/YYYY-MM-DD.csv` | 信号事件和对照组；跟踪满 30 分钟才写入 |
| `data/raw/trades/YYYY-MM-DD.csv` | 逐笔成交原文（数量单位：张） |
| `data/raw/books/YYYY-MM-DD.jsonl` | 每个桶结束时的前 50 档盘口 |
| `data/raw/misc/YYYY-MM-DD.jsonl` | 持仓量、资金费率、强平原文，连接事件，每个桶的完整性记录 |
| `data/meta/instrument.json` | 合约面值、精度（从公开接口读取，接口失败时用它兜底） |
| `data/state/events.json` | 还没跟踪完的事件，重启后接着跟 |
| `data/reports/YYYY-MM-DD.md` | 每日简报，次日 00:30 UTC 后生成 |
| `logs/flowmon.log` | 连接、重连、校验失败、异常；每个桶一行摘要。按天切分 |

估算磁盘占用：逐笔成交约 30–80 MB/天，盘口快照约 15–20 MB/天，桶表约 3 MB/天。

用 pandas 读：

```python
import pandas as pd
b = pd.read_csv("data/buckets/2026-10-05.csv")
b = b[b.complete == 1]                         # §5：不完整的桶不参与统计
e = pd.read_csv("data/events/2026-10-05.csv")
sig, ctrl = e[e.kind == "signal"], e[e.kind == "control"]
ret5 = (e.px_300s / e.price - 1) * e.direction * 100   # 信号后 5 分钟同向涨跌幅（%）
```

### 桶表的列（`buckets`）

| 列 | 含义 |
| --- | --- |
| `time_utc`、`start_ms`、`width_s` | 桶起始时间 |
| `open` `high` `low` `close`、`high_ms` `low_ms` | 价格；没成交的桶沿用上一个收盘 |
| `buy_vol`、`sell_vol` | 主动买入量、主动卖出量（BTC） |
| `trade_count`、`trade_msgs` | 撮合笔数（推送里 `count` 之和）、成交推送条数 |
| `oi`、`oi_ms` | 桶结束时的持仓量（BTC）及其自身时间戳 |
| `funding_rate` | 最新资金费率 |
| `bid1` `ask1` `spread`、`book_ms` `book_seq` | 桶结束时的买一、卖一、价差 |
| `near_bid_vol`、`near_ask_vol`、`near_truncated` | 中间价上下 0.2% 内的买单、卖单（BTC）；`near_truncated=1` 表示 400 档没铺满这个范围，数字偏小 |
| `cancel_bid_vol`、`cancel_ask_vol` | 估算撤单量（BTC）。盘口重建过的桶为空 |
| `liq_long_vol`、`liq_short_vol` | 多头、空头被强平量（BTC），见下文强平频道的限制 |
| `latency_ms` | 本地接收 − 交易所时间戳，桶内中位数 |
| `late_trades` | 封桶后才到的成交条数，没计入任何桶（原始数据里有） |
| `complete`、`incomplete_reason` | 是否完整；原因可多个，见「实现时补充的约定」第 4 条 |
| `F` `M` `A` `oi_chg` `Z` `B` `R_raw` `fix1` `fix2` `R` `price_chg` `S` | §6 各项。`R_raw` 是两条修正之前的原始分，`fix1`/`fix2` 为修正条件是否成立 |
| `score_valid`、`score_note` | 分数是否有效；无效时写原因（`warmup`、`incomplete`、`flow_window_gap` 等） |
| `range_pct`、`range_rank`、`range_hist_h` | 最近 30 分钟 (最高−最低)/收盘（%）、它在过去 7 天里的分位（0–100）、算分位用了多少小时的历史 |
| `flips` | 最近 30 分钟分数正负翻转次数 |
| `nt_low_vol` `nt_flips` `nt_calendar` `nt_data`、`no_trade` `no_trade_reason` | §8 不交易条件 |

### 事件表的列（`events`）

| 列 | 含义 |
| --- | --- |
| `event_id`、`kind` | `signal` 为信号，`control` 为对照组 |
| `time_utc`、`ts_ms` | 信号时刻 = 触发信号那个桶的结束时间 |
| `direction`、`tier` | 1 多 / −1 空；穿越的档位 30/40/50。对照组方向随机，档位为空 |
| `S` `F` `M` `A` `Z` `B` `R` | 信号时各项 |
| `price` `bid1` `ask1` | 信号时价格（该桶最后一笔成交）、买一、卖一 |
| `mkt_fill_px`、`mkt_slip_bps` | 600 USDT 市价单按当时前 50 档估算的成交均价；相对中间价的不利滑点（基点） |
| `limit_px`、`limit_queue`、`limit_fill_5s`、`limit_fill_15s` | 挂在买一（多）或卖一（空）的价格、当时这一档排在前面的量（BTC）、5 秒 / 15 秒内是否会成交 |
| `no_trade`、`no_trade_reason` | 当时是否处于 §8 不交易条件、是哪几条 |
| `px_15s` … `px_1800s` | 信号后 15 秒、1、2、5、15、30 分钟的价格 |
| `mfe_pct`、`mfe_after_s`、`mae_pct`、`mae_after_s` | 之后 30 分钟内朝信号方向最多走了多少（%）、在第几秒；朝反方向最多走了多少、在第几秒 |
| `max_score`、`max_score_after_s` | 之后 30 分钟内同向分数最高值及时间 |
| `t_lt_15_s` | 同向分数首次跌破 15（清仓门槛）的时间 |
| `t_ge_60_s`、`t_ge_75_s`、`t_lt_35_s` | 额外记的：首次到达加仓门槛、首次跌破减半门槛的时间，供阶段二校准 §7 |
| `latency_ms` | 信号那个桶的数据延迟 |
| `followup_complete` | 跟踪的 30 分钟里所有桶都完整（期间断线或停机为 0） |

## 阶段一判定（§11）

§11 要求标准在看到结果之前定好，下面这套口径已经定死，写在配置 `[evaluation]` 里，事后不改。

```bash
python -m flowmon evaluate --config config.toml --write     # 结果写到 data/reports/evaluation.md
```

**三条同时满足才算通过：**

1. **统计上有差别**：信号组 5 分钟同向涨跌幅的均值减去对照组均值，自助抽样 10000 次算 95% 置信区间，下限大于 0。
2. **够付手续费**：信号组均值不低于 0.10%（来回都按吃单 0.05%）。进场吃单、离场挂单的 0.07% 另列一行，只作参考。
3. **结果稳定**：做多、做空分开算，均值都为正；按周（UTC，周一起）拆开，至少四分之三的周均值为正。

**口径：**

- 信号组只取进场门槛那一档（40）的信号；30、50 档是为以后校准门槛记的，不参与判定。
- 同向涨跌幅 = (信号后 5 分钟的价格 ÷ 起算价 − 1) × 方向；起算价用 600 USDT 市价单的预计成交价（`mkt_fill_px`），不用中间价。
- 相邻信号间隔不足 5 分钟的，只保留前一条（和上一条保留下来的信号比）。
- 对照组每小时一条，方向随机，种子固定（`events.control_seed`）。自助抽样的种子也固定（`evaluation.bootstrap_seed`），同一份数据结果不变。
- 5 分钟是唯一的判定口径。15 秒、1、2、15、30 分钟的结果照常列在报告里，不参与判定。
- 数据量不够（去重后少于 300 条信号，或覆盖不足 2 周）时报告照出，但结论写「不做判定」。

## 回放工具

读已保存的逐笔成交、盘口快照、持仓量等原始数据，重新生成桶和信号事件（§14）。输出写到单独目录，不覆盖实时数据。

```bash
python -m flowmon replay --config config.toml --from 2026-10-05 --to 2026-10-06 --compare
```

改桶宽或分数参数时，先复制一份配置改好，再用 `--config 新配置 --src 原数据目录` 回放。`--compare` 会逐桶比对分数、逐条比对事件。
桶宽不变时，回放的分数和事件与实时完全一致（端到端测试就是这么验的）。已知差别：

- 撤单量需要完整的盘口增量，原始数据只存了前 50 档快照，回放时为空。
- 近处挂单只能用存下的前 50 档算，比实时（400 档）更容易铺不满。
- 数据延迟只用逐笔成交算，实时还包含盘口推送。
- 完整性沿用实时记录；实时没在运行的时段直接跳过。
- 实时里封桶后才到的成交（`late_trades`）回放时会算进它本该在的桶。

分数模块可以单独调用，阶段二直接复用：

```python
from flowmon import config
from flowmon.score import score_series
cfg = config.load("config.toml")
results = score_series(rows, cfg.score, cfg.bucket.width_s)  # rows 要有 start_ms complete buy_vol sell_vol close oi
```

## 离线试跑（本地假交易所）

不连 OKX，用合成行情把整条链路跑一遍。合成行情只用来验证链路，不能用来评价策略。

```bash
python -m flowmon sim --speed 60 --fault 1500:seq_gap --fault 2400:disconnect &
# 另复制一份配置，把 ws_public_url 改成 ws://127.0.0.1:18765、rest_base_url 改成 http://127.0.0.1:18766
python -m flowmon run --config sim.toml --duration 300
```

`--speed 60` 表示真实 1 秒 = 行情 1 分钟。加速时 `latency_ms` 是负的大数，属正常（推送时间戳是模拟时间）。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q tests
```

- `test_score.py`：F、M、A 手算核对；成交量倍数上限、两条修正及其顺序、空头对称、24 小时预热、不完整桶和缺桶；
  另写了一份逐字照 §6 的朴素实现（每个桶都把窗口从头扫一遍），用随机数据逐桶比对。
- `test_orderbook.py`：快照与增量、seqId 断档、空推送与 seqId 重置、价格交叉、checksum 拼接规则、近处挂单；撤单量估算的手工小例子。
- `test_bucket_events.py`：封桶时机、断线区间标记、持仓量过期、晚到成交、桶结束时的盘口截图、经济数据窗口、翻转和低波动；
  信号穿越（多档、空头、上一个桶无效）、之后各时点价格和 MFE/MAE、限价单成交判定、对照组可复现、停机断档。
- `test_evaluate.py`：§11 判定的收益口径（起算价用预计成交价）、5 分钟去重、自助区间可复现，以及通过、手续费不够、空头为负、按周不稳、数据不够五种情形。
- `test_end_to_end.py`：起一个加速 60 倍的假交易所，实时监控器中途重启一次，注入 seqId 断档、断线、停推，然后回放并逐桶比对，要求分数零差异。

## 与 OKX 官方文档的核对（§4、§14）

开发环境连不上 `www.okx.com`，官方文档没能直接打开。下表「依据」一栏写明来源；标「待确认」的，部署前请对照[官方文档](https://www.okx.com/docs-v5/en/)再看一眼，有出入以官方为准。

| 项目 | 规格写的 | 核对结果 | 依据 |
| --- | --- | --- | --- |
| 盘口校验 | 按文档做校验 | **与规格有差异，已确认**：OKX 自 2026-06-23 起，正式环境盘口频道的 `checksum` 字段仍在，但始终为 0，不能再用来校验；公告要求改用 `seqId`/`prevSeqId` 验证连续性。本实现：新消息的 `prevSeqId` 必须等于上一条的 `seqId`，否则重新订阅；`checksum` 非 0 时顺带核对 | OKX 公告 [Order Book Channels Checksum Field Deprecation](https://www.okx.com/en-us/help/okx-order-book-channels-checksum-field-deprecation)，已人工核对原文 |
| 盘口频道 | `books` 400 档 | 先推快照（`action=snapshot`，`prevSeqId=-1`），再推增量（`action=update`）；约 100 毫秒一次；不需要登录。长时间无变化时推 `seqId=prevSeqId` 的空消息；维护时 `seqId` 可能变小，但 `prevSeqId` 仍接得上 | 快照与增量有搜索结果佐证；空消息和重置规则凭记忆，待确认 |
| 档位格式 | — | `[价格, 数量(张), 已废弃字段, 订单数]`，数量 `"0"` 表示删除该档 | 凭记忆，待确认 |
| 逐笔成交 | `trades` | **与规格有差异**：`trades` 是聚合推送，同一主动单在同一价格的多笔撮合合成一条，`count` 是撮合笔数；不聚合的逐笔在 business 端点的 `trades-all`。两者成交量相同，本实现用 `trades`，成交笔数取 `count` 之和 | 搜索结果摘要；待确认 |
| 持仓量 | `open-interest` | 有变化时约 3 秒推一次，字段 `oi`（张）、`oiCcy`（币）、`ts` | 凭记忆，待确认 |
| 资金费率 | `funding-rate` | 30–90 秒推一次 | 凭记忆，待确认 |
| 强平单 | `liquidation-orders` | 按 `instType=SWAP` 订阅，会收到全部永续合约，程序按 `instId` 过滤；字段 `side`、`posSide`、`bkPx`、`sz`、`bkLoss`、`ts`。**同一合约每秒最多展示一条，不代表全部强平量**，`liq_*_vol` 只能当下限看。`sz` 按张换算 | 字段名有搜索结果佐证；频率限制和单位凭记忆，待确认 |
| 心跳 | 定时发心跳 | 30 秒内没有任何往来会被断开；发文本 `ping`，回 `pong`。本实现每 15 秒发一次 `ping`，另按 §12 超过 5 秒没行情主动重连 | 二手资料一致 |
| 合约信息 | 公开接口读取 | `GET /api/v5/public/instruments?instType=SWAP&instId=BTC-USDT-SWAP`，取 `ctVal`、`ctMult`、`lotSz`、`minSz`、`tickSz`；启动时读，失败用缓存 | 凭记忆，待确认 |

## 实现时补充的约定

规格没写死、实现时必须定下来的地方。都没有改动规格的规则或参数；涉及数字的都在配置里。

1. **桶的归属**：按交易所时间戳，左闭右开 `[起点, 起点+15 秒)`。水位线（收到的最大交易所时间戳）越过「桶结束 + 0.5 秒」才封桶，给晚到的推送留时间；完全没行情时按本地时钟兜底封桶。
2. **桶结束时的盘口**：在第一条时间戳 ≥ 桶结束的盘口增量套用之前截图。
3. **持仓量**：取时间戳早于桶结束的最后一条；距桶结束超过 30 秒，该桶标为不完整。
4. **不完整的原因**：`startup`（启动到收到第一份盘口快照）、`disconnect`（断线到重连后收到快照）、`stale`（超过 5 秒没行情，主动重连）、`book_invalid`（seqId 断档、买一 ≥ 卖一、checksum 非 0 且对不上，到重新订阅后收到快照）、`oi_stale` / `oi_missing`、`no_book`、`no_price`。
5. **单位**：成交量、持仓量、挂单量、撤单量、强平量都换算成 BTC（张数 × `ctVal` × `ctMult`）；原始数据保留交易所原文。
6. **M 的分母**：24 小时窗口里完整桶的总成交量 ÷ 这些桶覆盖的分钟数；「最近 1 分钟」就是成交方向窗口那 4 个桶。
7. **Z 的标准差**：24 小时内每个桶的 5 分钟变化量（两端都完整才算），总体标准差，含当前桶。
8. **最近 1 分钟价格变化**：当前桶收盘 − 4 个桶之前那个桶的收盘。变化为 0 不算「价格不配合」。
9. **基准值有效**：历史铺满 24 小时，且其中完整桶至少占 80%（`baseline_min_coverage`）。
10. **窗口不跨缺口**：任何窗口里有缺桶或不完整桶，该桶分数无效。平滑要求 4 个 R 都有效。
11. **信号穿越**：上一个桶分数有效且在门槛以内，本桶有效且到达门槛。上一个桶无效不算穿越（比如断线恢复后分数直接就在门槛外）。一次跨过多档时每档各记一条。
12. **事件里的价格**：信号时价格 = 触发那个桶的最后一笔成交；之后各时点价格 = 对应时点所在桶的收盘。
13. **市价单估算**：用桶结束时截下的前 50 档，按名义价值 600 USDT 逐档吃，没有按最小下单量取整。
14. **限价单成交**：价格穿过挂单价，或在挂单价上成交的量超过当时排在前面的量。不计前面的人撤单，结果偏保守。
15. **对照组**：每个 UTC 小时用固定种子抽一个时刻和一个随机方向；到点时数据不完整、分数无效或正好有信号，就顺延到本小时内下一个桶。同一份数据回放时抽到同一批时刻。
16. **撤单量**：同一个桶内按价位汇总「减少的挂单量 − 该价位成交量」，取正，只统计中间价上下 0.2% 内的价位（`cancel_range_pct`）。远处价位的减少多半是 400 档窗口滑动，不是撤单。
17. **低波动**：30 分钟幅度 = (最高 − 最低) / 收盘，只用完整桶；历史不满 7 天时用已有的历史算分位，`range_hist_h` 记下用了多少小时。
18. **不交易条件只记市场侧四条**。「连续 2 次亏损暂停」和风控表要看模拟成交的盈亏，阶段一没有仓位，留给阶段二回放判断（参数已在配置里）。
19. **重启**：读回最近 7 天的桶恢复基准值、分位和翻转计数；没跟踪完的事件从状态文件接着跟。停机期间缺的桶会让这些事件 `followup_complete=0`；需要逐笔成交判定、还没判定的限价单窗口记为空。停机错过的日报重启时补写。

## 需要你确认的问题

没有擅自改规格，下面这些请你定：

1. **处于 §8 不交易条件的信号算不算进判定**。目前算（`evaluation.exclude_no_trade = false`）：阶段一判的是信号本身有没有用，不交易条件留给阶段二。如果你认为只该看「真的会去交易」的信号，改成 `true`。要在看到结果之前定。
2. **「至少 300 次信号」按去重前还是去重后数**。目前按去重后、且有 5 分钟价格的条数算，偏保守。
3. **按周拆开时「为正」用什么**。目前用该周信号的 5 分钟同向涨跌幅均值（扣手续费之前）大于 0，和第 3 条「多空均值都为正」同一个口径。
4. **「分数正负翻转」按原文几乎一直成立**。平静时分数在 0 附近小幅摆动，+0.3 变 −0.2 也算一次翻转。2 天模拟里，84 条 40 档信号有 83 条因此被标为不交易（见 [docs/sim-run.md](docs/sim-run.md)），真实行情大概率也这样。目前按原文执行；加了开关 `conditions.flip_min_abs`，比如设成 15（清仓门槛），就只有分数在 ±15 以外时才参与翻转计数。这一条会直接影响阶段二能交易多少次，也影响第 1 个问题。
5. **近处挂单范围可能超出盘口深度**。BTC 在 6 万美元时，±0.2% 是 ±120 美元；`books` 只给 400 档，如果档位密集，400 档可能覆盖不到这个范围（`near_truncated=1`）。真实数据跑起来后看这一列：如果大部分桶都是 1，要么缩小范围，要么接受它是「400 档内」的近似。
6. **强平量只是下限**（见核对表）。如果以后打算把强平量放进分数，需要换数据源。
