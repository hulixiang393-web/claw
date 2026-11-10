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
        "hls": {
            # 主清单只下发**一个**变体（默认开）。
            #
            # 实测（tools/_probe_startup.py，本地合成上游 + 真 mpv）：ffmpeg/mpv
            # 打开带 3 个 #EXT-X-STREAM-INF 的主清单时，会把**每个**变体的清单
            # 都打开，并从每个变体各读头部若干片（探测编码参数）。于是：
            #   3 个变体 × 头部预取 + 播放器正在播的那一路 = 起播瞬间 3~4 路
            #   同时抢同一条出口带宽；慢 CDN（实测单连接 100~300KB/s、3000k
            #   变体要 375KB/s）上首帧直接晚好几秒，且每个变体各占一个磁盘
            #   缓存目录（max_videos=5 时一部片子吃掉 3 个槽）。
            # 折叠成单变体后 mpv 侧只剩 1 路：请求数与起播耗时都按变体数收敛。
            # 想恢复多画质探测（例如需要播放器自动降码率）就把它设成 false。
            "single_variant": True,
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
        "prefetch_ahead": 3,
        "prefetch_behind": 1,
        "show_images": True,
        "font_size": 0,
        "font_family": "system",
        "line_spacing": 1.8,
        "content_width": 960,
        "paragraph_indent": True,
    },
    "discover": {
        "preload_pages": 1,
        "preload_concurrency": 2,
    },
    "shelf_cache": {
        "max_book_mb": 256,
        "max_total_mb": 8192,
    },
    "history": {
        # 播放/阅读历史保留条数（影视+小说+漫画合计）。已加入书架的记录
        # 永久保留且不占名额；0 = 不限制（只按时间过期清理）。
        "max_items": 20,
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
        # 「检测播放源」严格模式：除清单+首个分片外，再用 App 的播放器（mpv）
        # 真解几帧，只有播放器能播才标「可播放」。每条线路多花 5~25s，默认关。
        "verify_player_probe": False,
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
        # 上限 12：聚合站（洛雪TV）一个作品能聚出 10+ 条采集源线路，
        # 上限 5 时「检测播放源」只能测前 5 条。真正并发仍是 1，逐条来。
        "max_sources": _clamp_int(probe.get("max_sources", 5), 1, 12, 5),
        "concurrency": 1,
        "timeout_ms": _clamp_int(
            probe.get("timeout_ms", 30000), 1000, 2_147_483_647, 30000
        ),
    }


def media_hls_settings(settings_manager) -> dict:
    """播放链路的 HLS 通用策略（全局，不属于单个源）。

    目前只有 ``single_variant``：主清单是否折叠成单变体下发。默认 True ——
    mpv/ffmpeg 会为每个变体都读头部若干片，多画质主清单等于开局把带宽按变体
    数均分（见 DEFAULTS 里的实测说明）。任何读取异常都回退默认 True。
    """
    hls = settings_manager.get("media", "hls", {}) if settings_manager else {}
    if not isinstance(hls, dict):
        hls = {}
    return {"single_variant": bool(hls.get("single_variant", True))}


def reader_prefetch_settings(settings_manager) -> dict:
    """阅读器章节预加载配置（设置页同款范围：向后 0–20、向前 0–10）。

    串行模型不变（同一时间只跑 1 个预取任务），这里只决定**预取多少章**。
    之前把 ahead 硬钳到 1、behind 硬钳到 0，设置页选 3/1 实际只预取下一章，
    向前缓存则完全没生效。
    """
    g = settings_manager.get
    return {
        "enabled": bool(g("reader", "prefetch_enabled", True)),
        "ahead": _clamp_int(g("reader", "prefetch_ahead", 3), 0, 20, 3),
        "behind": _clamp_int(g("reader", "prefetch_behind", 1), 0, 10, 1),
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


def history_max_items(settings_manager) -> int:
    """历史保留条数（0 = 不限制，按时间过期清理）。"""
    return _clamp_int(settings_manager.get("history", "max_items", 20), 0, 500, 20)
