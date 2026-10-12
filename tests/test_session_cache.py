from __future__ import annotations

from framework import cache_service


def test_discover_session_cache_is_not_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_service, "_session_singleton", None)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    stale_path = cache_dir / "redis_session.gz"
    stale_path.write_bytes(b"legacy session snapshot")

    cache = cache_service.get_session_cache(str(cache_dir))
    cache.set("disc:test", {"works": []})
    cache.save()
    assert cache.get("disc:test") is not None
    assert not stale_path.exists()
