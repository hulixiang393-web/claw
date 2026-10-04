from __future__ import annotations

import socket
import threading
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
        self._lock = threading.Lock()
        self._lan_endpoint_error = None

    def start(self, renderer, episode_index: int, media_url: str,
              title: str, headers: dict, ad_block: dict | None,
              force_proxy: bool) -> None:
        if self._active:
            raise RuntimeError("cast session is already active; call stop() first")

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
        self._lan_endpoint_error = None

    def stop(self) -> None:
        first_error = None
        try:
            if self._active and self.client is not None:
                self.client.stop()
        except Exception as exc:
            first_error = exc

        try:
            self.proxy.stop_lan_endpoint()
            if self.proxy._lan_error is not None:
                lan_error = self.proxy._lan_error
                self.proxy._lan_error = None
                if first_error is None:
                    first_error = lan_error
        except Exception as exc:
            self._lan_endpoint_error = exc
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
        with self._lock:
            client = self._require_active()
            old_index = self.current_episode_index
            old_url = self._current_url
            old_title = self._episode_title_from_calls(client)
            new_url = None
            uri_set = False

            try:
                new_url = self.proxy.build_url(
                    media_url, headers, ad_block, force_proxy, public_base=self.lan_base,
                )

                client.set_av_transport_uri(new_url, title, self._mime_type(media_url))
                uri_set = True

                client.play()

            except Exception:
                if uri_set:
                    try:
                        client.set_av_transport_uri(old_url, old_title or "Unknown",
                                                   self._mime_type(old_url))
                        client.play()
                    except Exception as recovery_error:
                        if new_url is not None:
                            self.proxy.release_token(new_url)
                        raise RuntimeError(f"set_episode failed and recovery failed: {recovery_error}") from recovery_error

                self.current_episode_index = old_index
                self._current_url = old_url

                if new_url is not None:
                    self.proxy.release_token(new_url)

                raise

            self.current_episode_index = episode_index
            self._current_url = new_url

            if old_url is not None:
                self.proxy.release_token(old_url)

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

    @staticmethod
    def _episode_title_from_calls(client) -> str:
        if hasattr(client, 'calls'):
            for call in reversed(client.calls):
                if isinstance(call, tuple) and len(call) >= 2 and call[0] == "set_av_transport_uri":
                    return call[2] if len(call) > 2 else "Unknown"
        return "Unknown"

    def _clear_state(self) -> None:
        self.client = None
        self.renderer = None
        self.lan_base = None
        self.current_episode_index = None
        self.local_player_stopped = False
        self._active = False
        self._current_url = None
