from __future__ import annotations

import inspect
import threading
import time
from urllib.parse import parse_qs, urlsplit


class StreamCancelledError(TimeoutError):
    pass


class StreamBudget:
    def __init__(self, deadline=None):
        self.deadline = deadline
        self._cancelled = threading.Event()

    @property
    def cancelled(self):
        return self._cancelled.is_set() or (
            self.deadline is not None and time.monotonic() >= self.deadline
        )

    def remaining(self):
        if self.deadline is None:
            return float("inf")
        return max(0.0, self.deadline - time.monotonic())

    def check(self):
        if self._cancelled.is_set():
            raise StreamCancelledError("stream resolution cancelled")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.cancel()
            raise StreamCancelledError("stream resolution deadline exhausted")
        return self

    def cancel(self):
        self._cancelled.set()


StreamCancellation = StreamCancelledError


class StreamResolver:
    DEFAULT_TOTAL_BUDGET = 30.0
    DEFAULT_CANDIDATE_TIMEOUT = 10.0
    DEFAULT_MAX_CANDIDATES = 6
    DEFAULT_CACHE_TTL = 30.0
    SIGNED_QUERY_NAMES = frozenset({
        "auth",
        "authorization",
        "expire",
        "expires",
        "expires_at",
        "hmac",
        "key",
        "policy",
        "sig",
        "signature",
        "token",
        "x-amz-date",
        "x-amz-expires",
        "x-amz-signature",
        "x-expires",
        "x-signature",
    })

    def __init__(self, content, total_budget=DEFAULT_TOTAL_BUDGET,
                 candidate_timeout=DEFAULT_CANDIDATE_TIMEOUT,
                 max_candidates=DEFAULT_MAX_CANDIDATES,
                 cache_ttl=DEFAULT_CACHE_TTL,
                 signed_query_names=None):
        self._content = content
        self._total_budget = float(total_budget)
        self._candidate_timeout = float(candidate_timeout)
        self._max_candidates = int(max_candidates)
        # D4：TTL 读取失败（配置缺失/非法）回退内置默认值（安全默认），不崩溃。
        default_ttl = float(self.DEFAULT_CACHE_TTL)
        try:
            self._cache_ttl = max(0.0, float(cache_ttl))
        except (TypeError, ValueError):
            self._cache_ttl = max(0.0, default_ttl)
        # 源的额外签名查询参数（小写）：内置集 ∪ 源配置。用于判断某取流 URL
        # 是否带时效签名 —— 命中即视为不可缓存（ttl=0），避免缓存过期 URL。
        # 非法配置（不可迭代）时回退内置集（安全默认）。
        try:
            extra_names = frozenset(str(n).lower() for n in signed_query_names)
        except TypeError:
            extra_names = frozenset()
        self._signed_query_names = extra_names | frozenset(self.SIGNED_QUERY_NAMES)
        self._cache = {}
        self._inflight = {}
        self._lock = threading.RLock()

    def resolve(self, source, episode_url, quality="", merged=True, deadline=None, context=None,
                source_lock=False, cache_ttl=None, signed_query_names=None):
        if context is None:
            if deadline is None:
                deadline = time.monotonic() + self._total_budget
            context = StreamBudget(deadline)
        else:
            context.check()
            if context.deadline is None:
                context.deadline = time.monotonic() + self._total_budget
        key = (getattr(source, "source_id", ""), episode_url, quality, bool(merged))
        with self._lock:
            cached = self.get_cached(source, episode_url, quality, merged)
            if cached is not None:
                context.check()
                return cached
            pending = self._inflight.get(key)
            if pending is None:
                pending = {
                    "event": threading.Event(),
                    "active": 0,
                    "finished": False,
                    "context": context,
                }
                self._inflight[key] = pending
                owner = True
            else:
                owner = False
        if not owner:
            remaining = context.remaining()
            if remaining <= 0 or not pending["event"].wait(remaining):
                context.cancel()
                context.check()
            context.check()
            with self._lock:
                if pending.get("cancelled"):
                    raise StreamCancelledError("stream resolution cancelled")
                if "result" in pending:
                    return pending["result"]
            raise RuntimeError("video stream resolution failed at candidate stage") from None

        failed = False
        try:
            result = self._resolve_uncached(source, episode_url, quality, merged, context, key,
                                            source_lock)
            with self._lock:
                context.check()
                pending["result"] = result
                if result[0] or result[1]:
                    # 单次调用可覆盖缓存 TTL / 签名参数：写缓存按生效值判定；
                    # 命中读取仍按实例默认判定过不过期。
                    ttl = self._result_ttl(result, cache_ttl, signed_query_names)
                    if ttl > 0:
                        self._cache[key] = (time.monotonic() + ttl, result)
        except StreamCancelledError:
            with self._lock:
                pending["cancelled"] = True
            raise
        except TimeoutError:
            result = ("", "")
        except Exception:
            failed = True
        finally:
            with self._lock:
                pending["finished"] = True
                self._cleanup_inflight(key, pending)
        if failed:
            raise RuntimeError("video stream resolution failed at candidate stage") from None
        return result

    def get_cached(self, source, episode_url, quality="", merged=True):
        key = (getattr(source, "source_id", ""), episode_url, quality, bool(merged))
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                expires_at, result = cached
                if expires_at > time.monotonic() and self._result_ttl(result) > 0:
                    return result
                self._cache.pop(key, None)
        return None

    def invalidate(self, source, episode_url=None, quality=None, merged=None):
        source_id = getattr(source, "source_id", "")
        with self._lock:
            for key in self._cache.keys() | self._inflight.keys():
                if key[0] != source_id:
                    continue
                if episode_url is not None and key[1] != episode_url:
                    continue
                if quality is not None and key[2] != quality:
                    continue
                if merged is not None and key[3] != bool(merged):
                    continue
                self._cache.pop(key, None)
                pending = self._inflight.get(key)
                if pending is not None:
                    pending["cancelled"] = True
                    pending["context"].cancel()

    @classmethod
    def _is_signed_url(cls, value, signed_query_names=None):
        if not value:
            return False
        try:
            query = parse_qs(urlsplit(value).query)
        except Exception:
            return True
        if signed_query_names is None:
            return any(name.lower() in cls.SIGNED_QUERY_NAMES for name in query)
        lowered = frozenset(str(n).lower() for n in signed_query_names)
        return any(name.lower() in lowered for name in query)

    @classmethod
    def cache_ttl_for_result(cls, result, cache_ttl, signed_query_names=None):
        if any(cls._is_signed_url(value, signed_query_names) for value in result if value):
            return 0.0
        return max(0.0, float(cache_ttl))

    def _result_ttl(self, result, cache_ttl=None, signed_query_names=None):
        try:
            ttl = float(cache_ttl) if cache_ttl is not None else self._cache_ttl
        except (TypeError, ValueError):
            ttl = self._cache_ttl
        names = self._signed_query_names if signed_query_names is None else signed_query_names
        return self.cache_ttl_for_result(result, ttl, names)

    def _cleanup_inflight(self, key, pending):
        if pending["finished"] and pending["active"] == 0:
            if self._inflight.get(key) is pending:
                self._inflight.pop(key)
            pending["event"].set()

    def _start_worker(self, key, pending, target):
        with self._lock:
            pending["active"] += 1

        def run():
            try:
                target()
            finally:
                with self._lock:
                    pending["active"] -= 1
                    self._cleanup_inflight(key, pending)

        thread = threading.Thread(target=run, daemon=True)
        try:
            thread.start()
        except Exception:
            with self._lock:
                pending["active"] -= 1
                self._cleanup_inflight(key, pending)
            raise

    def _resolve_uncached(self, source, episode_url, quality, merged, context, key,
                          source_lock=False):
        context.check()
        direct = self._direct_candidates(episode_url)
        if direct:
            context.check()
            return self._normalize_candidate(direct[0])
        candidates = self._load_candidates(
            source, episode_url, quality, merged, context, key, source_lock
        )
        context.check()
        unique = []
        seen = set()
        for candidate in candidates:
            candidate_key = self._candidate_key(candidate)
            if candidate_key in seen:
                continue
            seen.add(candidate_key)
            unique.append(candidate)
            if len(unique) >= self._max_candidates:
                break
        for candidate in unique:
            context.check()
            pair = self._resolve_candidate(candidate, context, key)
            if pair is not None:
                context.check()
                return pair
        context.check()
        return "", ""

    @staticmethod
    def _wait_for_worker(done, timeout, context):
        deadline = time.monotonic() + timeout
        while True:
            context.check()
            remaining = min(deadline - time.monotonic(), context.remaining())
            if remaining <= 0:
                context.check()
                return done.is_set()
            if done.wait(min(0.05, remaining)):
                context.check()
                return True

    def _resolve_candidate(self, candidate, context, key):
        timeout = self._candidate_timeout
        if isinstance(candidate, dict) and callable(candidate.get("resolver")):
            if candidate.get("budget") == "pipeline":
                timeout = context.remaining()
            candidate = candidate["resolver"]
        if callable(candidate):
            remaining = min(timeout, context.remaining())
            if remaining <= 0:
                return None
            result = []
            error = []
            done = threading.Event()

            def run():
                try:
                    try:
                        parameters = inspect.signature(candidate).parameters
                        accepts_context = (
                            "context" in parameters
                            or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())
                        )
                    except (TypeError, ValueError):
                        accepts_context = False
                    value = candidate(context=context) if accepts_context else candidate()
                    result.append(value)
                except Exception as exc:
                    error.append(exc)
                finally:
                    done.set()

            self._start_worker(key, self._inflight[key], run)
            if not self._wait_for_worker(done, remaining, context):
                context.check()
                return None
            if error:
                if isinstance(error[0], StreamCancelledError):
                    raise error[0]
                return None
            context.check()
            candidate = result[0] if result else None
        return self._normalize_candidate(candidate)

    @staticmethod
    def _candidate_key(candidate):
        if isinstance(candidate, dict) and callable(candidate.get("resolver")):
            return ("callback", id(candidate["resolver"]), candidate.get("budget"))
        if callable(candidate):
            return ("callback", id(candidate))
        pair = StreamResolver._normalize_candidate(candidate)
        return ("value", pair)

    def _load_candidates(self, source, episode_url, quality, merged, context, key,
                         source_lock=False):
        remaining = min(self._candidate_timeout, context.remaining())
        if remaining <= 0:
            context.check()
        loader = getattr(self._content, "resolve_video_stream_candidates", None)
        if loader is None:
            loader = self._content._fetch_video_streams_legacy
        result = []
        done = threading.Event()
        error = []

        def run():
            try:
                try:
                    parameters = inspect.signature(loader).parameters
                    accepts_context = (
                        "context" in parameters
                        or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())
                    )
                    accepts_deadline = "deadline" in parameters
                    accepts_source_lock = (
                        "source_lock" in parameters
                        or any(p.kind == inspect.Parameter.VAR_KEYWORD
                               for p in parameters.values())
                    )
                except (TypeError, ValueError):
                    accepts_context = False
                    accepts_deadline = False
                kwargs = {}
                if accepts_context:
                    kwargs["context"] = context
                elif accepts_deadline:
                    kwargs["deadline"] = context.deadline
                if accepts_source_lock:
                    kwargs["source_lock"] = source_lock
                value = loader(source, episode_url, quality, merged, **kwargs)
                if isinstance(value, (str, tuple)):
                    value = [value]
                result.extend(value or [])
            except Exception as exc:
                error.append(exc)
            finally:
                done.set()

        self._start_worker(key, self._inflight[key], run)
        if not self._wait_for_worker(done, remaining, context):
            return []
        context.check()
        if error:
            raise error[0]
        return result

    @staticmethod
    def _normalize_candidate(candidate):
        if isinstance(candidate, str):
            return candidate, ""
        if isinstance(candidate, (tuple, list)) and candidate:
            video = candidate[0] or ""
            audio = candidate[1] if len(candidate) > 1 else ""
            return video, audio or ""
        if isinstance(candidate, dict):
            return candidate.get("video", "") or "", candidate.get("audio", "") or ""
        return None

    @staticmethod
    def _direct_candidates(url):
        path = urlsplit(url).path.lower()
        suffixes = (".m3u8", ".mpd", ".mp4", ".flv", ".webm", ".mkv", ".mov", ".ts", ".m4a")
        return [(url, "")] if path.endswith(suffixes) else []


def stream_resolver_defaults(settings_manager=None) -> dict:
    """D4：从 settings_manager 读取 StreamResolver 实例级默认配置。

    读取 sources_runtime.stream_cache_ttl_seconds 与
    sources_runtime.stream_signed_query_names；缺失/非法/读取失败一律回退
    当前硬编码默认（不返回键即走 StreamResolver 内置值和 DEFAULT_CACHE_TTL）。

    入参可为 SettingsManager（get(section,key,default) 接口）或 dict；其他
    类型或任何异常返回 {}（安全默认，不阻断取流）。
    """
    if settings_manager is None:
        return {}
    try:
        from .settings_manager import SettingsManager

        if isinstance(settings_manager, SettingsManager):
            def _get(key, default):
                return settings_manager.get("sources_runtime", key, default)
        elif isinstance(settings_manager, dict):
            section = settings_manager.get("sources_runtime") or {}
            if not isinstance(section, dict):
                return {}

            def _get(key, default):
                return section.get(key, default)
        else:
            return {}
    except Exception:  # noqa: BLE001 —— 读取通道不可用回退默认
        return {}

    options: dict = {}
    try:
        names = _get("stream_signed_query_names", None)
        if names is not None:
            options["signed_query_names"] = [str(n) for n in names]
    except TypeError:
        pass
    try:
        ttl = _get("stream_cache_ttl_seconds", None)
        if ttl is not None:
            options["cache_ttl"] = float(ttl)
    except (TypeError, ValueError):
        pass
    return options


def stream_resolver_options(source, signed_query_names=None, cache_ttl=None) -> dict:
    """源级流解析选项：读取 source.raw["stream_resolver"] 块的
    signed_query_names / cache_ttl；显式传入参数优先，未配置项省略。

    返回 dict 可直接 **kwargs 传给 StreamResolver.resolve()。
    cache_ttl 非法（不可浮点化）时省略，走 resolver 默认。
    """
    raw = getattr(source, "raw", None)
    cfg = raw.get("stream_resolver") if isinstance(raw, dict) else None
    if not isinstance(cfg, dict):
        cfg = {}
    options: dict = {}
    names = cfg.get("signed_query_names") if signed_query_names is None else signed_query_names
    if names is not None:
        options["signed_query_names"] = names
    value = cfg.get("cache_ttl") if cache_ttl is None else cache_ttl
    if value is not None:
        try:
            options["cache_ttl"] = float(value)
        except (TypeError, ValueError):
            pass
    return options
