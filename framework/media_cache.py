"""视频播放磁盘缓存（media_cache.py）。

文件式磁盘缓存：播放过的 mp4 / HLS 落盘到 data/cache/video/，重播最近约 N 部
（默认 3 部）本地秒开；影片粒度 LRU + 字节配额兜底，防缓存爆炸；原子写
（.part / 临时文件 + rename）保证不产生半截文件，进程退出自动落索引。

目录结构：:
    data/cache/video/
        index.json           索引 {key: {path, size, last_access, complete, kind}}
        <key>.mp4            mp4 单文件（完整写毕才置 complete，才可本地 serve）
        <key>/               HLS 一部影片一个子目录
            playlist.m3u8    过滤后的 m3u8（广告过滤结果确定性缓存）
            <segment>        各分片（经 /c/<key>/ 路由逐片落盘）

key = sha1(去时效签名参数后的最终 URL)：源 URL 带 token/expires 签名时效时，
换 token 重播缓存 key 不变 → 直接命中本地缓存。

用法：:
    from framework.media_cache import MediaCache
    cache = MediaCache.instance()          # 单例（读 app_config.json 配额）
    key = cache.key_of(url)                # 去签名参数的缓存 key
    cache.mark_mp4(key, size, complete=True)
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    from urllib.parse import unquote_to_bytes  # noqa: F401 —— 预留给安全处理
except ImportError:  # pragma: no cover
    pass

_INDEX_NAME = "index.json"


class _Inflight:
    __slots__ = ("event", "done", "cancel", "error", "status", "headers", "response")

    def __init__(self):
        self.event = threading.Event()
        self.done = threading.Event()
        self.cancel = threading.Event()
        self.error = None
        self.status = None
        self.headers = {}
        self.response = None


# 带签名时效的 URL 参数：参与缓存 key 时必须剔除，否则换 token 重播 key 整体
# 失效（缓存越积越多且永远命中不了）。只砍明确的时效/签名参数名，不动其余参数。
_SIGNED_PARAM_NAMES = frozenset([
    "token", "access_token", "auth_token", "expires", "expires_at",
    "expires_in", "expire", "exp", "sign", "signature", "sig",
    "auth_key", "authkey", "policy", "x-amz-credential", "x-amz-signature",
    "x-amz-security-token", "x-amz-date", "x-oss-expires", "x-oss-signature",
])


def _strip_signed_params(url: str) -> str:
    """去掉 URL 中带签名时效的参数（缓存 key 用稳定版本）。"""
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query)
            if k.lower() not in _SIGNED_PARAM_NAMES]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(kept), parts.fragment))


def key_of(url: str) -> str:
    """缓存 key：sha1(去时效参数后的最终 URL)。"""
    return hashlib.sha1(_strip_signed_params(url).encode("utf-8")).hexdigest()


def _base_dir() -> Path:
    """应用根目录：与 gui/app.py 一致（frozen → exe 目录；开发 → 项目根）。"""
    import sys

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


class MediaCache:
    """文件式视频磁盘缓存（影片粒度 LRU + 字节配额兜底）。"""

    _instance: "MediaCache | None" = None

    def __init__(self, root: str | Path | None = None, *,
                 max_videos: int | None = None,
                 max_bytes: int | None = None,
                 enabled: bool = True):
        if root is not None:
            self.root = Path(root).resolve()
        else:
            self.root = _base_dir() / "data" / "cache" / "video"
        if max_videos is None:
            max_videos = 5
        if max_bytes is None:
            max_bytes = 8 * 1024 ** 3
        self.max_videos = int(max_videos)
        self.max_bytes = int(max_bytes)
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        self._index: dict[str, dict] = {}
        self._inflight: set[str] = set()       # 正在写入的 key（淘汰时跳过）
        self._fetches: dict[str, _Inflight] = {}
        self._path_locks: dict[str, threading.Lock] = {}
        self._path_locks_guard = threading.Lock()
        self._last_flush = 0.0
        atexit.register(self.flush)
        self._startup_reclaim()

    # ------------------------------------------------------------------ #
    @classmethod
    def instance(cls) -> "MediaCache":
        if cls._instance is None:
            cls._instance = cls(**_settings_defaults())
        return cls._instance

    @staticmethod
    def key_of(url: str) -> str:
        return key_of(url)

    # ------------------------------------------------------------------ #
    # 路径助手
    # ------------------------------------------------------------------ #
    def mp4_final(self, key: str) -> Path:
        return self.root / f"{key}.mp4"

    def mp4_part(self, key: str) -> Path:
        return self.root / f"{key}.mp4.part"

    def hls_dir(self, key: str) -> Path:
        return self.root / key

    def playlist_file(self, key: str) -> Path:
        return self.hls_dir(key) / "playlist.m3u8"

    @staticmethod
    def _safe_seg_name(relpath: str) -> str:
        """分片/资源相对路径 → 安全文件名（去路径穿越/分隔符/query）。"""
        name = relpath.replace("\\", "/")
        name = name.rsplit("?", 1)[0]
        name = name.rsplit("/", 1)[-1]
        name = re.sub(r"[^A-Za-z0-9._~-]", "_", name)
        name = name.strip("._")
        return name or "seg"

    def hls_segment_name(self, relpath: str) -> str:
        return self._safe_seg_name(relpath)

    def hls_segment_final(self, key: str, relpath: str) -> Path:
        return self.hls_dir(key) / self._safe_seg_name(relpath)

    def hls_segment_part(self, key: str, relpath: str) -> Path:
        return self.hls_dir(key) / (self._safe_seg_name(relpath) + ".part")

    # ------------------------------------------------------------------ #
    # 索引
    # ------------------------------------------------------------------ #
    def entry(self, key: str) -> dict | None:
        with self._lock:
            e = self._index.get(key)
            return dict(e) if e is not None else None

    def index_snapshot(self) -> dict:
        with self._lock:
            return {k: dict(e) for k, e in self._index.items()}

    def is_complete(self, key: str) -> bool:
        e = self.entry(key)
        return bool(e and e.get("complete"))

    def kind(self, key: str) -> str | None:
        e = self.entry(key)
        return e.get("kind") if e else None

    def touch(self, key: str) -> None:
        """播放/命中时刷新 last_access（LRU 依据）。"""
        with self._lock:
            e = self._index.get(key)
            if e is None:
                return
            e["last_access"] = time.time()
            self._maybe_flush()
            self._prune_locked()

    def mark_mp4(self, key: str, size: int, complete: bool) -> None:
        with self._lock:
            self._index[key] = {
                "path": str(self.mp4_final(key)),
                "size": int(size),
                "last_access": time.time(),
                "complete": bool(complete),
                "kind": "mp4",
            }
            self._maybe_flush()
            self._prune_locked()

    def mark_hls(self, key: str, playlist_size: int) -> None:
        """建立/刷新 HLS 影片条目（playlist 缓存成功）。"""
        with self._lock:
            e = self._index.get(key)
            if e is None:
                self._index[key] = {
                    "path": str(self.hls_dir(key)),
                    "size": int(playlist_size),
                    "last_access": time.time(),
                    "complete": True,
                    "kind": "hls",
                }
            else:
                e.setdefault("size", 0)
                e["size"] += int(playlist_size)
                e["last_access"] = time.time()
                e["complete"] = True
            self._maybe_flush()
            self._prune_locked()

    def mark_hls_segment(self, key: str, size: int) -> None:
        """HLS 分片落盘成功后累加影片字节数。"""
        if key not in self._safe_keys():
            return
        with self._lock:
            e = self._index.get(key)
            if e is None:
                return
            e["size"] = int(e.get("size", 0)) + int(size)
            e["last_access"] = time.time()
            self._maybe_flush()
            self._prune_locked()

    def _safe_keys(self) -> set:
        return {k for k in self._index if re.fullmatch(r"[0-9a-f]{40}", k)}

    # ------------------------------------------------------------------ #
    # 原子写
    # ------------------------------------------------------------------ #
    def playlist_write(self, key: str, data: bytes) -> bool:
        """原子写过滤后的 m3u8（tmp + rename），写毕才置 HLS 条目。"""
        try:
            p = self.playlist_file(key)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".m3u8.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, p)
            self.mark_hls(key, len(data))
            return True
        except OSError:
            try:
                p = self.playlist_file(key).with_suffix(".m3u8.tmp")
                if p.exists():
                    p.unlink()
            except OSError:
                pass
            return False

    @staticmethod
    def commit_part(part: Path, final: Path) -> bool:
        """.part → 正式文件原子改名；失败丢弃 .part 返回 False。"""
        try:
            os.replace(part, final)
            return True
        except OSError:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    @staticmethod
    def discard_part(part: Path) -> None:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # 并发防护
    # ------------------------------------------------------------------ #
    def fetch_state(self, key: str):
        with self._lock:
            return self._fetches.get(key)

    def start_fetch(self, key: str):
        with self._lock:
            state = self._fetches.get(key)
            if state is not None:
                return False, state
            state = _Inflight()
            self._fetches[key] = state
            return True, state

    def wait_fetch(self, state, timeout: float) -> bool:
        return state.event.wait(max(0.0, timeout))

    def set_fetch_response(self, key: str, state, response) -> None:
        with self._lock:
            if self._fetches.get(key) is not state:
                close = True
            else:
                state.response = response
                close = state.cancel.is_set()
        if close:
            try:
                response.close()
            except Exception:
                pass

    def fetch_cancelled(self, state) -> bool:
        return state.cancel.is_set()

    def finish_fetch(self, key: str, state, status: int | None = None,
                     error: Exception | None = None, headers: dict | None = None) -> None:
        with self._lock:
            if self._fetches.get(key) is not state:
                return
            self._fetches.pop(key, None)
            if state.cancel.is_set():
                state.status = state.status or 503
                state.error = state.error or RuntimeError("media fetch cancelled")
            else:
                state.status = status
                state.error = error
                state.headers = dict(headers or {})
            state.event.set()
            state.done.set()

    def cancel_fetches(self, timeout: float = 1.0) -> None:
        with self._lock:
            states = list(self._fetches.items())
            responses = []
            for _key, state in states:
                state.cancel.set()
                state.status = 503
                state.error = RuntimeError("media fetch cancelled")
                state.event.set()
                if state.response is not None:
                    responses.append(state.response)
        for response in responses:
            try:
                response.close()
            except Exception:
                pass
        deadline = time.monotonic() + max(0.0, timeout)
        for _key, state in states:
            state.done.wait(max(0.0, deadline - time.monotonic()))
        with self._lock:
            for key, state in states:
                if self._fetches.get(key) is state:
                    self._fetches.pop(key, None)
                    state.done.set()

    def inflight_add(self, key: str) -> None:
        with self._lock:
            self._inflight.add(key)

    def inflight_remove(self, key: str) -> None:
        with self._lock:
            self._inflight.discard(key)

    def path_lock(self, name: str) -> threading.Lock:
        """按分片名串行化写入（防止同分片并发双写 .part 交错）。"""
        with self._path_locks_guard:
            lock = self._path_locks.get(name)
            if lock is None:
                lock = threading.Lock()
                self._path_locks[name] = lock
            return lock

    # ------------------------------------------------------------------ #
    # 启动回收 + 淘汰
    # ------------------------------------------------------------------ #
    def _startup_reclaim(self) -> None:
        """App 启动回收：建目录、载索引、清遗留 .part、剔除失效条目、LRU。"""
        with self._lock:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            self._load_index()
            for p in list(self.root.glob("*.part")):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
            for d in list(self.root.iterdir()):
                if d.is_dir():
                    for p in list(d.glob("*.part")):
                        try:
                            p.unlink(missing_ok=True)
                        except OSError:
                            pass
            self._drop_missing()
            self._prune_locked()
            self._write_index()

    def _load_index(self) -> None:
        idx = self.root / _INDEX_NAME
        raw: dict = {}
        if idx.exists():
            try:
                data = json.loads(idx.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    raw = data
            except (json.JSONDecodeError, OSError):
                raw = {}
        for k, v in raw.items():
            if isinstance(v, dict) and re.fullmatch(r"[0-9a-f]{40}", str(k)):
                e = {
                    "path": str(v.get("path", "")),
                    "size": int(v.get("size", 0) or 0),
                    "last_access": float(v.get("last_access", 0.0) or 0.0),
                    "complete": bool(v.get("complete", False)),
                    "kind": str(v.get("kind", "mp4")),
                }
                if e["kind"] not in ("mp4", "hls"):
                    e["kind"] = "mp4"
                self._index[str(k)] = e

    def _drop_missing(self) -> None:
        for k in [k for k, e in self._index.items()]:
            p = Path(e["path"]) if e and e.get("path") else None
            if p is None or not p.exists():
                self._index.pop(k, None)

    def prune(self) -> None:
        with self._lock:
            self._prune_locked()

    def _prune_locked(self) -> None:
        """影片粒度 LRU：只留最近 N=3 部（可配）+ 字节配额兜底（如 2GB）。"""
        if not self.enabled:
            return
        complete = {k: e for k, e in self._index.items()
                    if e.get("complete") and k not in self._inflight}
        ordered = sorted(complete, key=lambda k: complete[k].get("last_access", 0.0))
        total = sum(e.get("size", 0) for e in complete.values())
        remove: list[str] = []
        # 1) 部数约束：保留最近 max_videos 部
        while len(ordered) - len(remove) > self.max_videos and ordered:
            remove.append(ordered.pop(0))
        # 2) 字节配额兜底
        while total > self.max_bytes and ordered:
            k = ordered.pop(0)
            total -= complete[k].get("size", 0)
            if k not in remove:
                remove.append(k)
        for k in remove:
            self._remove_entry(k)

    def _remove_entry(self, key: str) -> None:
        e = self._index.pop(key, None)
        if not e:
            return
        p = Path(e["path"])
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # 索引落盘
    # ------------------------------------------------------------------ #
    def flush(self) -> None:
        with self._lock:
            self._write_index()

    def _maybe_flush(self) -> None:
        now = time.time()
        if now - self._last_flush >= 2.0:
            self._last_flush = now
            self._write_index()

    def _write_index(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.root / f"{_INDEX_NAME}.tmp"
            tmp.write_text(
                json.dumps(self._index, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.root / _INDEX_NAME)
        except OSError:
            pass


def _settings_defaults() -> dict:
    """从 app_config.json 的 video_cache 段读取配额；读取失败用默认值。

    默认 8GB / 5 集：实测长片可达 1.4GB（1060 个 HLS 分片），2GB 配额装不下
    单集，写到一半就被 LRU 淘汰 → 边写边淘汰抖动 + 重播缓存不中。
    """
    try:
        from .settings_manager import SettingsManager

        sm = SettingsManager(_base_dir() / "app_config.json")
        sec = sm.get_section("video_cache") or {}
        return {
            "enabled": bool(sec.get("enabled", True)),
            "max_videos": int(sec.get("max_videos", 5) or 5),
            "max_bytes": int(sec.get("max_bytes_mb", 8192) or 8192) * 1024 * 1024,
        }
    except Exception:  # noqa: BLE001 —— 配置缺失/损坏用默认
        return {"enabled": True, "max_videos": 5, "max_bytes": 8 * 1024 ** 3}