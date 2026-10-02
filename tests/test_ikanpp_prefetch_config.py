from framework.media_proxy import MediaProxy


def test_prefetch_config_can_be_scoped_to_current_playback():
    proxy = MediaProxy(cache=None, prefetch={"enabled": False})
    proxy.configure_prefetch(True, depth=30, workers=3)
    assert proxy._pf_cfg["enabled"] is True
    assert proxy._pf_cfg["depth"] == 30
    assert proxy._pf_cfg["workers"] == 3
    proxy.stop()
