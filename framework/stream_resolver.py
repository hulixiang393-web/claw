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
                 cache_ttl=DEFAULT_CACHE_TTL):
        self._content = content
        self._total_budget = float(total_budget)
        self._candidate_timeout = float(candidate_timeout)
        self._max_candidates = int(max_candidates)
        self._cache_ttl = max(0.0, float(cache_ttl))
        self._cache = {}
        self._inflight = {}
        self._lock = threading.RLock()

    def resolve(self, source, episode_url, quality="", merged=True, deadline=None, context=None,
                source_lock=False):
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
                    ttl = self._result_ttl(result)
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
    def _is_signed_url(cls, value):
        if not value:
            return False
        try:
            query = parse_qs(urlsplit(value).query)
        except Exception:
            return True
        return any(name.lower() in cls.SIGNED_QUERY_NAMES for name in query)

    @classmethod
    def cache_ttl_for_result(cls, result, cache_ttl):
        if any(cls._is_signed_url(value) for value in result if value):
            return 0.0
        return max(0.0, float(cache_ttl))

    def _result_ttl(self, result):
        return self.cache_ttl_for_result(result, self._cache_ttl)

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

    def _resolve_candidate(self, candidate, context, key):
        if callable(candidate):
            remaining = min(self._candidate_timeout, context.remaining())
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
            if not done.wait(remaining):
                context.check()
                return None
            if error:
                if isinstance(error[0], StreamCancelledError):
                    raise error[0]
                return None
            context.check()
            candidate = result[0] if result else None
        elif isinstance(candidate, dict) and callable(candidate.get("resolver")):
            return self._resolve_candidate(candidate["resolver"], context, key)
        return self._normalize_candidate(candidate)

    @staticmethod
    def _candidate_key(candidate):
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
        if not done.wait(remaining):
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
