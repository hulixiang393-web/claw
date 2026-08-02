"""源加载管理（source_manager.py）。

职责：
- 扫描 sources/ 目录加载所有源配置；
- 隔离坏文件（跳过并记录告警，不中断）；
- 首页统计：按类型分组、启用数、失效数、健康状态历史。

对应 core.md §源加载模块 + ui-home.md 统计卡片。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .config import CONTENT_TYPES, SourceConfig, load_source
from .errors import ConfigError, SourceNotFoundError

HEALTH_OK = "ok"        # 绿
HEALTH_WARN = "warn"    # 黄
HEALTH_BROKEN = "broken"  # 红

# 健康状态历史最多保留 N 次
MAX_HEALTH_HISTORY = 10


@dataclass
class SourceHealth:
    """单个源的健康状态。"""

    state: str = HEALTH_OK          # ok / warn / broken
    last_error: str = ""            # 最近错误信息
    history: List[str] = field(default_factory=list)  # 最近 N 次状态


class SourceManager:
    def __init__(self, sources_dir: str | Path | None = None, health_file: str | Path | None = None):
        self._sources: Dict[str, SourceConfig] = {}
        self._health: Dict[str, SourceHealth] = {}
        self._warnings: List[str] = []
        self._health_file = Path(health_file) if health_file else None
        if self._health_file is not None:
            self._load_health()
        if sources_dir is not None:
            self.load_dir(sources_dir)

    # ------------------------------------------------------------------ #
    # 加载
    # ------------------------------------------------------------------ #
    def load_dir(self, sources_dir: str | Path) -> None:
        directory = Path(sources_dir)
        if not directory.is_dir():
            self._warnings.append(f"源目录不存在：{directory}")
            return
        for path in sorted(directory.glob("*.json")):
            try:
                config = load_source(path)
            except ConfigError as exc:
                self._warnings.append(f"跳过 {path.name}：{exc.message}")
                continue
            self.add(config)

    def add(self, config: SourceConfig) -> None:
        if config.source_id in self._sources:
            self._warnings.append(
                f"source_id 重复 {config.source_id!r}，已由 {config.source_path} 覆盖"
            )
        self._sources[config.source_id] = config
        if config.source_id not in self._health:
            self._health[config.source_id] = SourceHealth()

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def get(self, source_id: str) -> SourceConfig:
        try:
            return self._sources[source_id]
        except KeyError:
            raise SourceNotFoundError(f"未找到数据源：{source_id}", source_id=source_id)

    def all(self) -> List[SourceConfig]:
        return list(self._sources.values())

    def by_type(self, content_type: str) -> List[SourceConfig]:
        return [s for s in self._sources.values() if s.content_type == content_type]

    def types(self) -> set:
        return {s.content_type for s in self._sources.values()}

    def source_ids(self) -> List[str]:
        return list(self._sources.keys())

    def warnings(self) -> List[str]:
        return list(self._warnings)

    # ------------------------------------------------------------------ #
    # 统计（首页用）
    # ------------------------------------------------------------------ #
    def count_by_type(self) -> Dict[str, int]:
        """按类型统计源数（含禁用）。"""
        counts = {t: 0 for t in CONTENT_TYPES}
        for s in self._sources.values():
            counts[s.content_type] += 1
        return counts

    def count_enabled(self) -> int:
        return sum(1 for s in self._sources.values() if s.enabled)

    def count_broken(self) -> int:
        return sum(1 for h in self._health.values() if h.state == HEALTH_BROKEN)

    # ------------------------------------------------------------------ #
    # 启停 / 权重（源管理用）
    # ------------------------------------------------------------------ #
    def set_enabled(self, source_id: str, enabled: bool) -> None:
        source = self.get(source_id)
        source.enabled = enabled

    def set_weight(self, source_id: str, weight: float) -> None:
        source = self.get(source_id)
        source.weight = weight

    # ------------------------------------------------------------------ #
    # 健康状态
    # ------------------------------------------------------------------ #
    def get_health(self, source_id: str) -> SourceHealth:
        if source_id not in self._health:
            self._health[source_id] = SourceHealth()
        return self._health[source_id]

    def update_health(self, source_id: str, state: str, error: str = "") -> None:
        health = self.get_health(source_id)
        health.state = state
        if error:
            health.last_error = error
        health.history.append(state)
        if len(health.history) > MAX_HEALTH_HISTORY:
            health.history = health.history[-MAX_HEALTH_HISTORY:]
        if self._health_file is not None:
            self._save_health()

    def _load_health(self) -> None:
        if not self._health_file.exists():
            return
        try:
            raw = json.loads(self._health_file.read_text(encoding="utf-8"))
            for sid, data in raw.items():
                self._health[sid] = SourceHealth(
                    state=data.get("state", HEALTH_OK),
                    last_error=data.get("last_error", ""),
                    history=list(data.get("history", [])),
                )
        except (json.JSONDecodeError, OSError):
            self._health = {}

    def _save_health(self) -> None:
        try:
            data = {
                sid: {
                    "state": h.state,
                    "last_error": h.last_error,
                    "history": h.history,
                }
                for sid, h in self._health.items()
            }
            self._health_file.parent.mkdir(parents=True, exist_ok=True)
            self._health_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass  # 健康状态写失败不阻塞

    def discoverable_sources(self) -> List[SourceConfig]:
        """配置了发现规则的源（发现界面只列这些）。"""
        return [s for s in self._sources.values() if s.has_discovery()]
