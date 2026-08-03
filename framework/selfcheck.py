"""结构自检（selfcheck.py）。

读 diagnostics.selfcheck（strategy/selector/interval_hours）。
对应 source-schema-v2 §7.2 与 ui-source.md 健康状态灯。

策略：
- off    : 跳过自检
- soft   : 失败仅 WARN（告警），请求照常
- strict : 失败抛 StructureChangedError，请求中止
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from .config import SourceConfig
from .errors import StructureChangedError
from .http import HttpClient
from .parser import Parser

log = logging.getLogger(__name__)


class StructureChecker:
    def __init__(self, http: HttpClient, parser: Parser, default_strategy: str = "soft"):
        self._http = http
        self._parser = parser
        self._default_strategy = default_strategy
        self._last_check: dict[str, tuple[float, bool]] = {}

    # ------------------------------------------------------------------ #
    def _get_config(self, source: SourceConfig):
        diag = source.raw.get("diagnostics") or {}
        sc = diag.get("selfcheck") or {}
        strategy = sc.get("strategy") or self._default_strategy
        selector = sc.get("selector")
        interval_hours = float(sc.get("interval_hours") or 0)
        return strategy, selector, interval_hours

    def check(self, source: SourceConfig, url: str) -> bool:
        """自检。返回 True=通过或跳过；strict 失败抛 StructureChangedError。

        interval_hours 为 0 时也加最小缓存 60s，避免短时间重复自检。
        """
        strategy, selector, interval_hours = self._get_config(source)
        if strategy == "off" or selector is None:
            return True

        # interval 缓存：间隔内不重复自检；0 也加最小 60s 缓存
        now = time.time()
        min_interval = max(interval_hours * 3600, 60)  # 至少 60s 缓存
        last = self._last_check.get(source.source_id)
        if last and now - last[0] < min_interval:
            return last[1]

        try:
            html = self._http.get_text(
                url,
                headers=source.request_headers(),
                timeout=float(source.raw.get("transports", {}).get("timeout") or 10),
                retries=int(source.raw.get("transports", {}).get("retries") or 3),
            )
        except Exception:
            ok = False
        else:
            doc = self._parser.parse(html)
            ok = bool(self._parser.extract(doc, selector))

        self._last_check[source.source_id] = (now, ok)
        if not ok and strategy == "strict":
            raise StructureChangedError(
                f"站点结构已变更，请更新源配置（校验标签未命中 {url}）",
                source_id=source.source_id,
            )
        if not ok:
            log.warning("[%s] 结构自检失败（soft）：%s", source.source_id, url)
        return ok
