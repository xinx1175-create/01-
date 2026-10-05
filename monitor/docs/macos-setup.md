# 在自己的 Mac 上从零开始运行监控器

按顺序做。每一步的命令都可以整段复制，粘贴到「终端」里回车。
所有时间、文件名里的日期都是 UTC（北京时间 = UTC + 8 小时）。

整个过程大约 30 分钟，其中安装 Homebrew 最慢（5–15 分钟）。

---

## 0. 准备

- 电脑**插上电源**。笔记本**不要合上盖子**：合盖一定会睡眠，程序挡不住（外接显示器、键盘、电源的「合盖模式」除外）。
- 磁盘至少留 20 GB 空闲（4 周数据约 3–4 GB）。
- 打开「终端」：按 `⌘ + 空格`，输入 `终端`（或 `Terminal`），回车。

## 1. 安装 Homebrew（装软件用的工具）

已经装过的会直接跳过。中途会要求输入开机密码（输入时屏幕上不显示，输完回车即可），以及按一次回车确认。

```bash
command -v brew >/dev/null || /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

让终端以后都能找到它（Apple 芯片的 Mac 需要这一步，Intel 的 Mac 执行了也没有影响）：

```bash
if [ -x /opt/homebrew/bin/brew ]; then
  eval "$(/opt/homebrew/bin/brew shellenv)"
  grep -q 'brew shellenv' ~/.zprofile 2>/dev/null || echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
fi
brew --version
```

最后一行应显示 `Homebrew 4.x.x` 之类的版本号。

## 2. 安装 Python 3.12 和 git

```bash
brew install python@3.12 git
"$(brew --prefix)/bin/python3.12" --version
```

应显示 `Python 3.12.x`。

## 3. 把代码拿到电脑上

代码放在 `~/flowmon`（你的用户目录下）。**不要放在「桌面」「文稿」「下载」里**：macOS 不允许后台服务读写这几个文件夹。

```bash
mkdir -p ~/flowmon
git clone -b claude/ecstatic-thompson-egpex3 https://github.com/xinx1175-create/01-.git ~/flowmon/src
ls ~/flowmon/src/monitor
```

最后一行应列出 `README.md  config.example.toml  flowmon  tests …`。

## 4. 安装

```bash
"$(brew --prefix)/bin/python3.12" -m venv ~/flowmon/venv
~/flowmon/venv/bin/pip install -r ~/flowmon/src/monitor/requirements.txt
cp -n ~/flowmon/src/monitor/config.example.toml ~/flowmon/config.toml
cp -n ~/flowmon/src/monitor/calendar.example.csv ~/flowmon/calendar.csv
```

加一个快捷命令 `flowmon`，以后只要输入 `flowmon 命令` 就行（它会自动带上配置文件）：

```bash
cat >> ~/.zshrc <<'EOF'
# flowmon 监控器：flowmon <命令>，例如 flowmon status
flowmon() { (cd ~/flowmon/src/monitor && ~/flowmon/venv/bin/python -m flowmon "$@" --config ~/flowmon/config.toml) }
EOF
source ~/.zshrc
flowmon --help
```

最后一行应列出 `run, replay, report, evaluate, status, check, service, notify, sim` 这些命令（`sim` 用不上）。

## 5. 确认时间准

数据延迟 = 收到的时间 − 交易所时间戳，本机时钟偏了延迟就不准，桶的归属也会受影响。

1. 打开「系统设置」→「通用」→「日期与时间」，确认「自动设置时间和日期」是打开的。
2. 在终端检查偏差：

```bash
sntp time.apple.com
```

输出里第一个数字是偏差（秒），例如 `+0.0123 +/- 0.03`。绝对值在 0.1 以内就行。

## 6. 设置手机通知和心跳服务

有两路通知，都推到手机上的 ntfy App：

| 谁发 | 什么时候 |
| --- | --- |
| 电脑上的监控器 | 启动、停止、上次没正常停止、连不上交易所、磁盘快满、电脑睡眠过、改用电池供电、每日日报 |
| 心跳服务 Healthchecks.io | 电脑断电、断网、睡眠、监控器卡住或收不到行情 —— 这些时候电脑自己发不出通知 |

监控器每分钟访问一次心跳服务给你的地址（只有最近 2 分钟内收到过完整数据才访问）。心跳服务超过「1 分钟 + 宽限 5 分钟」没收到，就通知你。只发报到信号，不发任何行情或交易数据。

### 6.1 手机装 ntfy，选一个主题名

1. 手机应用商店搜索 **ntfy** 安装（iPhone 和安卓都有，免费，不用注册）。
2. 在电脑终端生成一个别人猜不到的主题名：

```bash
echo flowmon-$(openssl rand -hex 6)
```

   会输出类似 `flowmon-3f9a1c0b7e2d`。记下它。
3. 手机 ntfy 里点「+」订阅这个主题名（服务器用默认的 ntfy.sh）。

### 6.2 注册 Healthchecks.io，建一个检查

1. 打开 <https://healthchecks.io>，用邮箱注册（免费版就够）。
2. 点 **Add Check**，然后点这个检查右边的齿轮（或 **Change Schedule…**）：
   - **Period（周期）**：1 minute
   - **Grace Time（宽限）**：5 minutes
   - 保存
3. 名字改成 `flowmon`（可选）。
4. 复制页面上的 **Ping URL**，形如 `https://hc-ping.com/1a2b3c4d-....`。这串地址相当于密码，不要发给别人。
5. 左上进入 **Integrations**，找到 **ntfy**，点 **Add Integration**：
   - Server：`https://ntfy.sh`
   - Topic：6.1 里的主题名
   - 保存后点 **Test!**，手机应收到一条测试通知。
   - 注册邮箱默认也会收到报警邮件，作为备用。

### 6.3 写进配置文件

把下面两行引号里的内容换成你自己的，然后整段复制执行：

```bash
HC_URL="https://hc-ping.com/把这里换成你的Ping地址"
NTFY_TOPIC="flowmon-换成你的主题名"

sed -i '' "/^\[heartbeat\]/,\$ s#^url = \"[^\"]*\"#url = \"$HC_URL\"#" ~/flowmon/config.toml
sed -i '' "/^\[notify\]/,/^\[power\]/ s#^kind = \"[^\"]*\"#kind = \"ntfy\"#" ~/flowmon/config.toml
sed -i '' "/^\[notify\]/,/^\[power\]/ s#^url = \"[^\"]*\"#url = \"https://ntfy.sh/$NTFY_TOPIC\"#" ~/flowmon/config.toml
grep -A2 '^\[notify\]' ~/flowmon/config.toml; grep -A1 '^\[heartbeat\]' ~/flowmon/config.toml
```

最后会显示这几行（每行后面的 `#` 注释不用管）：

```
[notify]
kind = "ntfy"
url = "https://ntfy.sh/flowmon-你的主题名"
[heartbeat]
url = "https://hc-ping.com/你的Ping地址"
```

发一条测试通知，手机应收到「[flowmon BTC-USDT-SWAP] 测试」：

```bash
flowmon notify 测试 "通知通了"
```

## 7. 前台试跑 2 分钟

```bash
flowmon run --duration 120
```

屏幕上应依次出现：

- `合约信息：ctVal=0.01 BTC …`
- `已阻止系统睡眠：/usr/bin/caffeinate -i -m -s -w …`
- `已连接 wss://ws.okx.com:8443/ws/v5/public，发送订阅`
- `盘口快照就绪 …`
- 之后每 15 秒一行 `桶 2026-…Z 完整=1 S=… 买=… 卖=… OI=… 延迟=…`

第一个桶 `完整=0`（启动中）是正常的，之后应该都是 `完整=1`。
`S=` 在开头几分钟是 `-`，之后有数字但后面跟着 `（无效:warmup）`：分数照算，要积累满 24 小时才算有效。

2 分钟后自动停止。如果出现 `连接断开` 反复刷屏、或者 `解析 … 推送失败`，先别往下做，把屏幕输出发给我。

**用了代理软件的话**：监控器会自动使用「系统设置 → 网络 → 详细信息 → 代理」里的系统代理（HTTP 和 SOCKS 都支持）；在终端里用 `export https_proxy=…` 设的代理，第 8 步装服务时会一并写进去。只要这一步能连上，后台服务也能连上。

## 8. 装成后台服务（开机登录后自动启动、崩溃后自动拉起）

```bash
flowmon service install
```

应显示 `服务已启动；以后每次登录都会自动启动，崩溃后 10 秒内自动拉起`。
macOS 可能弹出「已添加后台项目」的提示，这是正常的；**不要**在「系统设置 → 通用 → 登录项」里把它关掉。

查看状态：

```bash
flowmon service status
```

应显示 `state：running`。

## 9. 防睡眠和断电（只需设置一次）

监控器运行时会自己阻止睡眠（`caffeinate`），停止后自动解除，不改系统设置。另外建议：

1. **系统设置 → 电池（台式机是「节能」）→ 选项**：打开「使用电源适配器且显示器关闭时，防止自动进入睡眠」。双保险。
2. **关掉 macOS 自动安装更新**：系统设置 → 通用 → 软件更新 → 「自动更新」旁的 ⓘ → 关闭「安装 macOS 更新」。自动更新会在夜里重启电脑。
3. **停电后自动开机**（只有台式机 Mac 支持，笔记本有电池不需要）：

```bash
pmset -g cap | grep -q autorestart && sudo pmset -a autorestart 1 && pmset -g | grep autorestart
```

   会要求输入开机密码。笔记本上这条什么都不会做。

**重要**：Mac 默认开着「文件保险箱」（FileVault 磁盘加密）。断电或重启后，必须有人在登录界面输入密码，监控器才会启动。这段时间心跳服务会通知你，回家登录一下就好；停机期间的数据会标为不完整（`downtime`），不影响之后的判定。

## 10. 确认它在正常录数据（装好服务 5 分钟后）

```bash
flowmon status
```

会列出最近 20 个桶：`完整` 一列应该都是 1，`收盘`、`买量`、`卖量` 有数字，时间是最近几分钟（UTC）。

```bash
tail -n 5 ~/flowmon/logs/flowmon.log
pmset -g assertions | grep caffeinate
```

- 日志最后几行时间是刚才（UTC），内容是 `桶 … 完整=1 …`。
- 第二条应显示一行 `caffeinate … PreventUserIdleSystemSleep …` 之类：阻止睡眠在生效。

打开 Healthchecks.io 的页面，检查状态应是绿色的 **up**，「Last Ping」是一分钟以内。

## 11. 启动一小时后：自检，把结果发回来

```bash
flowmon check --bundle
```

会逐项列出「通过 / 不通过」：监控器在运行（以及有没有崩溃后被重新拉起）、最新的桶是不是刚封、最近 60 分钟的桶数和完整率、成交、盘口、持仓量推送频率、分数是否在计算、数据延迟、日志里的错误、阻止睡眠、心跳。
「分数」一项会写「预热中 … 最早 某时刻 有效」，这是正常的：首次启动要积累满 24 小时。

最后一行是 `已打包要发回的文件：/Users/你的用户名/flowmon/flowmon-check-….zip`。在访达里找到它：

```bash
open -R ~/flowmon/flowmon-check-*.zip
```

把这个 zip 文件发给我。里面是：

| 文件 | 用来确认什么 |
| --- | --- |
| `check-….txt` | 自检结论和详细数字（不含心跳地址、通知主题） |
| `logs/flowmon.log`（跨过 UTC 零点时还有前一天的 `flowmon.log.日期`） | 连接、订阅、每个桶一行摘要、警告和错误 |
| `buckets/<日期>.csv` | 这一小时涉及的每一天的全部 15 秒桶，含 F、M、A、Z、S 各项：我用它核对分数算得对不对 |
| `logs/launchd.err.log` | 后台服务启动失败、程序崩溃时的报错（正常是空的） |

配置文件 `config.toml` 不在里面（里面有心跳地址和通知主题），不用发。

## 12. 看日报

每天 UTC 00:30（北京时间 08:30）自动生成前一天的日报，并推送一行摘要到手机，例如
`2026-10-06 运行 24.0h，不完整 0.3%，信号 37 条，平均延迟 85ms，当天数据 92.4 MB`。

完整日报：

```bash
ls ~/flowmon/data/reports/
cat ~/flowmon/data/reports/2026-10-06.md
```

或者现场生成任意一天的（日期换成你要看的那天，UTC）：

```bash
flowmon report --date 2026-10-06
```

日报里有：运行时长、停机时长、不完整桶比例和原因、分数有效的比例、信号和对照组条数、处于不交易条件的比例、数据延迟、**当天数据占用的磁盘空间**（按逐笔成交、盘口快照、持仓量等原文、桶表、事件、日志分开列）、数据目录合计和磁盘剩余。

在访达里看所有文件：

```bash
open ~/flowmon/data
```

## 日常操作

| 要做什么 | 命令 |
| --- | --- |
| 看最近的桶 | `flowmon status` |
| 自检（加 `--bundle` 顺便打包） | `flowmon check` |
| 看服务状态 | `flowmon service status` |
| 停止（下次登录还会自动启动） | `flowmon service stop` |
| 启动 | `flowmon service start` |
| 重启 | `flowmon service restart` |
| 彻底卸掉后台服务 | `flowmon service uninstall` |
| 更新代码 | `cd ~/flowmon/src && git pull && ~/flowmon/venv/bin/pip install -r monitor/requirements.txt && flowmon service restart` |
| 满 2 周、300 条后做阶段一判定 | `flowmon evaluate --write` |

故意停机（比如带电脑出门）前，先在 Healthchecks.io 上点这个检查的 **Pause**，免得一直收到报警；回来后它收到第一次报到会自动恢复。

## 常见问题

- **`flowmon: command not found`**：执行 `source ~/.zshrc`，或者重新打开一个终端窗口。
- **`没有启动：另一个监控器（pid …）正在写数据目录`**：后台服务已经在跑了。要前台试跑，先 `flowmon service stop`。
- **`配置错误：[xxx] 缺少：…`**：更新代码后配置项有增加。对照 `~/flowmon/src/monitor/config.example.toml` 把新的项补进 `~/flowmon/config.toml`。
- **服务状态不是 running，或者一直在重启**：看 `tail -n 50 ~/flowmon/logs/launchd.err.log` 和 `tail -n 50 ~/flowmon/logs/flowmon.log`，发给我。
- **收到「电脑睡眠过」**：通常是合上了笔记本盖子，或者拔了电源后电量低。那段时间的桶标为 `sleep`（不完整），醒来后自动重连。
- **收到「已重新启动（上次没有正常停止）」**：上次是崩溃、被强制结束、断电或关机。停机期间的桶标为 `downtime`。
