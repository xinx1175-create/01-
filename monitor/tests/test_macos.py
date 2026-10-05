"""macOS 部分：launchd 服务定义和启停流程、阻止睡眠、供电方式。开发环境是 Linux，系统命令都用假的代替。"""
import asyncio
import plistlib
import subprocess
from pathlib import Path

import pytest

from conftest import make_cfg
from flowmon import macos, power
from flowmon import monitor as monitor_mod
from flowmon.monitor import Monitor
from flowmon.sim import instrument


def test_plist_contents():
    data = plistlib.loads(macos.render_plist("/Users/a/flowmon/venv/bin/python", Path("/Users/a/flowmon/config.toml"),
                                             Path("/Users/a/flowmon/src/monitor"), Path("/Users/a/flowmon/logs")))
    assert data["Label"] == "com.flowmon.monitor"
    assert data["ProgramArguments"] == ["/Users/a/flowmon/venv/bin/python", "-m", "flowmon", "run", "--config",
                                        "/Users/a/flowmon/config.toml", "--no-console-log"]
    assert data["WorkingDirectory"] == "/Users/a/flowmon/src/monitor"
    assert data["RunAtLoad"] is True and data["KeepAlive"] is True
    assert data["ThrottleInterval"] == 10 and data["ExitTimeOut"] == 30
    assert data["StandardErrorPath"] == "/Users/a/flowmon/logs/launchd.err.log"


def test_venv_python_keeps_the_venv_link(tmp_path, monkeypatch):
    real = tmp_path / "brew" / "python3.12"
    real.parent.mkdir()
    real.write_text("")
    link = tmp_path / "venv" / "bin" / "python"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    monkeypatch.setattr(macos.sys, "executable", str(link))
    assert macos.venv_python() == str(link)  # 不能解析成 Homebrew 的原始解释器，那里没装依赖


class FakeLaunchctl:
    def __init__(self):
        self.loaded = False
        self.calls = []

    def __call__(self, args):
        assert args[0] == macos.LAUNCHCTL
        self.calls.append(args[1])
        rc, out = 0, ""
        if args[1] == "print":
            rc = 0 if self.loaded else 113
            out = "gui/501/com.flowmon.monitor = {\n\tstate = running\n\truns = 3\n\tpid = 4242\n" \
                  "\tlast exit code = 0\n\tspawn type = interactive (4)\n}\n" if self.loaded else ""
        elif args[1] == "bootstrap":
            self.loaded = True
        elif args[1] == "bootout":
            self.loaded = False
        return subprocess.CompletedProcess(args, rc, out, "")


def test_install_status_restart_uninstall(tmp_path, monkeypatch):
    monkeypatch.setattr(macos.Path, "home", lambda: tmp_path)
    lc = FakeLaunchctl()
    no_wait = lambda s: None  # noqa: E731
    msgs = macos.install(tmp_path / "config.toml", tmp_path / "logs", run=lc, sleep=no_wait)
    p = tmp_path / "Library" / "LaunchAgents" / "com.flowmon.monitor.plist"
    assert p.exists() and lc.loaded and (tmp_path / "logs").is_dir()
    assert lc.calls == ["print", "enable", "bootstrap"]
    assert plistlib.loads(p.read_bytes())["ProgramArguments"][5] == str((tmp_path / "config.toml").resolve())
    assert any("自动启动" in m for m in msgs)

    st = macos.status(lc)
    assert st == {"loaded": True, "state": "running", "runs": "3", "pid": "4242", "last exit code": "0"}

    lc.calls.clear()
    macos.restart(lc, sleep=no_wait)
    assert lc.calls[0] == "print" and "bootout" in lc.calls and lc.calls[-1] == "bootstrap" and lc.loaded

    lc.calls.clear()
    macos.install(tmp_path / "config.toml", tmp_path / "logs", run=lc, sleep=no_wait)  # 重装：先停旧的
    assert lc.calls[:2] == ["print", "bootout"] and lc.loaded

    assert macos.stop(lc, sleep=no_wait)[0].startswith("服务已停止") and not lc.loaded and p.exists()
    assert macos.start(lc) == ["服务已启动"] and lc.loaded
    macos.uninstall(lc, sleep=no_wait)
    assert not lc.loaded and not p.exists()
    with pytest.raises(RuntimeError):
        macos.start(lc)


def test_bootstrap_failure_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(macos.Path, "home", lambda: tmp_path)

    def broken(args):
        rc = 113 if args[1] == "print" else (5 if args[1] == "bootstrap" else 0)
        return subprocess.CompletedProcess(args, rc, "", "Bootstrap failed: 5: Input/output error")

    with pytest.raises(RuntimeError, match="Input/output error"):
        macos.install(tmp_path / "c.toml", tmp_path / "logs", run=broken, sleep=lambda s: None)


# ---------- 阻止睡眠 ----------

def test_sleep_guard_is_noop_off_macos(monkeypatch):
    monkeypatch.setattr(power, "is_macos", lambda: False)
    g = power.SleepGuard(True, pid=123)
    assert not g.enabled
    g.start()
    assert not g.alive() and not g.ensure()


class FakeProc:
    n = 0

    def __init__(self, args, **kw):
        FakeProc.n += 1
        self.args = args
        self.rc = None
        self.pid = 1000 + FakeProc.n

    def poll(self):
        return self.rc

    def terminate(self):
        self.rc = -15

    def wait(self, timeout=None):
        return self.rc

    def kill(self):
        self.rc = -9


def test_sleep_guard_on_macos(monkeypatch):
    monkeypatch.setattr(power, "is_macos", lambda: True)
    monkeypatch.setattr(power.subprocess, "Popen", FakeProc)
    g = power.SleepGuard(True, pid=123)
    assert g.command() == ["/usr/bin/caffeinate", "-i", "-m", "-s", "-w", "123"]
    g.start()
    assert g.alive() and g.proc.args == g.command()
    g.proc.rc = 0  # caffeinate 意外退出
    assert g.ensure() and g.alive()
    assert not g.ensure()
    proc = g.proc
    g.stop()
    assert proc.rc == -15 and g.proc is None


def test_power_source_parsing(monkeypatch):
    monkeypatch.setattr(power, "is_macos", lambda: True)
    monkeypatch.setattr(power, "_run", lambda args, timeout=5: "Now drawing from 'Battery Power'\n"
                        " -InternalBattery-0 (id=1234)\t87%; discharging; 5:12 remaining present: true\n")
    assert power.power_source() == "Battery Power"
    monkeypatch.setattr(power, "_run", lambda args, timeout=5: "Now drawing from 'AC Power'\n")
    assert power.power_source() == "AC Power"
    monkeypatch.setattr(power, "is_macos", lambda: False)
    assert power.power_source() is None


def test_power_changes_are_notified(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, power={"check_interval_s": 0.05})
    seq = iter(["AC Power", "AC Power", "Battery Power", "Battery Power", "AC Power"] + ["AC Power"] * 100)
    monkeypatch.setattr(monitor_mod, "power_source", lambda: next(seq))
    m = Monitor(cfg, instrument())
    titles = []
    m.notifier.send = lambda title, body, key=None: titles.append(title) or True

    async def main():
        t = asyncio.create_task(m._power())
        await asyncio.sleep(0.5)
        m.stop.set()
        await t
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert titles == ["改用电池供电", "恢复接电源"]
