# -*- coding: utf-8 -*-
"""pythonw 启动时 sys.stderr 为 None，日志初始化不能注册控制台 handler。"""
import logging
import sys

from framework.logging_setup import setup_logging


class _Settings:
    def get(self, section, key, default=None):
        if (section, key) == ("diagnostics", "log_dir"):
            return str(self.log_dir)
        return default


def test_pythonw_without_stderr_keeps_file_handler_only(tmp_path, monkeypatch):
    settings = _Settings()
    settings.log_dir = tmp_path
    root = logging.getLogger()
    saved = root.handlers[:]
    for handler in saved:
        root.removeHandler(handler)

    monkeypatch.setattr(sys, "stderr", None)
    try:
        setup_logging(settings)
        console = [
            h for h in root.handlers
            if type(h) is logging.StreamHandler
        ]
        files = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
        assert console == []
        assert len(files) == 1
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
            handler.close()
        for handler in saved:
            root.addHandler(handler)
