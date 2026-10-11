"""全局应用设置（settings_manager.py）。

读写 app_config.json，提供默认值合并。首页读主题、字体缩放等。
对应 app_config.json 六块：network / ui / download / library / diagnostics
/ video_cache（视频播放磁盘缓存配额）。

用法：
    sm = SettingsManager("app_config.json")
    theme = sm.get("ui", "theme")          # -> "sakura"
    sm.set("ui", "theme", "midnight")      # 实时生效
    sm.save()
"""

from __future__ import annotations

import copy
import json
import logging
import threading
from pathlib import Path
from typing import Any

from .atomic_io import atomic_write_json

log = logging.getLogger(__name__)

DEFAULTS: dict = {
    "network": {
        "default_user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "default_timeout": 10,
        "default_retries": 3,
        "default_request_interval": 0.0,
        "proxy": None,
        "concurrent_search_sources": 4,
        "max_bytes_per_image": 5242880,
        "impersonate": "chrome",
        "user_agents": [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ],
        # 源链接批量可用性检查（check_links）并发：None = 不限制，按链接数
        # 自适应（3..8）；源 constraints 显式配置优先于本值。
        "check_links_threads": None,
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
    "video_cache": {
        "enabled": True,
        "max_videos": 5,
        "max_bytes_mb": 8192,
    },
    "media": {
        "source_probe": {
            "enabled": True,
            "max_sources": 5,
            "concurrency": 1,
            "timeout_ms": 30000,
        },
    },

    "hls_prefetch": {
        # 有界 HLS 预取：按消费位置前瞻 depth 片、并发 workers 路拉取落盘。
        # 实测部分 CDN 按连接限速，4 并发总带宽可达单连接的 2.1x。
        # 默认**关闭**：开启会改变对源站的请求模式，需实测确认不触发风控后再手动开。
        "enabled": False,
        "depth": 4,
        "workers": 3,
    },
    "content": {
        # 18+ 源显示策略：true = 在发现/搜索/源管理中显示成人源。
        "show_adult_sources": True,
    },
    "reader": {
        "prefetch_enabled": True,
        "prefetch_ahead": 1,
        "prefetch_behind": 0,
        "show_images": True,
    },
    "discover": {
        "preload_pages": 1,
        "preload_concurrency": 2,
    },
    "shelf_cache": {
        "max_book_mb": 256,
        "max_total_mb": 8192,
    },
    "sources_runtime": {

        "broken_source_warn_interval_hours": 24,
        "auto_disable_after_failures": 3,
        "selfcheck_strategy": "soft",
        # 播放死线路黑名单 TTL（秒）：probe 失败的子线路惰性过期时长，
        # 站修复后超时自动恢复尝试；有效范围 30..3600，越界钳制。
        "dead_line_ttl_seconds": 600,
        # 播放 URL 可达性探测超时上限（秒）：probe 只做可达性判断，
        # 不必等满源配置超时；有效范围 1..30，越界钳制。
        "play_url_probe_timeout_cap": 5,
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
        with self._lock:
            value = self._data.get(section, {}).get(key, default)
        # 内层 list/dict 返回副本，避免调用方原地修改泄漏到内部数据；标量/None 直接返回。
        if isinstance(value, (list, dict)):
            return copy.deepcopy(value)
        return value

    def set(self, section: str, key: str, value: Any) -> None:
        self._data.setdefault(section, {})[key] = value

    def get_section(self, section: str) -> dict:
        return dict(self._data.get(section, {}))

    def as_dict(self) -> dict:
        return json.loads(json.dumps(self._data))

    def save(self) -> None:
        try:
            with self._lock:
                # 锁内序列化 + 原子替换：并发 save 不会半写覆盖。
                atomic_write_json(self.path, self._data)
        except (OSError, TypeError, ValueError) as exc:
            # OSError：磁盘/权限问题；TypeError/ValueError：值不可 JSON 序列化。
            log.warning("保存配置失败：%s", exc)

    def reset_to_defaults(self) -> None:
        with self._lock:
            self._data = json.loads(json.dumps(DEFAULTS))
        self.save()


def _clamp_int(value, lo: int, hi: int, fallback: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(lo, min(hi, n))


def media_source_probe_settings(settings_manager) -> dict:
    probe = settings_manager.get("media", "source_probe", {})
    if not isinstance(probe, dict):
        probe = {}
    return {
        "enabled": bool(probe.get("enabled", True)),
        "max_sources": _clamp_int(probe.get("max_sources", 5), 1, 5, 5),
        "concurrency": 1,
        "timeout_ms": _clamp_int(
            probe.get("timeout_ms", 30000), 1000, 2_147_483_647, 30000
        ),
    }


def reader_prefetch_settings(settings_manager) -> dict:
    g = settings_manager.get
    return {
        "enabled": bool(g("reader", "prefetch_enabled", True)),
        "ahead": _clamp_int(g("reader", "prefetch_ahead", 1), 0, 1, 1),
        "behind": 0,
    }


def discover_preload_settings(settings_manager) -> dict:
    g = settings_manager.get
    return {
        "pages": _clamp_int(g("discover", "preload_pages", 1), 0, 2, 1),
        "concurrency": _clamp_int(g("discover", "preload_concurrency", 2), 1, 2, 2),
    }


def shelf_cache_settings(settings_manager) -> dict:
    g = settings_manager.get
    return {
        "max_book_mb": _clamp_int(g("shelf_cache", "max_book_mb", 256), 16, 65536, 256),
        "max_total_mb": _clamp_int(g("shelf_cache", "max_total_mb", 8192), 256, 131072, 8192),
    }
