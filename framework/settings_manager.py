"""全局应用设置（settings_manager.py）。

读写 app_config.json，提供默认值合并。首页读主题、字体缩放等。
对应 app_config.json 五块：network / ui / download / library / diagnostics。

用法：
    sm = SettingsManager("app_config.json")
    theme = sm.get("ui", "theme")          # -> "sakura"
    sm.set("ui", "theme", "midnight")      # 实时生效
    sm.save()
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULTS: dict = {
    "network": {
        "default_user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 SpiderFramework/1.0"
        ),
        "default_timeout": 10,
        "default_retries": 3,
        "default_request_interval": 0.0,
        "proxy": None,
        "concurrent_search_sources": 4,
        "max_bytes_per_image": 5242880,
    },
    "ui": {
        "theme": "sakura",
        "font_scale": 1.0,
        "cover_cache_size_mb": 256,
        "default_page_size": 20,
        "background_image": "",
        "background_opacity": 0.6,
        # 阅读区独立设置（ui-reader #12）：留空 = 跟随全局主题
        "reading_bg": "",
        "reading_font_size": 0,
    },
    "download": {
        "output_dir": "downloads",
        "naming_template": "{title}_{chapter_no}_{chapter_title}",
        "merge_chapters_into_one_file": False,
        "skip_existing": True,
        "max_concurrent_downloads": 6,
        "show_notification": True,
    },
    "library": {
        "shelf_export_dir": "library",
        "webdav_url": None,
    },
    "diagnostics": {
        "verbose_logging": False,
        "log_dir": "logs",
        "log_file_pattern": "framework-{date}.jsonl",
    },
    "adblock": {
        "extra_rule_dir": "",
    },
    "sources_runtime": {
        "broken_source_warn_interval_hours": 24,
        "auto_disable_after_failures": 3,
        "selfcheck_strategy": "soft",
    },
}


class SettingsManager:
    def __init__(self, path: str | Path = "app_config.json"):
        self.path = Path(path)
        self._lock = threading.RLock()  # set/save 并发保护（下载线程 + UI 线程）
        self._data: dict = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return json.loads(json.dumps(DEFAULTS))  # 深拷贝默认值
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return json.loads(json.dumps(DEFAULTS))
        # 与默认值合并（缺失键用默认值）
        merged = json.loads(json.dumps(DEFAULTS))
        self._merge(merged, raw)
        return merged

    @staticmethod
    def _merge(base: dict, override: dict) -> None:
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                SettingsManager._merge(base[key], value)
            else:
                base[key] = value

    # ------------------------------------------------------------------ #
    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self._data.get(section, {}).get(key, default)

    def set(self, section: str, key: str, value: Any) -> None:
        self._data.setdefault(section, {})[key] = value

    def get_section(self, section: str) -> dict:
        return dict(self._data.get(section, {}))

    def as_dict(self) -> dict:
        return json.loads(json.dumps(self._data))

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with self._lock:
                tmp.write_text(
                    json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            tmp.replace(self.path)  # 原子替换：避免中途崩溃截断配置文件
        except OSError as exc:
            log.warning("保存配置失败：%s", exc)

    def reset_to_defaults(self) -> None:
        with self._lock:
            self._data = json.loads(json.dumps(DEFAULTS))
        self.save()
