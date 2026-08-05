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
from typing import Any, Dict, List, Optional

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
    def __init__(
        self,
        sources_dir: str | Path | None = None,
        health_file: str | Path | None = None,
        runtime_settings: Optional[dict] = None,
    ):
        """runtime_settings：可选，读 sources_runtime.* 策略（自动禁用阈值/告警间隔）。"""
        self._sources: Dict[str, SourceConfig] = {}
        self._health: Dict[str, SourceHealth] = {}
        self._warnings: List[str] = []
        self._health_file = Path(health_file) if health_file else None
        self._runtime = runtime_settings or {}
        self._last_warn_ts: Dict[str, float] = {}
        self._cookie_provider = None  # 可选: load(source_id) -> cookie_header 字符串
        if self._health_file is not None:
            self._load_health()
        if sources_dir is not None:
            self.load_dir(sources_dir)

    # ------------------------------------------------------------------ #
    # 加载
    # ------------------------------------------------------------------ #
    def load_dir(self, sources_dir: str | Path) -> None:
        directory = Path(sources_dir)
        self._sources_dir = directory
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
        config.cookie_provider = self._cookie_provider  # 注入登录 cookie 加载器
        if config.source_id not in self._health:
            self._health[config.source_id] = SourceHealth()

    def set_cookie_provider(self, provider) -> None:
        """注入 cookie 加载回调（load(source_id) -> cookie 头字符串），供请求合并。"""
        self._cookie_provider = provider
        for cfg in self._sources.values():
            cfg.cookie_provider = provider

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

    def list_broken(self) -> List[dict]:
        """不可用源列表：{source_id, name, content_type, error}（供首页/管理页展示）。"""
        out = []
        for sid, health in self._health.items():
            if health.state != HEALTH_BROKEN:
                continue
            src = self._sources.get(sid)
            out.append({
                "source_id": sid,
                "name": src.source_name if src else sid,
                "content_type": src.content_type if src else "",
                "error": health.last_error or "结构变更/不可用",
            })
        return out

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
        self._apply_policy(source_id, health)
        if self._health_file is not None:
            self._save_health()

    # ------------------------------------------------------------------ #
    # 自动禁用 / 告警策略（sources_runtime.*）
    # ------------------------------------------------------------------ #
    def _runtime_opt(self, section_key: str, default):
        """读 runtime_settings（可为 SettingsManager 或 dict）。

        命名避开实例属性 self._runtime（保存 runtime_settings 对象）。
        """
        from .settings_manager import SettingsManager

        runtime = self._runtime or {}
        if isinstance(runtime, SettingsManager):
            # SettingsManager.get(section, key, default)
            return runtime.get("sources_runtime", section_key, default)
        sec = runtime.get("sources_runtime") or {}
        return sec.get(section_key, default)

    def _apply_policy(self, source_id: str, health: SourceHealth) -> None:
        """连续 broken 达阈值 → 自动禁用；broken 转移时按间隔门控告警。

        健康恢复（ok）时，若源因连续失败被自动禁用，自动重新启用（状态机恢复路径）。
        """
        # 恢复路径：上次因自动禁用被关的源，健康恢复后重新启用
        if health.state == HEALTH_OK:
            src = self._sources.get(source_id)
            if src is not None and not src.enabled and health.last_error:
                src.enabled = True
                health.last_error = ""
                self._warnings.append(f"源 {source_id} 健康恢复，已自动重新启用")
            return
        if health.state != HEALTH_BROKEN:
            return
        # 连续 broken 次数 = history 尾部连续 broken 计数
        streak = 0
        for s in reversed(health.history):
            if s == HEALTH_BROKEN:
                streak += 1
            else:
                break
        threshold = int(self._runtime_opt("auto_disable_after_failures", 3) or 0)
        if threshold > 0 and streak >= threshold:
            src = self._sources.get(source_id)
            if src is not None and src.enabled:
                src.enabled = False
                note = "连续失败自动禁用" if not health.last_error else f"连续失败自动禁用：{health.last_error}"
                health.last_error = note
                self._warnings.append(f"源 {source_id} 连续失败 {streak} 次，已自动禁用")
        # 告警门控：间隔内不重复
        import time

        interval_h = float(self._runtime_opt("broken_source_warn_interval_hours", 24) or 0)
        if interval_h > 0:
            now = time.time()
            if now - self._last_warn_ts.get(source_id, 0.0) >= interval_h * 3600:
                self._last_warn_ts[source_id] = now

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
