from framework.settings_manager import SettingsManager, media_source_probe_settings


def test_media_source_probe_settings_defaults(tmp_path):
    settings = SettingsManager(tmp_path / "app_config.json")

    assert media_source_probe_settings(settings) == {
        "enabled": True,
        "max_sources": 5,
        "concurrency": 1,
        "timeout_ms": 30000,
    }


def test_media_source_probe_settings_can_be_disabled(tmp_path):
    settings = SettingsManager(tmp_path / "app_config.json")
    settings.set("media", "source_probe", {"enabled": False})

    result = media_source_probe_settings(settings)

    assert result["enabled"] is False
    assert result["max_sources"] == 5
    assert result["concurrency"] == 1
    assert result["timeout_ms"] == 30000


def test_media_source_probe_settings_clamps_limits(tmp_path):
    settings = SettingsManager(tmp_path / "app_config.json")
    settings.set(
        "media",
        "source_probe",
        {"max_sources": 99, "concurrency": 8, "timeout_ms": 500},
    )

    assert media_source_probe_settings(settings) == {
        "enabled": True,
        "max_sources": 5,
        "concurrency": 1,
        "timeout_ms": 1000,
    }


def test_media_source_probe_settings_clamps_max_sources_lower_bound(tmp_path):
    settings = SettingsManager(tmp_path / "app_config.json")
    settings.set("media", "source_probe", {"max_sources": 0})

    assert media_source_probe_settings(settings)["max_sources"] == 1


def test_media_source_probe_settings_does_not_write_user_config(tmp_path):
    path = tmp_path / "app_config.json"
    original = '{"media": {"source_probe": {"max_sources": 99}}}'
    path.write_text(original, encoding="utf-8")
    settings = SettingsManager(path)

    media_source_probe_settings(settings)

    assert path.read_text(encoding="utf-8") == original
