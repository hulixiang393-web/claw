"""健康自检公共函数（health.py）。

把 content.py / discovery.py 两份几乎逐字相同的后台自检（_report_health +
_bg_check）收敛为模块级公共函数 report_bg_check。行为与现有实现完全一致：
- checker.check 返回 True → HEALTH_OK（绿）
- 返回 False → HEALTH_WARN（黄，网络/超时/反爬/未达硬失败阈值，不标 dead）
- StructureChangedError → HEALTH_BROKEN（红，结构确认变更）
- 其他异常 → HEALTH_WARN（未知异常不误标 dead）

reporter 为 None 或没有 update_health 方法时静默跳过。
"""

from __future__ import annotations

from typing import Optional, Protocol

from .config import SourceConfig
from .errors import StructureChangedError
from .selfcheck import StructureChecker
from .source_manager import HEALTH_OK, HEALTH_WARN, HEALTH_BROKEN


class _HealthReporter(Protocol):
    """健康上报接口：update_health(source_id, state, error)。"""

    def update_health(self, source_id: str, state: str, error: str = "") -> None: ...


def _report_health(
    reporter, source: SourceConfig, state: str, error: str = ""
) -> None:
    """上报源健康状态（reporter 为 None / 无 update_health 时静默跳过）。"""
    if reporter is None:
        return
    updater = getattr(reporter, "update_health", None)
    if updater is None:
        return
    try:
        updater(source.source_id, state, error)
    except Exception:  # noqa: BLE001
        pass


def report_bg_check(
    checker: StructureChecker,
    reporter: Optional[_HealthReporter],
    source: SourceConfig,
    abs_url: str,
) -> None:
    """后台线程执行结构自检，不阻塞抓取（阅读/下载/播放提速）。

    checker.check() 会对同一 URL 额外发一次 GET（最长 timeout×retries），
    同步等待会让每章下载/每页漫画/每次取流慢一倍。自检仅健康监控，
    移后台 daemon 线程（与 discovery 一致）。

    状态映射（与 GUI 诊断一致，避免把活源标死）：
    - 自检通过（True）→ 绿 ok
    - 软失败（False：网络/超时/反爬/未达硬失败阈值）→ 黄 warn，不标 broken
    - 硬失败（StructureChangedError：结构确认变更）→ 红 broken
    - 其他异常 → 黄 warn（未知异常不误标 dead）
    """
    from threading import Thread

    # 仅自检指定页面才起后台检查线程：章节/漫画/视频页的抓取 URL ≠ 自检指定
    # URL，checker.check 在这些页上恒判 ok（列表选择器不适用该页型），每章/每集
    # 触发只会多一次整页 GET + 一次 health.json 全量写盘（20 源 × 200 章 = 200
    # 次重复 GET + 200 次磁盘写）。非指定页直接返回，不再发后台二次 GET。
    selfcheck_url = ""
    target_fn = getattr(checker, "_selfcheck_url", None)
    if target_fn is not None:
        try:
            selfcheck_url = target_fn(source) or ""
        except Exception:  # noqa: BLE001  自检 URL 计算失败 → 不阻止检查
            selfcheck_url = ""
    if selfcheck_url and abs_url.rstrip("/") != selfcheck_url.rstrip("/"):
        return

    def _run() -> None:
        try:
            ok = checker.check(source, abs_url)
        except StructureChangedError as exc:
            _report_health(
                reporter, source, HEALTH_BROKEN,
                getattr(exc, "message", "") or "站点结构已变更",
            )
            return
        except Exception:  # noqa: BLE001  未知异常：不误标死，降级 WARN
            _report_health(reporter, source, HEALTH_WARN, "自检异常")
            return
        _report_health(
            reporter, source,
            HEALTH_OK if ok else HEALTH_WARN,
            "" if ok else "自检软失败",
        )

    Thread(target=_run, daemon=True).start()
