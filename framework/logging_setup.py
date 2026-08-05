"""日志初始化（logging_setup.py）。

按 app_config.json 中 diagnostics.* 配置根 logger：
- verbose_logging: True=DEBUG / False=INFO
- log_dir: 日志文件目录（重启后生效）

启动时调用一次 setup_logging(settings) 即可。
"""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from typing import Any


def setup_logging(settings: Any | None = None) -> None:
    """初始化根 logger，输出到 stderr + 日志文件。

    settings: SettingsManager 实例；None 用硬编码默认值。
    """
    log_level = logging.INFO
    log_dir = "logs"
    file_pattern = "framework-{date}.log"

    if settings is not None:
        if settings.get("diagnostics", "verbose_logging", False):
            log_level = logging.DEBUG
        log_dir = settings.get("diagnostics", "log_dir", "logs") or "logs"
        file_pattern = settings.get("diagnostics", "log_file_pattern", "framework-{date}.log") or file_pattern

    root = logging.getLogger()
    root.setLevel(log_level)

    # 避免重复添加 handler（如多次调用）
    if any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        return

    # stderr handler（始终有）
    sh = logging.StreamHandler()
    sh.setLevel(log_level)
    sh.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(sh)

    # 文件 handler（按日期，追加模式）
    try:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        today = date.today().isoformat()
        filename = file_pattern.replace("{date}", today)
        fh = logging.FileHandler(
            log_path / filename, mode="a", encoding="utf-8"
        )
        fh.setLevel(log_level)
        fh.setFormatter(logging.Formatter(
            "[%(asctime)s] %(levelname)s %(name)s: %(message)s"
        ))
        root.addHandler(fh)
    except OSError:
        pass  # 写日志文件失败不阻塞启动
