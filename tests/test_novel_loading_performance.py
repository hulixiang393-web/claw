from __future__ import annotations

import json
from pathlib import Path

import pytest

from framework.settings_manager import SettingsManager, discover_preload_settings
from gui.pages.discover_page import DiscoverPage


class _Manager:
    def discoverable_sources(self):
        return []


class _Settings:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, section, key, default=None):
        return self.values.get(section, {}).get(key, default)


def test_discover_preload_defaults_are_one_page_and_two_workers():
    settings = _Settings()

    assert discover_preload_settings(settings) == {"pages": 1, "concurrency": 2}


def test_discover_preload_settings_cap_explicit_values_without_writing_config():
    settings = _Settings({"discover": {"preload_pages": 9, "preload_concurrency": 8}})

    assert discover_preload_settings(settings) == {"pages": 2, "concurrency": 2}


def test_discover_preload_settings_preserve_user_config_file(tmp_path):
    path = tmp_path / "app_config.json"
    path.write_text(
        json.dumps({"discover": {"preload_pages": 4, "preload_concurrency": 3}}),
        encoding="utf-8",
    )

    settings = SettingsManager(path)

    assert settings.get("discover", "preload_pages") == 4
    assert settings.get("discover", "preload_concurrency") == 3
    assert discover_preload_settings(settings) == {"pages": 2, "concurrency": 2}
    assert json.loads(path.read_text(encoding="utf-8"))["discover"] == {
        "preload_pages": 4,
        "preload_concurrency": 3,
    }


@pytest.fixture
def discover_page(_qapp):
    page = DiscoverPage(_Manager(), None, None, None, None, None)
    yield page
    page.deleteLater()


def test_discover_page_preload_defaults_and_upper_bounds(discover_page):
    assert discover_page._preload_ahead == 1
    assert discover_page._preload_concurrency == 2

    discover_page.apply_preload_settings(9, 8)

    assert discover_page._preload_ahead == 2
    assert discover_page._preload_concurrency == 2


def test_fanqie_search_render_config_is_fast_and_single_page():
    source_path = Path(__file__).parents[1] / "sources" / "fanqie.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    render_config = source["endpoints"]["search"]["item"]["render_config"]

    assert render_config["wait_for"] == "a[href*='/page/']"
    assert render_config["wait_until"] == "domcontentloaded"
    assert render_config["extra_delay_ms"] == 0
    assert render_config["response_pages"] == 1
