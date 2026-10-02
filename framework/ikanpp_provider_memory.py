"""Persistent response-time memory for Ikanpp's upstream providers."""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path


class ProviderMemory:
    def __init__(
        self,
        path: str | Path,
        *,
        decay_seconds: float = 3600.0,
        failure_penalty_ms: float = 5000.0,
        ewma_alpha: float = 0.35,
    ) -> None:
        self.path = Path(path)
        self.decay_seconds = max(float(decay_seconds), 1.0)
        self.failure_penalty_ms = max(float(failure_penalty_ms), 0.0)
        self.ewma_alpha = min(max(float(ewma_alpha), 0.01), 1.0)
        self._lock = threading.RLock()
        self._providers = self._load()

    def rank(self, provider_ids, *, now: float | None = None) -> list[str]:
        now = time.time() if now is None else float(now)
        ids = [str(provider_id) for provider_id in provider_ids]
        with self._lock:
            ranked = sorted(
                enumerate(ids),
                key=lambda pair: (self._state(pair[1]), self._score(pair[1], now), pair[0], pair[1]),
            )
        return [provider_id for _, provider_id in ranked]

    def record(
        self,
        provider_id: str,
        response_ms: float | None,
        *,
        success: bool,
        now: float | None = None,
    ) -> None:
        provider_id = str(provider_id).strip()
        if not provider_id:
            return
        now = time.time() if now is None else float(now)
        latency = self._finite_nonnegative(response_ms)
        with self._lock:
            item = self._providers.setdefault(provider_id, {})
            item["samples"] = int(item.get("samples", 0)) + 1
            item["last_seen"] = now
            item["failures"] = int(item.get("failures", 0)) + (0 if success else 1)
            if success and latency is not None:
                previous = self._finite_nonnegative(item.get("ewma_ms"))
                item["ewma_ms"] = latency if previous is None else (
                    self.ewma_alpha * latency + (1.0 - self.ewma_alpha) * previous
                )
            self._save()

    def _state(self, provider_id: str) -> int:
        item = self._providers.get(provider_id) or {}
        if self._failure_count(item) > 0:
            return 2
        return 0 if self._finite_nonnegative(item.get("ewma_ms")) is not None else 1

    def _score(self, provider_id: str, now: float) -> float:
        item = self._providers.get(provider_id) or {}
        ewma = self._finite_nonnegative(item.get("ewma_ms"))
        failures = self._failure_count(item)
        if failures:
            return self.failure_penalty_ms + (ewma or 0.0)
        if ewma is None:
            return self.failure_penalty_ms * 0.5
        last_seen = self._finite_nonnegative(item.get("last_seen"))
        age = max(0.0, now - last_seen) if last_seen is not None else 0.0
        decay = math.exp(-age / self.decay_seconds)
        return ewma * decay

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            providers = data.get("providers", {}) if isinstance(data, dict) else {}
            return providers if isinstance(providers, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "providers": self._providers}
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _failure_count(item: dict) -> int:
        try:
            return max(0, int(item.get("failures", 0) or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _finite_nonnegative(value) -> float | None:
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value >= 0.0 else None
