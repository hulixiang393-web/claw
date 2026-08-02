"""源配置解析与校验（config.py）。

把 sources/*.json 解析为 SourceConfig 对象，做字段校验。
首页统计只需顶层字段（$id/$type/$name/$enabled/$weight/$metadata）；
完整 endpoints 解析后续业务模块实现时再扩展。

对应 source-schema-v2.md §1 顶层字段。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .errors import ConfigError

CONTENT_TYPES = ("novel", "comic", "video")


@dataclass
class SourceConfig:
    """一个数据源（对应一份 sources/*.json）。"""

    source_id: str
    source_name: str
    content_type: str
    enabled: bool = True
    weight: float = 1.0
    base_url: str = ""
    homepage: str = ""
    icon: str = ""
    description: str = ""
    tags: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)  # 原始 JSON，供后续完整解析
    source_path: str = ""

    @classmethod
    def from_dict(cls, data: Any, path: str = "<memory>") -> "SourceConfig":
        _require_dict(data, path)
        source_id = _require_str(data, "$id", path)
        content_type = str(data.get("$type") or "")
        if content_type not in CONTENT_TYPES:
            raise ConfigError(
                f"{path}.$type 只能是 novel/comic/video 之一，收到 {content_type!r}"
            )
        source_name = _require_str(data, "$name", path)

        transports = data.get("transports") or {}
        _require_dict(transports, f"{path}.transports")
        base_url = transports.get("base_url") or ""

        meta = data.get("$metadata") or {}
        if not isinstance(meta, dict):
            meta = {}

        return cls(
            source_id=source_id,
            source_name=source_name,
            content_type=content_type,
            enabled=bool(data.get("$enabled", True)),
            weight=float(data.get("$weight", 1.0)),
            base_url=str(base_url),
            homepage=str(meta.get("homepage") or ""),
            icon=str(meta.get("icon") or ""),
            description=str(meta.get("description") or ""),
            tags=list(meta.get("tags") or []),
            raw=data,
            source_path=path,
        )

    def to_dict(self) -> dict:
        """导出回 JSON dict（供编辑器/落盘）。"""
        meta = {
            "homepage": self.homepage,
            "icon": self.icon,
            "description": self.description,
            "tags": self.tags,
        }
        transports = dict(self.raw.get("transports") or {})
        transports["base_url"] = self.base_url
        out = dict(self.raw)
        out["$id"] = self.source_id
        out["$name"] = self.source_name
        out["$type"] = self.content_type
        out["$enabled"] = self.enabled
        out["$weight"] = self.weight
        out["$metadata"] = meta
        out["transports"] = transports
        return out

    # ---- 便捷访问 ------------------------------------------------------ #
    def has_discovery(self) -> bool:
        """是否配置了 HTML 发现规则（决定是否出现在发现界面）。

        仅认 endpoints.discovery（HTML selector）。api_endpoints（JSON API）
        待内核支持 JSONPath 解析后再接入发现界面。
        """
        endpoints = self.raw.get("endpoints") or {}
        return bool(endpoints.get("discovery"))

    def get_discovery_config(self) -> dict:
        """读取 endpoints.discovery（返回 {} 若未配置）。"""
        endpoints = self.raw.get("endpoints") or {}
        return endpoints.get("discovery") or {}

    def get_search_config(self) -> dict:
        """读取 endpoints.search（返回 {} 若未配置）。"""
        endpoints = self.raw.get("endpoints") or {}
        return endpoints.get("search") or {}

    def get_detail_config(self) -> dict:
        """读取 endpoints.detail（返回 {} 若未配置）。"""
        endpoints = self.raw.get("endpoints") or {}
        return endpoints.get("detail") or {}

    def transports(self) -> dict:
        """读取 transports 配置。"""
        return self.raw.get("transports") or {}


def load_source(path: str | Path) -> SourceConfig:
    """从磁盘加载一份源配置。坏 JSON → ConfigError。"""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path.name} 不是合法 JSON：{exc.msg}") from exc
    except OSError as exc:
        raise ConfigError(f"{path.name} 读取失败：{exc}") from exc
    return SourceConfig.from_dict(raw, path=str(path))


def _require_dict(data: Any, path: str) -> None:
    if not isinstance(data, dict):
        raise ConfigError(f"{path} 必须是对象（JSON 键值对）")


def _require_str(data: dict, key: str, path: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path}.{key} 缺失或不是非空字符串")
    return value
