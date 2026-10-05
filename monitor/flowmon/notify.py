"""推送到手机（§12）。渠道在配置文件 [notify] 里选：

  ntfy     POST 到 https://ntfy.sh/<主题>（手机装 ntfy 订阅同一主题）
  bark     GET  https://api.day.app/<key>/<标题>/<正文>（iOS Bark）
  webhook  POST JSON {"title": ..., "body": ...} 到任意地址

发送失败只记日志，不影响监控器运行。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request

from .config import NotifyCfg

log = logging.getLogger(__name__)

# 推送请求本身的超时，属于网络调用细节，不是策略参数
_TIMEOUT_S = 10


class Notifier:
    def __init__(self, cfg: NotifyCfg, prefix: str):
        self.cfg = cfg
        self.prefix = prefix
        self._last: dict[str, float] = {}

    def send(self, title: str, body: str, key: str | None = None) -> bool:
        """同步发送。key 不为空时按 min_interval_s 限频，同类告警不刷屏。"""
        if self.cfg.kind == "none":
            log.info("[通知未启用] %s：%s", title, body)
            return False
        if key is not None:
            t = time.monotonic()
            last = self._last.get(key)
            if last is not None and t - last < self.cfg.min_interval_s:
                return False
            self._last[key] = t
        title = f"{self.prefix} {title}"
        try:
            if self.cfg.kind == "ntfy":
                # 中文标题放在查询参数里，HTTP 头只能放 ASCII
                sep = "&" if "?" in self.cfg.url else "?"
                url = self.cfg.url + sep + urllib.parse.urlencode({"title": title})
                req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
            elif self.cfg.kind == "bark":
                url = (self.cfg.url.rstrip("/") + "/" + urllib.parse.quote(title, safe="")
                       + "/" + urllib.parse.quote(body, safe=""))
                req = urllib.request.Request(url, method="GET")
            else:
                req = urllib.request.Request(
                    self.cfg.url, method="POST",
                    data=json.dumps({"title": title, "body": body}, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
                r.read()
            log.info("已推送：%s", title)
            return True
        except Exception as e:
            log.error("推送失败（%s）：%s", self.cfg.kind, e)
            return False
