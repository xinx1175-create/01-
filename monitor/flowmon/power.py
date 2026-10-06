"""运行在自己的 Mac 上：运行期间不让电脑睡眠，并留意供电方式。

- 阻止睡眠：挂一个 `caffeinate -i -m -s -w <本进程>`。-i 阻止空闲睡眠，-m 阻止磁盘空闲休眠，
  -s 阻止系统睡眠（只在接着电源时有效）。-w 让它跟着本进程走：监控器一退出，阻止就自动解除，
  不改系统的电源设置。Mac 的「休眠」（把内存写到磁盘再断电）只会在睡眠之后发生，挡住睡眠也就挡住了休眠。
  合上笔记本的盖子仍然会睡眠（外接显示器的合盖模式除外），这是 macOS 的硬性行为，程序挡不住。
- 供电方式：`pmset -g batt` 第一行。改用电池（拔了电源或停电）时推送一条。

不是 macOS 的系统上这些都不做（Linux 服务器本来就不睡眠）。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys

log = logging.getLogger(__name__)

CAFFEINATE = "/usr/bin/caffeinate"
PMSET = "/usr/bin/pmset"
CAFFEINATE_FLAGS = ["-i", "-m", "-s"]


def is_macos() -> bool:
    return sys.platform == "darwin"


def _run(args: list[str], timeout: float = 5) -> str | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def power_source() -> str | None:
    """'AC Power' / 'Battery Power' / 'UPS Power'；不是 macOS 或读不到时返回 None。"""
    if not is_macos():
        return None
    out = _run([PMSET, "-g", "batt"])
    if not out:
        return None
    m = re.search(r"drawing from '([^']+)'", out)
    return m.group(1) if m else None


def sleep_assertions() -> list[str]:
    """`pmset -g assertions` 里和 caffeinate 有关的行，用来确认阻止睡眠确实生效。"""
    if not is_macos():
        return []
    out = _run([PMSET, "-g", "assertions"]) or ""
    return [ln.strip() for ln in out.splitlines() if "caffeinate" in ln]


class SleepGuard:
    def __init__(self, enabled: bool, pid: int | None = None):
        self.enabled = enabled and is_macos()
        self.pid = pid or os.getpid()
        self.proc: subprocess.Popen | None = None

    def command(self) -> list[str]:
        return [CAFFEINATE, *CAFFEINATE_FLAGS, "-w", str(self.pid)]

    def start(self) -> None:
        if not self.enabled:
            return
        try:
            self.proc = subprocess.Popen(self.command(), stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.info("已阻止系统睡眠：%s（pid %d）", " ".join(self.command()), self.proc.pid)
        except OSError as e:
            self.proc = None
            log.error("启动 caffeinate 失败，电脑可能会睡眠：%s", e)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def ensure(self) -> bool:
        """caffeinate 意外退出时重新挂上。返回 True 表示这次重新挂了。"""
        if not self.enabled or self.alive():
            return False
        log.warning("caffeinate 不在了，重新阻止睡眠")
        self.start()
        return True

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
