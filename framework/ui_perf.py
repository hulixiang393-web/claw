"""轻量 UI 卡顿探针（默认关闭；开启后只记录日志，不改变任何行为）。

用途：定位「操作时瞬间卡一下」这类 **GUI 线程阻塞**（收藏 / 弹窗 / 界面切换 /
书架加载等）。核心是看门狗：独立线程定期检查 GUI 心跳，一旦主线程停滞超过阈值，
就抓取**主线程当时的 Python 调用栈**，直接指出卡在哪个函数。

开启方式（任一即可）：
1. 环境变量 ``CLAW_UI_PERF=1``
2. ``app_config.json`` → ``diagnostics.ui_perf_logging = true``
3. 哨兵文件 ``data/ui_perf.on``（相对程序目录或当前工作目录）

产物：``logs/ui_perf-YYYY-MM-DD.log``（同时打印到终端）
- ``STALL <ms>``：GUI 线程停滞，附主线程调用栈 + 线程数 + 进程 CPU%
- ``OP <name> <ms>``：被埋点操作耗时超过阈值时记录

阈值可用环境变量微调：``CLAW_UI_PERF_STALL_MS``（默认 150）、
``CLAW_UI_PERF_OP_MS``（默认 80）。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import date
from pathlib import Path

_logger: logging.Logger | None = None
_started = False
_enabled_flag: bool | None = None
_base_dir: Path | None = None
_keepalive: list = []

STALL_THRESHOLD_MS = int(os.environ.get("CLAW_UI_PERF_STALL_MS", "150"))
OP_THRESHOLD_MS = int(os.environ.get("CLAW_UI_PERF_OP_MS", "80"))
SAMPLE_MS = 50

_hb = {"t": 0.0}


def _marker_files(base_dir: Path | None) -> list[Path]:
    cands = []
    if base_dir is not None:
        cands.append(base_dir / "data" / "ui_perf.on")
    cands.append(Path("data") / "ui_perf.on")
    return cands


def should_enable(settings=None, base_dir=None) -> bool:
    """启用判定：环境变量 > 设置项 > 哨兵文件。纯函数，便于测试。"""
    if os.environ.get("CLAW_UI_PERF") == "1":
        return True
    if settings is not None:
        try:
            if settings.get("diagnostics", "ui_perf_logging", False):
                return True
        except Exception:  # noqa: BLE001
            pass
    bd = Path(base_dir) if base_dir is not None else _base_dir
    for path in _marker_files(bd):
        try:
            if path.is_file():
                return True
        except OSError:
            continue
    return False


def enabled() -> bool:
    """是否启用（安装后缓存结果；未安装时按环境变量判断）。"""
    global _enabled_flag
    if _enabled_flag is not None:
        return _enabled_flag
    return os.environ.get("CLAW_UI_PERF") == "1"


def set_enabled(value) -> None:
    """显式设置启用状态；传 ``None`` 恢复「按环境变量判断」。"""
    global _enabled_flag
    _enabled_flag = None if value is None else bool(value)


def _get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger
    lg = logging.getLogger("claw.ui_perf")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    if not lg.handlers:
        try:
            log_dir = Path(
                os.environ.get("CLAW_UI_PERF_DIR")
                or ((_base_dir / "logs") if _base_dir is not None else Path("logs"))
            )
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(
                log_dir / f"ui_perf-{date.today().isoformat()}.log",
                mode="a",
                encoding="utf-8",
            )
            fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            lg.addHandler(fh)
        except OSError:
            pass
        # 性能探针（stall/op 超时日志）只落文件，不刷终端：属于非必要的
        # 诊断噪音，避免淹没真正的报错（用户需求：只显示必要的报错）。
        # 必要时可临时开启 StreamHandler 调试，但默认保持安静。
    _logger = lg
    return lg


def mark_heartbeat() -> None:
    _hb["t"] = time.perf_counter()


def _process_cpu() -> float:
    try:
        t = os.times()
        return t.user + t.system
    except Exception:  # noqa: BLE001
        return 0.0


def _watchdog(main_ident: int) -> None:
    log = _get_logger()
    last_cpu = _process_cpu()
    last_cpu_t = time.perf_counter()
    while True:
        time.sleep(SAMPLE_MS / 1000.0)
        if (time.perf_counter() - _hb["t"]) * 1000.0 < STALL_THRESHOLD_MS:
            continue
        t0 = time.perf_counter()
        stacks: list[str] = []
        while True:
            frame = sys._current_frames().get(main_ident)
            if frame is not None:
                stacks.append("".join(traceback.format_stack(frame)))
            time.sleep(SAMPLE_MS / 1000.0)
            if (time.perf_counter() - _hb["t"]) * 1000.0 < SAMPLE_MS:
                break
        dur_ms = (time.perf_counter() - t0) * 1000.0
        rep = max(set(stacks), key=stacks.count) if stacks else "(no stack)"
        now = time.perf_counter()
        cpu = _process_cpu()
        cpu_pct = (
            (cpu - last_cpu) / (now - last_cpu_t) * 100.0 if now > last_cpu_t else 0.0
        )
        last_cpu, last_cpu_t = cpu, now
        log.info(
            "STALL %.0fms threads=%d cpu=%.0f%%\n%s",
            dur_ms,
            threading.active_count(),
            cpu_pct,
            rep,
        )


def install(settings=None, base_dir=None) -> bool:
    """由 GUI 层在 MainWindow 初始化时调用一次。默认关闭，返回是否启用。"""
    global _started, _base_dir, _enabled_flag
    if base_dir is not None:
        _base_dir = Path(base_dir)
    choose = should_enable(settings, base_dir)
    _enabled_flag = choose
    if not choose or _started:
        return False
    _started = True
    log = _get_logger()
    log.info(
        "=== ui_perf started (stall>%dms, op>%dms) ===",
        STALL_THRESHOLD_MS,
        OP_THRESHOLD_MS,
    )
    mark_heartbeat()
    heartbeat_ok = False
    try:
        from PySide6.QtCore import QTimer, Qt
        from PySide6.QtWidgets import QApplication

        if QApplication.instance() is not None:
            timer = QTimer()
            timer.setTimerType(Qt.TimerType.PreciseTimer)
            timer.setInterval(SAMPLE_MS)
            timer.timeout.connect(mark_heartbeat)
            timer.start()
            _keepalive.append(timer)
            heartbeat_ok = True
    except Exception:  # noqa: BLE001
        heartbeat_ok = False
    if not heartbeat_ok:
        log.info("ui_perf: 心跳定时器不可用，仅启用操作计时（不启动看门狗）")
        return True
    main_ident = threading.main_thread().ident
    if main_ident is not None:
        threading.Thread(
            target=_watchdog,
            args=(main_ident,),
            name="ui-perf-watchdog",
            daemon=True,
        ).start()
    return True


@contextmanager
def op(name: str):
    """对被埋点操作计时；超过阈值才记一行 OP 日志。默认关闭时零额外开销。"""
    if not enabled():
        yield
        return
    t = time.perf_counter()
    try:
        yield
    finally:
        dt = (time.perf_counter() - t) * 1000.0
        if dt >= OP_THRESHOLD_MS:
            try:
                _get_logger().info("OP %-24s %.0fms", name, dt)
            except Exception:  # noqa: BLE001
                pass
