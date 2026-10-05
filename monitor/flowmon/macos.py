"""macOS 后台服务（launchd）：登录后自动启动监控器，崩溃后自动拉起。

服务定义写在 ~/Library/LaunchAgents/com.flowmon.monitor.plist：
  RunAtLoad   登录后（包括开机后登录）自动启动
  KeepAlive   进程不管因为什么退出，launchd 都会再拉起来；ThrottleInterval 限制最快 10 秒一次
  ExitTimeOut 停止服务时先发 SIGTERM，监控器落盘、保存状态后退出；30 秒还没退才强杀

用户级服务（LaunchAgent）而不是系统级（LaunchDaemon）：不需要管理员权限，数据都在自己的用户目录下。
代价是必须有人登录过一次才会启动。开了 FileVault（磁盘加密，Mac 默认开启）时，开机后本来就要先输入
密码解锁磁盘，系统级服务也一样要等，所以两者在这一点上没有差别。
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

LABEL = "com.flowmon.monitor"
LAUNCHCTL = "/bin/launchctl"
STOP_WAIT_S = 40  # 比 ExitTimeOut 多留一点
# 后台服务不继承终端里的环境变量。安装时终端里设了代理，就把它们写进服务定义，免得前台能连上、后台连不上。
# 系统设置里的代理（「网络 → 代理」）不用管：程序自己会读到
PROXY_ENV = ("http_proxy", "https_proxy", "all_proxy", "no_proxy",
             "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def domain() -> str:
    return f"gui/{os.getuid()}"


def target() -> str:
    return f"{domain()}/{LABEL}"


def package_dir() -> Path:
    """flowmon 包所在的目录（也就是仓库里的 monitor/），python -m flowmon 要从这里运行。"""
    return Path(__file__).resolve().parent.parent


def render_plist(python: str, config: Path, workdir: Path, log_dir: Path,
                 extra_env: dict[str, str] | None = None) -> bytes:
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [python, "-m", "flowmon", "run", "--config", str(config), "--no-console-log"],
        "WorkingDirectory": str(workdir),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ExitTimeOut": 30,
        # 不让系统把它当成可以随意降速的后台任务
        "ProcessType": "Interactive",
        # 正常日志写在 logs/flowmon.log（按天切分）；这两个文件只接启动失败、未捕获异常这类输出
        "StandardOutPath": str(log_dir / "launchd.out.log"),
        "StandardErrorPath": str(log_dir / "launchd.err.log"),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "LANG": "en_US.UTF-8", **(extra_env or {})},
    })


def proxy_env() -> dict[str, str]:
    return {k: os.environ[k] for k in PROXY_ENV if os.environ.get(k)}


def venv_python() -> str:
    """当前解释器的路径，保持虚拟环境里的那个链接，不要解析成 Homebrew 的原始 python（那里没装依赖）。"""
    return str(Path(sys.executable).absolute())


def is_loaded(run: Runner = _run) -> bool:
    return run([LAUNCHCTL, "print", target()]).returncode == 0


def status(run: Runner = _run) -> dict:
    """launchctl print 里关心的几项：state、pid、runs（启动过几次）、last exit code。"""
    r = run([LAUNCHCTL, "print", target()])
    if r.returncode != 0:
        return {"loaded": False}
    out = {"loaded": True}
    for line in r.stdout.splitlines():
        line = line.strip()
        for key in ("state", "pid", "runs", "last exit code", "last terminating signal"):
            if line.startswith(key + " = "):
                out.setdefault(key, line.split(" = ", 1)[1])
    return out


def _wait_unloaded(run: Runner, sleep=time.sleep) -> bool:
    for _ in range(STOP_WAIT_S):
        if not is_loaded(run):
            return True
        sleep(1)
    return not is_loaded(run)


def install(config: Path, log_dir: Path, run: Runner = _run, sleep=time.sleep) -> list[str]:
    """写服务定义并立即启动。已经装过就先停掉旧的再装（用来更新路径或参数）。返回给人看的说明。"""
    msgs = []
    p = plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    if is_loaded(run):
        run([LAUNCHCTL, "bootout", target()])
        _wait_unloaded(run, sleep)
        msgs.append("已停掉正在运行的旧服务")
    env = proxy_env()
    p.write_bytes(render_plist(venv_python(), config.resolve(), package_dir(), log_dir.resolve(), env))
    msgs.append(f"已写入 {p}")
    if env:
        msgs.append(f"终端里设了代理，已一并写进服务：{', '.join(sorted(env))}")
    run([LAUNCHCTL, "enable", target()])
    r = run([LAUNCHCTL, "bootstrap", domain(), str(p)])
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap 失败：{(r.stderr or r.stdout).strip()}")
    msgs.append("服务已启动；以后每次登录都会自动启动，崩溃后 10 秒内自动拉起")
    return msgs


def start(run: Runner = _run) -> list[str]:
    p = plist_path()
    if not p.exists():
        raise RuntimeError("还没安装服务，先运行 service install")
    if is_loaded(run):
        return ["服务已经在运行"]
    r = run([LAUNCHCTL, "bootstrap", domain(), str(p)])
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap 失败：{(r.stderr or r.stdout).strip()}")
    return ["服务已启动"]


def stop(run: Runner = _run, sleep=time.sleep) -> list[str]:
    """停止服务（正常停止：落盘、保存状态）。服务定义保留，下次登录还会自动启动。"""
    if not is_loaded(run):
        return ["服务没有在运行"]
    run([LAUNCHCTL, "bootout", target()])
    if not _wait_unloaded(run, sleep):
        raise RuntimeError("等了 40 秒服务还没停下")
    return ["服务已停止（下次登录会自动启动；要彻底去掉用 service uninstall）"]


def restart(run: Runner = _run, sleep=time.sleep) -> list[str]:
    """先正常停止再启动（更新代码后用）。不用 kickstart -k：它会直接杀进程，来不及落盘。"""
    return stop(run, sleep) + start(run)


def uninstall(run: Runner = _run, sleep=time.sleep) -> list[str]:
    msgs = stop(run, sleep)
    p = plist_path()
    if p.exists():
        p.unlink()
        msgs.append(f"已删除 {p}，以后不会自动启动")
    return msgs
