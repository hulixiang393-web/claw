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
    """投屏会话。

    并发契约：start/stop/set_episode 等状态变更方法在同一把锁下串行执行，
    其中 start()/stop()/set_episode() 在**持锁状态下**完成各自的网络 IO
    （DlnaClient SOAP 调用、LAN 端点启停），最坏可让其他线程（如轮询线程取
    快照、GUI 调 play/pause/seek）同步等待数秒至十余秒——这是延迟风险而非
    死锁；play/pause/seek 仅在锁内取 client 快照，网络调用在锁外。
    poll_state() 在轮询线程运行，client 网络调用在锁外做，仅「检查并标记
    local_player_stopped」临界区持锁（临界区复验 _active：会话在本轮网络
    调用期间被 stop() 则不再触发回调），on_local_player_stop 回调在持锁状态下
    于轮询线程执行，实现方不得在回调里阻塞等待其他线程调用本会话方法。
    回调标记遵循「只尝试一次」：local_player_stopped 先于回调置位，回调抛
    异常也不会在下一轮 PLAYING 时重复触发。

    proxy 契约：注入的 proxy 必须实现 start_lan_endpoint / build_url /
    release_token / stop_lan_endpoint / lan_endpoint_error 五个公开方法；
    stop() 无条件调用 lan_endpoint_error()，缺失该方法将以 AttributeError
    失败（finally 中的 _clear_state() 仍会执行，LAN 端点也已停止）。
    """

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
        self._current_title = None
        self._current_token_url = None
        self._transcode_session = None
        self._transcode_attempted = False
        self._lock = threading.RLock()

    def start(self, renderer, episode_index: int, media_url: str,
              title: str, headers: dict, ad_block: dict | None,
              force_proxy: bool) -> None:
        with self._lock:
            if self._active:
                raise RuntimeError("cast session is already active; call stop() first")
            self._transcode_attempted = False

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
            self._current_title = title
            self._current_token_url = lan_url

    def start_transcoded(self, renderer, episode_index: int, playlist_url: str,
                         title: str, transcode_session=None) -> None:
        with self._lock:
            if self._transcode_attempted:
                raise RuntimeError("transcode fallback already attempted")
            self._transcode_attempted = True
            was_active = self._active
            old_token_url = self._current_token_url
            client = self.client
            lan_base = self.lan_base
            if not was_active:
                local_ip = _local_ip_for_renderer(renderer)
                try:
                    lan_base = self.proxy.start_lan_endpoint(local_ip)
                    client = self.client_factory(renderer)
                except Exception:
                    try:
                        self.proxy.stop_lan_endpoint()
                    except Exception:
                        pass
                    self._clear_state()
                    raise

            try:
                client.set_av_transport_uri(
                    playlist_url, title, self._mime_type(playlist_url),
                )
                client.play()
            except Exception:
                if transcode_session is not None:
                    try:
                        transcode_session.stop()
                    except Exception:
                        pass
                if old_token_url is not None:
                    try:
                        self.proxy.release_token(old_token_url)
                    except Exception:
                        pass
                try:
                    if client is not None and was_active:
                        client.stop()
                except Exception:
                    pass
                try:
                    self.proxy.stop_lan_endpoint()
                except Exception:
                    pass
                self._clear_state()
                raise

            if old_token_url is not None and old_token_url != playlist_url:
                self.proxy.release_token(old_token_url)
            self.client = client
            self.renderer = renderer if not was_active else self.renderer
            self.lan_base = lan_base
            self.current_episode_index = episode_index
            self.local_player_stopped = False
            self._active = True
            self._current_url = playlist_url
            self._current_title = title
            self._current_token_url = None
            self._transcode_session = transcode_session

    def has_transcode_fallback(self) -> bool:
        with self._lock:
            return self._transcode_attempted

    def stop(self) -> None:
        with self._lock:
            first_error = None
            transcode_session = self._transcode_session
            token_url = self._current_token_url
            try:
                if self._active and self.client is not None:
                    self.client.stop()
            except Exception as exc:
                first_error = exc

            if transcode_session is not None:
                try:
                    transcode_session.stop()
                except Exception as exc:
                    if first_error is None:
                        first_error = exc

            if token_url is not None:
                try:
                    self.proxy.release_token(token_url)
                except Exception as exc:
                    if first_error is None:
                        first_error = exc

            try:
                self.proxy.stop_lan_endpoint()
                lan_error = self.proxy.lan_endpoint_error()
                if lan_error is not None and first_error is None:
                    first_error = lan_error
            except Exception as exc:
                if first_error is None:
                    first_error = exc
            finally:
                self._clear_state()

            if first_error is not None:
                raise first_error

    def play(self) -> None:
        with self._lock:
            client = self._require_active()
        client.play()

    def pause(self) -> None:
        with self._lock:
            client = self._require_active()
        client.pause()

    def seek(self, seconds: int) -> None:
        with self._lock:
            client = self._require_active()
        client.seek(seconds)

    def set_volume(self, level: int) -> None:
        """设置投屏设备主音量（0-100）。与 seek 同契约：锁内取 client 快照，
        网络调用在锁外。设备不支持 RenderingControl 时由 client 抛受控错误。"""
        with self._lock:
            client = self._require_active()
        client.set_volume(level)

    def set_episode(self, episode_index: int, media_url: str, title: str,
                    headers: dict, ad_block: dict | None,
                    force_proxy: bool) -> None:
        with self._lock:
            client = self._require_active()
            old_index = self.current_episode_index
            old_url = self._current_url
            old_token_url = self._current_token_url
            old_title = self._current_title or "Unknown"
            new_url = None
            uri_set = False

            try:
                new_url = self.proxy.build_url(
                    media_url, headers, ad_block, force_proxy, public_base=self.lan_base,
                )

                client.set_av_transport_uri(new_url, title, self._mime_type(media_url))
                uri_set = True

                client.play()

            except Exception as original_error:
                if uri_set:
                    try:
                        client.set_av_transport_uri(old_url, old_title,
                                                   self._mime_type(old_url))
                        client.play()
                    except Exception as recovery_error:
                        if new_url is not None:
                            self.proxy.release_token(new_url)
                        raise RuntimeError(
                            f"set_episode failed and recovery failed: {recovery_error}"
                            f" (original: {original_error})"
                        ) from recovery_error

                self.current_episode_index = old_index
                self._current_url = old_url

                if new_url is not None:
                    self.proxy.release_token(new_url)

                raise

            self.current_episode_index = episode_index
            self._current_url = new_url
            self._current_title = title
            self._current_token_url = new_url

            if old_token_url is not None:
                self.proxy.release_token(old_token_url)

    def poll_state(self) -> dict:
        with self._lock:
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
        if state == "PLAYING":
            with self._lock:
                if self._active and not self.local_player_stopped:
                    self.local_player_stopped = True
                    if self.on_local_player_stop is not None:
                        self.on_local_player_stop()
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

    def _clear_state(self) -> None:
        self.client = None
        self.renderer = None
        self.lan_base = None
        self.current_episode_index = None
        self.local_player_stopped = False
        self._active = False
        self._current_url = None
        self._current_title = None
        self._current_token_url = None
        self._transcode_session = None
