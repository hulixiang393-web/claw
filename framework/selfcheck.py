"""结构自检（selfcheck.py）。

读 diagnostics.selfcheck（strategy/selector/interval_hours/hard_failures）。
对应 source-schema-v2 §7.2 与 ui-source.md 健康状态灯。

策略：
- off    : 跳过自检
- soft   : 失败仅 WARN（告警），请求照常
- strict : 确认结构不符抛 StructureChangedError，请求中止

判定规则（避免把活源标死 / 死源标正常）：
- 网络错误/超时 → 软失败（False → WARN）：瞬时抖动不判"结构变更"；
- 疑似反爬/验证页 → 软失败（False → WARN）：无法验证 ≠ 结构不符；
- 仅"确实取到页面且校验标签未命中"才视为结构不符，且只在自检指定页面
  （discovery 列表入口）上判定：连续 hard_failures 次未命中（strict 首次即判）
  才抛 StructureChangedError（→ BROKEN）。详情/章节/视频页不含列表选择器，
  页面可达即算通过（→ OK），不因结构未命中降级。

check() 返回：
- True  = 通过或跳过（off / 无 selector / 非指定页可达）
- False = 软失败（网络/反爬/指定页未命中但未达硬失败阈值）→ 调用方降级 WARN
- 硬失败抛 StructureChangedError → 调用方标 BROKEN
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .config import SourceConfig
from .errors import StructureChangedError
from .http import HttpClient
from .parser import Parser

log = logging.getLogger(__name__)

# 疑似反爬 / 验证 / 拦截页特征（命中任一 → 视为"无法验证"，而非"结构变更"）
_ANTIBOT_MARKERS = (
    "just a moment",
    "attention required",
    "__cf_chl",
    "cf-chl",
    "challenge-platform",
    "captcha",
    "robot check",
    "verify you are human",
    "access denied",
    "your request has been blocked",
    "验证码",
    "安全验证",
    "访问验证",
    "人机验证",
    "请求被拒绝",
    "访问过于频繁",
)


class StructureChecker:
    def __init__(
        self,
        http: HttpClient,
        parser: Parser,
        default_strategy: str = "soft",
        hard_fail_threshold: int = 2,
    ):
        self._http = http
        self._parser = parser
        self._default_strategy = default_strategy
        # 判定 broken 所需连续结构未命中次数（≥1）；默认 2，首次未命中先降级 WARN
        self._hard_fail_threshold = max(1, hard_fail_threshold)
        # 缓存：key=(source_id, url) → (ts, state)；state: ok / soft / hard
        self._last_check: dict[tuple[str, str], tuple[float, str]] = {}
        # 连续结构未命中次数（仅自检指定页累计），按 source_id
        self._structure_streak: dict[str, int] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def _get_config(self, source: SourceConfig):
        diag = source.raw.get("diagnostics") or {}
        sc = diag.get("selfcheck") or {}
        strategy = sc.get("strategy") or self._default_strategy
        selector = sc.get("selector")
        interval_hours = float(sc.get("interval_hours") or 0)
        threshold = int(sc.get("hard_failures") or self._hard_fail_threshold)
        return strategy, selector, interval_hours, max(1, threshold)

    # ------------------------------------------------------------------ #
    def _selfcheck_url(self, source: SourceConfig) -> str:
        """自检指定页面 URL：优先 discovery 列表入口，否则 base_url/homepage。

        与 gui/pages/source_page.diagnose_url 同一套规则。仅该页面可判结构硬失败
        （selfcheck.selector 针对的是列表页结构，详情/章节/视频页不含这些标签）。
        """
        from urllib.parse import urljoin

        raw = source.raw
        endpoints = raw.get("endpoints") or {}
        disc = endpoints.get("discovery") or {}
        rel = (
            disc.get("works_list_url")
            or disc.get("list_url")
            or disc.get("list_categories_url")
        )
        base = source.base_url or source.homepage
        if not base:
            return ""
        if rel:
            return rel if rel.startswith("http") else urljoin(base, rel)
        return base

    @staticmethod
    def _is_antibot_page(html: str) -> bool:
        """页面内容疑似反爬/验证/拦截页（HTTP 200 但内容异常）。"""
        if not html:
            return True
        head = html[:30000].lower()
        return any(marker in head for marker in _ANTIBOT_MARKERS)

    # ------------------------------------------------------------------ #
    def check(self, source: SourceConfig, url: str) -> bool:
        """自检。返回 True=通过或跳过；False=软失败（调用方降级 WARN）。

        硬失败（结构确认变更）抛 StructureChangedError，调用方标 BROKEN。
        网络/超时/反爬均为软失败，不判"结构变更"；结构未命中需连续
        hard_failures 次（strict 策略第 1 次即判）才升为硬失败。

        interval_hours 为 0 时也加最小缓存 60s，避免短时间重复自检。
        """
        strategy, selector, interval_hours, threshold = self._get_config(source)
        if strategy == "off" or selector is None:
            return True

        # interval 缓存：间隔内不重复自检；0 也加最小 60s 缓存。
        # key 用 (source_id, url)：同一源不同页面各自缓存，避免跨页面重复整页请求。
        now = time.time()
        min_interval = max(interval_hours * 3600, 60)  # 至少 60s 缓存
        cache_key = (source.source_id, url)
        with self._lock:
            last = self._last_check.get(cache_key)
            if last and now - last[0] < min_interval:
                # 硬失败缓存仍抛异常（保持硬失败语义）；软失败不重判为结构变更
                if last[1] == "hard":
                    raise StructureChangedError(
                        f"站点结构已变更，请更新源配置（校验标签未命中 {url}）",
                        source_id=source.source_id,
                    )
                return last[1] == "ok"

        # strict：首次结构未命中即硬失败；soft：需连续 threshold 次
        effective_threshold = 1 if strategy == "strict" else threshold
        state = self._perform_check(source, url, selector, effective_threshold)

        with self._lock:
            self._last_check[cache_key] = (now, state)
        if state == "hard":
            raise StructureChangedError(
                f"站点结构已变更，请更新源配置（校验标签未命中 {url}）",
                source_id=source.source_id,
            )
        if state == "soft":
            log.warning("[%s] 结构自检软失败：%s", source.source_id, url)
        return state == "ok"

    def _perform_check(self, source, url, selector, threshold) -> str:
        """执行一次自检，返回状态：ok / soft / hard。

        判定要点（避免把活源标死）：
        - 网络错误/超时 → soft（可达性失败 ≠ 结构变更）
        - 反爬/验证页 → soft（无法验证 ≠ 结构变更）
        - 非自检指定页结构未命中 → ok（列表选择器不覆盖详情/章节/视频页，
          页面能取到即算源活着，结构校验只在指定页做）
        - 自检指定页结构未命中 → 连续未命中达 threshold 次才 hard，否则 soft
        """
        try:
            html = self._http.get_text(
                url,
                headers=source.request_headers(),
                timeout=float(source.raw.get("transports", {}).get("timeout") or 10),
                retries=int(source.raw.get("transports", {}).get("retries") or 3),
                encoding=source.transports().get("charset"),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "[%s] 自检可达性失败（非结构变更）：%s %s", source.source_id, url, exc
            )
            return "soft"

        doc = self._parser.parse(html)
        if self._parser.extract(doc, selector):
            if url == self._selfcheck_url(source):
                with self._lock:
                    self._structure_streak[source.source_id] = 0
            return "ok"

        # 结构未命中
        if url != self._selfcheck_url(source):
            # 非自检指定页：页面可达即通过（选择器不适用于该页型）
            return "ok"
        if self._is_antibot_page(html):
            log.warning(
                "[%s] 自检疑似反爬/验证页，无法验证结构：%s", source.source_id, url
            )
            return "soft"
        with self._lock:
            streak = self._structure_streak.get(source.source_id, 0) + 1
            self._structure_streak[source.source_id] = streak
        if streak >= threshold:
            log.warning(
                "[%s] 自检结构未命中（连续 %d 次）：%s", source.source_id, streak, url
            )
            return "hard"
        log.warning(
            "[%s] 自检结构未命中（第 %d 次，未达阈值，先降级 WARN）：%s",
            source.source_id, streak, url,
        )
        return "soft"
