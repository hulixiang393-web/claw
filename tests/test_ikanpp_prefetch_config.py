import json
from pathlib import Path

from framework.media_proxy import MediaProxy


ROOT = Path(__file__).resolve().parent.parent


def test_ikanpp_uses_low_contention_playback_prefetch():
    raw = json.loads((ROOT / "sources" / "ikanpp.json").read_text(encoding="utf-8"))
    prefetch = ((raw.get("media") or {}).get("hls") or {}).get("prefetch") or {}
    assert prefetch == {"enabled": False, "depth": 1, "workers": 1}
    assert ((raw.get("media") or {}).get("hls") or {}).get(
        "network_caching_ms"
    ) == 30000


def test_prefetch_config_can_be_scoped_to_current_playback():
    proxy = MediaProxy(cache=None, prefetch={"enabled": False})
    proxy.configure_prefetch(True, depth=30, workers=3)
    assert proxy._pf_cfg["enabled"] is True
    assert proxy._pf_cfg["depth"] == 30
    assert proxy._pf_cfg["workers"] == 3
    proxy.stop()
