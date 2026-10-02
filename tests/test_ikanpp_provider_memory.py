import json
import time
from pathlib import Path

from framework.ikanpp_provider_memory import ProviderMemory
from framework.parser import Parser
from framework.search import Search
from tests.test_ikanpp import _source, _FakeHttp


def test_provider_memory_persists_ewma_decay_and_failure_penalty(tmp_path: Path):
    path = tmp_path / "ikanpp-provider-memory.json"
    store = ProviderMemory(path, decay_seconds=100.0, failure_penalty_ms=500.0)
    store.record("slow", 1000.0, success=True, now=100.0)
    store.record("slow", 500.0, success=True, now=100.0)
    store.record("bad", 10.0, success=False, now=100.0)

    assert store.rank(["bad", "slow", "new"]) == ["slow", "new", "bad"]

    loaded = ProviderMemory(path, decay_seconds=100.0, failure_penalty_ms=500.0)
    assert loaded.rank(["bad", "slow", "new"], now=100.0) == ["slow", "new", "bad"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert set(payload["providers"]) == {"bad", "slow"}


def test_ikanpp_search_reorders_only_configured_provider_body_and_preserves_urls(tmp_path: Path):
    http = _FakeHttp()
    src = _source()
    store = ProviderMemory(tmp_path / "memory.json")
    store.record("jisu", 1.0, success=True)
    store.record("juliang", 1.0, success=False)
    searcher = Search(http, Parser(), provider_memory=store)

    results = searcher.search_one(src, "战狼")

    assert [item["id"] for item in http.last_body["sources"]][:2] == ["jisu", "guangsu"]
    assert all("source=" in result.url for result in results)
    assert next(result for result in results if result.title == "战狼2").url.endswith(
        "?title=战狼2&source=jisu"
    )


def test_non_ikanpp_search_does_not_use_provider_memory(tmp_path: Path):
    http = _FakeHttp()
    source = _source()
    source.source_id = "other"
    source.raw["$id"] = "other"
    store = ProviderMemory(tmp_path / "memory.json")
    store.record("jisu", 1.0, success=False)

    Search(http, Parser(), provider_memory=store).search_one(source, "战狼")

    assert http.last_body["sources"][0]["id"] == "juliang"
