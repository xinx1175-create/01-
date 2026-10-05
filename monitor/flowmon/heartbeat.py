"""外部心跳报到：每分钟访问一次心跳服务给的地址（GET）。

心跳服务（推荐 Healthchecks.io）在约定时间内收不到报到就通知手机，
所以电脑断电、断网、睡眠、监控器卡死或收不到行情，都会报警 —— 这些情况下电脑自己发不出通知。

监控器只在「最近 max_data_age_s 秒内收到过完整的桶」时才报到：进程还活着、但行情断了，同样会报警。
"""
from __future__ import annotations

import urllib.error
import urllib.request


def ping(url: str, timeout_s: float) -> str | None:
    """报到一次。成功返回 None，失败返回原因。"""
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "flowmon-heartbeat"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            r.read()
            if not 200 <= r.status < 300:
                return f"HTTP {r.status}"
        return None
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except Exception as e:  # 断网、DNS、超时都在这里
        return f"{type(e).__name__}: {e}"


def redact(url: str) -> str:
    """地址里的 UUID 相当于密码，日志和自检输出只留主机名。"""
    try:
        host = url.split("://", 1)[1].split("/", 1)[0]
    except IndexError:
        return "?"
    return f"{url.split('://', 1)[0]}://{host}/…"
