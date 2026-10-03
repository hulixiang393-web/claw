from __future__ import annotations

import socket
from urllib.parse import urlsplit


def _local_ip_for_renderer(renderer) -> str:
    parts = urlsplit(renderer.location)
    if not parts.hostname:
        raise ValueError("renderer location has no hostname")
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    family = socket.AF_INET6 if ":" in parts.hostname else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.connect((parts.hostname, port))
        return sock.getsockname()[0]


class CastSession:
    def __init__(self, proxy, client_factory, on_local_player_stop=None):
        self.proxy = proxy
        self.client_factory = client_factory
        self.on_local_player_stop = on_local_player_stop
        self.client = None
        self.renderer = None
        self.lan_base = None
        self.current_episode_index = None
        self.local_player_stopped = False
        self._active = False
        self._current_url = None

    def start(self, renderer, episode_index: int, media_url: str,
              title: str, headers: dict, ad_block: dict | None,
              force_proxy: bool) -> None:
        local_ip = _local_ip_for_renderer(renderer)
        try:
            lan_base = self.proxy.start_lan_endpoint(local_ip)
            client = self.client_factory(renderer)
            lan_url = self.proxy.build_url(
                media_url, headers, ad_block, force_proxy, public_base=lan_base,
            )
            client.set_av_transport_uri(lan_url, title, self._mime_type(media_url))
            client.play()
        except Exception:
            try:
                self.proxy.stop_lan_endpoint()
            except Exception:
                pass
            self._clear_state()
            raise
        self.client = client
        self.renderer = renderer
        self.lan_base = lan_base
        self.current_episode_index = episode_index
        self.local_player_stopped = False
        self._active = True
        self._current_url = lan_url

    def stop(self) -> None:
        first_error = None
        try:
            if self._active and self.client is not None:
                self.client.stop()
        except Exception as exc:
            first_error = exc
        try:
            self.proxy.stop_lan_endpoint()
        except Exception as exc:
            if first_error is None:
                first_error = exc
        finally:
            self._clear_state()
        if first_error is not None:
            raise first_error

    def play(self) -> None:
        self._require_active().play()

    def pause(self) -> None:
        self._require_active().pause()

    def seek(self, seconds: int) -> None:
        self._require_active().seek(seconds)

    def set_episode(self, episode_index: int, media_url: str, title: str,
                    headers: dict, ad_block: dict | None,
                    force_proxy: bool) -> None:
        client = self._require_active()
        old_index = self.current_episode_index
        old_url = self._current_url
        new_url = None
        try:
            new_url = self.proxy.build_url(
                media_url, headers, ad_block, force_proxy, public_base=self.lan_base,
            )
            client.set_av_transport_uri(new_url, title, self._mime_type(media_url))
            client.play()
        except Exception:
            if new_url is not None:
                self._release_url(new_url)
            self.current_episode_index = old_index
            self._current_url = old_url
            raise
        self.current_episode_index = episode_index
        self._current_url = new_url

    def poll_state(self) -> dict:
        client = self._require_active()
        transport = client.get_transport_info()
        position = client.get_position_info()
        state = transport.get("state")
        result = {
            "state": state,
            "position": position.get("position"),
            "duration": position.get("duration"),
            "track": position.get("track"),
            "uri": position.get("uri"),
        }
        if state == "PLAYING" and not self.local_player_stopped:
            if self.on_local_player_stop is not None:
                self.on_local_player_stop()
            self.local_player_stopped = True
        return result

    def _require_active(self):
        if not self._active or self.client is None:
            raise RuntimeError("cast session is not active")
        return self.client

    @staticmethod
    def _mime_type(media_url: str) -> str:
        return (
            "application/vnd.apple.mpegurl"
            if urlsplit(media_url).path.lower().endswith(".m3u8")
            else "video/mp4"
        )

    def _release_url(self, url: str) -> None:
        token = url.rsplit("/", 1)[-1]
        tokens = getattr(self.proxy, "_tokens", None)
        lan_tokens = getattr(self.proxy, "_lan_tokens", None)
        if tokens is not None:
            tokens.pop(token, None)
        if lan_tokens is not None:
            lan_tokens.discard(token)

    def _clear_state(self) -> None:
        self.client = None
        self.renderer = None
        self.lan_base = None
        self.current_episode_index = None
        self.local_player_stopped = False
        self._active = False
        self._current_url = None
