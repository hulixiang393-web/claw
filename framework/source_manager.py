"""源加载管理（source_manager.py）。

职责：
- 扫描 sources/ 目录加载所有源配置；
- 隔离坏文件（跳过并记录告警，不中断）；
- 首页统计：按类型分组、启用数、失效数、健康状态历史。

对应 PROJECT_GUIDE.md §4.2、§6.2 的源加载和首页统计。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .atomic_io import atomic_write_json, read_text_lenient
from .config import CONTENT_TYPES, SourceConfig, load_source
from .errors import ConfigError, SourceNotFoundError

log = logging.getLogger(__name__)

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
        runtime_file: str | Path | None = None,
    ):
        """runtime_settings：可选，读 sources_runtime.* 策略（自动禁用阈值/告警间隔）。
        runtime_file：自动禁用集持久化路径；缺省与 health_file 同目录的
        sources_runtime.json（health_file 为空则不持久化）。"""
        self._sources: Dict[str, SourceConfig] = {}
        self._health: Dict[str, SourceHealth] = {}
        self._warnings: List[str] = []
        self._health_file = Path(health_file) if health_file else None
        self._runtime = runtime_settings or {}
        self._cookie_provider = None  # 可选: load(source_id) -> cookie_header 字符串
        self._auto_disabled: set = set()  # 因连续失败被自动禁用的 source_id（恢复路径专用）
        self._soft_deleted: set = set()  # 软删除了的 source_id（配置保留，列表/发现均不出现）
        self._adult_visible: bool = True  # 18+ 源是否显示（设置 content.show_adult_sources）
        self._bus = None
        self._cache_invalidator = None  # 可选：自动禁用后清理整源缓存（Content 注册）
        if runtime_file is not None:
            self._auto_disabled_file = Path(runtime_file)
        elif health_file is not None:
            # 默认与 health.json 同目录：重启后恢复自动禁用状态，
            # 避免「重启→重试→失败→再禁用」在每次启动时重复循环。
            self._auto_disabled_file = Path(health_file).parent / "sources_runtime.json"
        else:
            self._auto_disabled_file = None
        if self._health_file is not None:
            self._load_health()
        self._load_auto_disabled()
        if sources_dir is not None:
            self.load_dir(sources_dir)
        self._apply_auto_disabled()

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
            if config.raw.get("$deleted"):
                # 软删除过的源：配置保留但不出现在列表（重启后仍保持隐藏）
                self._soft_deleted.add(config.source_id)
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
        return self._visible(self._sources.values())

    def set_adult_visible(self, visible: bool) -> None:
        """设置 18+ 源是否可见。列表出口统一过滤，避免各页面各写一遍。"""
        visible = bool(visible)
        if visible == self._adult_visible:
            return
        self._adult_visible = visible
        self._emit_visibility_changed()

    def is_adult_visible(self) -> bool:
        return self._adult_visible

    def is_adult(self, source_id: str) -> bool:
        """只读元数据判断，不过滤列表。未知 source_id 视为非成人。"""
        cfg = self._sources.get(source_id)
        return bool(getattr(cfg, "adult", False)) if cfg is not None else False

    def _visible(self, items) -> List[SourceConfig]:
        if self._adult_visible:
            return list(items)
        return [s for s in items if not getattr(s, "adult", False)]

    def _emit_visibility_changed(self) -> None:
        """通知订阅者刷新源列表；无 EventBus 时静默返回。

        EventBus.emit 是单参接口（framework/events.py:68）。
        """
        bus = self._bus
        if bus is None:
            return
        try:
            from framework.events import Event, EVENT_SOURCE_VISIBILITY_CHANGED
            bus.emit(Event(EVENT_SOURCE_VISIBILITY_CHANGED, {
                "visible": self._adult_visible,
            }))
        except Exception:
            log.warning("[source] 源可见性事件广播失败", exc_info=True)

    def set_event_bus(self, bus) -> None:
        """注入 EventBus（App 层在 source_manager 建好后调用一次）。"""
        self._bus = bus

    def set_cache_invalidator(self, invalidator) -> None:
        """注入整源缓存失效回调（Content.__init__ 自动注册）。

        源健康状态迁入 broken 时（含随后触发的连续失败自动禁用）调用
        invalidator(source_id)，清掉该源详情/正文/播放流的旧缓存；手动
        禁用/软删除不触发。回调失败不影响禁用本身。
        """
        self._cache_invalidator = invalidator

    def soft_deleted_ids(self) -> List[str]:
        return list(self._soft_deleted)

    def is_soft_deleted(self, source_id: str) -> bool:
        return source_id in self._soft_deleted

    def soft_delete(self, source_id: str) -> None:
        """软删除：$deleted=true + 禁用，持久化到源 JSON，配置保留可恢复。"""
        source = self.get(source_id)
        source.raw["$deleted"] = True
        self._persist_enabled(source, False)
        source.enabled = False
        self._auto_disabled.discard(source_id)
        self._soft_deleted.add(source_id)
        self._sources.pop(source_id, None)
        self._save_auto_disabled()

    def restore(self, source_id: str) -> None:
        """恢复软删除的源：清 $deleted 并重新启用（源文件仍在 sources 目录）。"""
        path = Path(self._sources_dir) / f"{source_id}.json"
        try:
            cfg = load_source(path)
        except ConfigError as exc:
            raise SourceNotFoundError(f"恢复失败：{exc.message}", source_id=source_id) from exc
        cfg.raw["$deleted"] = False
        cfg.raw["$enabled"] = True
        cfg.enabled = True
        try:
            path.write_text(
                json.dumps(cfg.raw, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass
        self._soft_deleted.discard(source_id)
        self.add(cfg)

    def by_type(self, content_type: str) -> List[SourceConfig]:
        return self._visible(
            s for s in self._sources.values() if s.content_type == content_type
        )

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
        for s in self._visible(self._sources.values()):
            counts[s.content_type] += 1
        return counts

    def count_enabled(self) -> int:
        return len(self.enabled_sources())

    def count_broken(self) -> int:
        return sum(
            1
            for sid, health in self._health.items()
            if health.state == HEALTH_BROKEN and self._is_visible_source_id(sid)
        )

    def list_broken(self) -> List[dict]:
        """不可用源列表：{source_id, name, content_type, error}（供首页/管理页展示）。"""
        out = []
        for sid, health in self._health.items():
            if health.state != HEALTH_BROKEN or not self._is_visible_source_id(sid):
                continue
            src = self._sources.get(sid)
            out.append({
                "source_id": sid,
                "name": src.source_name if src else sid,
                "content_type": src.content_type if src else "",
                "error": health.last_error or "结构变更/不可用",
            })
        return out

    def _is_visible_source_id(self, source_id: str) -> bool:
        source = self._sources.get(source_id)
        return source is None or self._adult_visible or not source.adult

    # ------------------------------------------------------------------ #
    # 启停 / 权重（源管理用）
    # ------------------------------------------------------------------ #
    def set_enabled(self, source_id: str, enabled: bool) -> None:
        source = self.get(source_id)
        source.enabled = enabled
        # 手动启停后由用户掌控，清除自动禁用标记（不再走自动恢复路径）
        self._auto_disabled.discard(source_id)
        self._save_auto_disabled()

    def set_enabled_persist(self, source_id: str, enabled: bool) -> None:
        """启停并持久化 $enabled 到源 JSON（供删除/禁用等需要重启后保留的场景）。"""
        self.set_enabled(source_id, enabled)
        source = self.get(source_id)
        self._persist_enabled(source, enabled)

    def set_weight(self, source_id: str, weight: float) -> None:
        source = self.get(source_id)
        source.weight = weight

    def enabled_sources(self) -> List[SourceConfig]:
        """当前启用的源（搜索页源范围、发现页源列表据此过滤）。"""
        return self._visible(s for s in self._sources.values() if s.enabled)

    def apply_enabled_selection(self, selected_ids) -> None:
        """按「源选择引导」勾选结果批量启停，并把 $enabled 持久化到各源 JSON。

        只启用勾选的源，其余禁用。内存态立即生效（发现/搜索页下一次
        refresh 即按 enabled 过滤），JSON 持久化保证重启后保留。
        """
        selected = set(selected_ids)
        for source in self._sources.values():
            enabled = source.source_id in selected
            source.enabled = enabled
            self._auto_disabled.discard(source.source_id)
            self._persist_enabled(source, enabled)
        self._save_auto_disabled()

    @staticmethod
    def _persist_enabled(source: SourceConfig, enabled: bool) -> None:
        """把源 JSON 的 $enabled 字段写回磁盘。

        失败不阻塞：内存态已生效，仅失去持久化（下次启动恢复原状态）。
        """
        path = source.source_path
        if not path:
            return
        source.raw["$enabled"] = enabled
        try:
            Path(path).write_text(
                json.dumps(source.raw, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # 健康状态
    # ------------------------------------------------------------------ #
    def get_health(self, source_id: str) -> SourceHealth:
        if source_id not in self._health:
            self._health[source_id] = SourceHealth()
        return self._health[source_id]

    def update_health(self, source_id: str, state: str, error: str = "") -> None:
        health = self.get_health(source_id)
        prev_state = health.state
        changed = health.state != state or (bool(error) and health.last_error != error)
        health.state = state
        if error:
            health.last_error = error
        health.history.append(state)
        if len(health.history) > MAX_HEALTH_HISTORY:
            health.history = health.history[-MAX_HEALTH_HISTORY:]
        self._apply_policy(source_id, health)
        # D1：源被判定 broken（状态由非 broken 迁入 broken）时，同步失效该源
        # 缓存，避免带毒缓存（结构变更/失效结果）在恢复前反复命中而持续失败。
        # 只在状态迁移时触发，避免每次 broken 上报都重复清理；清理失败不影响
        # 健康状态记录（安全，回调内部已兜底）。
        if state == HEALTH_BROKEN and prev_state != HEALTH_BROKEN:
            self._invalidate_source_cache(source_id)
        # 状态/错误未变化时跳过写盘：后台自检每次成功（ok, ""）若都全量写
        # health.json，会拖慢每一章/每一集的抓取。只在状态变化时落盘。
        if self._health_file is not None and changed:
            self._save_health()

    def _invalidate_source_cache(self, source_id: str) -> None:
        """调用注入的整源缓存失效回调；未接线/回调异常时静默不影响调用方。

        仅清该源前缀业务缓存（回调由 Content 实现），与源_cache 生命周期一致。
        """
        invalidator = self._cache_invalidator
        if invalidator is None:
            return
        try:
            invalidator(source_id)
        except Exception:  # noqa: BLE001 —— 清理失败不影响健康状态/禁用本身
            log.warning(
                "[source] 清理源缓存失败：%s", source_id, exc_info=True
            )

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
        # 恢复路径：仅「因连续失败自动禁用」的源在健康恢复后自动重新启用；
        # 用户手动禁用的源不自动打开（避免悄悄改变用户意图）。
        if health.state == HEALTH_OK:
            src = self._sources.get(source_id)
            if (
                src is not None
                and not src.enabled
                and source_id in self._auto_disabled
            ):
                src.enabled = True
                health.last_error = ""
                self._auto_disabled.discard(source_id)
                self._warnings.append(f"源 {source_id} 健康恢复，已自动重新启用")
                self._save_auto_disabled()
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
                self._auto_disabled.add(source_id)
                note = "连续失败自动禁用" if not health.last_error else f"连续失败自动禁用：{health.last_error}"
                health.last_error = note
                self._warnings.append(f"源 {source_id} 连续失败 {streak} 次，已自动禁用")
                self._save_auto_disabled()
                # 整源缓存失效：该源判定不可用，清掉其详情/正文/播放流旧缓存，
                # 恢复后重新抓取。只清该源前缀键，不碰共享/全局缓存。
                self._invalidate_source_cache(source_id)

    def _load_health(self) -> None:
        if not self._health_file.exists():
            return
        try:
            raw = json.loads(self._health_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            for sid, data in raw.items():
                if not isinstance(data, dict):
                    continue
                history = data.get("history", [])
                if not isinstance(history, list):
                    history = []
                state = data.get("state", HEALTH_OK)
                if state not in {HEALTH_OK, HEALTH_WARN, HEALTH_BROKEN}:
                    state = HEALTH_OK
                last_error = data.get("last_error", "")
                if not isinstance(last_error, str):
                    last_error = ""
                self._health[sid] = SourceHealth(
                    state=state,
                    last_error=last_error,
                    history=[
                        item for item in history
                        if isinstance(item, str)
                        and item in {HEALTH_OK, HEALTH_WARN, HEALTH_BROKEN}
                    ],
                )
        except (json.JSONDecodeError, OSError, UnicodeError, TypeError):
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

    # ------------------------------------------------------------------ #
    # 自动禁用集持久化（sources_runtime.json）
    # ------------------------------------------------------------------ #
    def _load_auto_disabled(self) -> None:
        """从 runtime 文件恢复自动禁用集（重启后保持禁用直至健康恢复）。"""
        path = self._auto_disabled_file
        if path is None:
            return
        text = read_text_lenient(path)
        if not text:
            return
        try:
            raw = json.loads(text)
        except (ValueError, TypeError):
            return
        if not isinstance(raw, dict):
            return
        values = raw.get("auto_disabled")
        if not isinstance(values, list):
            return
        self._auto_disabled = {
            str(s) for s in values if isinstance(s, (str, int)) and str(s)
        }

    def _save_auto_disabled(self) -> None:
        """原子写自动禁用集（atomic_io：临时文件 + os.replace）。失败静默。"""
        path = self._auto_disabled_file
        if path is None:
            return
        try:
            atomic_write_json(path, {"auto_disabled": sorted(self._auto_disabled)})
        except (OSError, TypeError, ValueError):
            pass  # 持久化失败不阻塞：内存态已生效

    def _apply_auto_disabled(self) -> None:
        """重启后应用自动禁用集：仍在集合中的源保持禁用（直至健康恢复/手动启用）。

        用户手动禁用的源（$enabled 显式为 false）从集合中移除并持久化——
        自动恢复路径不打开用户手动关闭的源（手动禁用永不自动打开）。
        """
        if not self._auto_disabled:
            return
        changed = False
        for sid in list(self._auto_disabled):
            src = self._sources.get(sid)
            if src is None:
                self._auto_disabled.discard(sid)
                changed = True
                continue
            if src.raw.get("$enabled") is False:
                self._auto_disabled.discard(sid)
                changed = True
                continue
            if src.enabled:
                src.enabled = False
        if changed:
            self._save_auto_disabled()

    def discoverable_sources(self) -> List[SourceConfig]:
        """配置了发现规则且已启用的源（发现界面只列这些）。"""
        return self._visible(
            s for s in self._sources.values() if s.has_discovery() and s.enabled
        )
