"""爆仓数据与防双杀规则：BTC、ETH、SOL 永续，只记录，不下单。

  config.py     liq.toml（和 config.toml 放在一起；没有这个文件就不采集）
  types.py      数据结构：爆仓单、K 线、小时汇总、爆仓价位图
  store.py      落盘：data/liq/ 下按天分文件，去重
  rest.py       OKX 公开 REST 接口（限频、重试、翻页）
  fake.py       假交易所（测试和离线试跑用）
  hourly.py     每小时汇总：爆仓量、7 天倍数、平均小时波动、持仓量变化、多空人数比
  heatmap.py    爆仓价位估算（过去 72 小时新增持仓 × 25/50/100 倍杠杆）
  strategy.py   「逆散户 + 触发」信号、防双杀过滤、两种出场、模拟交易
  report.py     报告：每个版本、每条过滤开和关的结果
  collector.py  实时采集（在监控器进程里运行）
"""
