from framework import cache_service


def _reset_singletons():
    cache_service._shelf_singleton = None
    cache_service._search_singleton = None
    cache_service._session_singleton = None


def test_session_cache_persists_to_disk(tmp_path):
    _reset_singletons()
    s1 = cache_service.get_session_cache(str(tmp_path))
    s1.set("snap:s1", {"pages": [1, 2, 3], "current_page": 3})
    s1.save()

    _reset_singletons()
    s2 = cache_service.get_session_cache(str(tmp_path))
    assert s2.get("snap:s1") == {"pages": [1, 2, 3], "current_page": 3}
    assert (tmp_path / "redis_session.gz").exists()


def test_session_cache_lru_evicts_oldest_source(tmp_path):
    _reset_singletons()
    s = cache_service.get_session_cache(str(tmp_path))
    for i in range(7):
        s.set(f"snap:s{i}", {"n": i})
    assert s.get("snap:s0") is None
    assert s.get("snap:s6") is not None


def test_corrupt_session_file_degrades_to_empty(tmp_path):
    _reset_singletons()
    (tmp_path / "redis_session.gz").write_bytes(b"not-a-valid-store")
    s = cache_service.get_session_cache(str(tmp_path))
    assert s.get("snap:anything") is None
