from __future__ import annotations

import json
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return


def find_ffmpeg(explicit: str | None = None) -> str:
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return str(path)
        raise FileNotFoundError(f"FFmpeg not found: {explicit}")
    path = shutil.which("ffmpeg")
    if path:
        return path
    raise FileNotFoundError("FFmpeg not found on PATH; pass --ffmpeg")


def _ffprobe_path(ffmpeg_path: str) -> str:
    candidate = Path(ffmpeg_path).with_name("ffprobe.exe" if Path(ffmpeg_path).suffix.lower() == ".exe" else "ffprobe")
    return str(candidate) if candidate.is_file() else (shutil.which("ffprobe") or "ffprobe")


def _safe_stderr(stderr: bytes | str, media_url: str | None = None) -> str:
    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
    if media_url:
        text = text.replace(media_url, "<media-url>")
        parsed = urlsplit(media_url)
        if parsed.query:
            text = text.replace(parsed.query, "<query>")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[-3:])[:1000]


def run_ffprobe(
    ffmpeg_path: str,
    media_url: str,
    timeout: float = 10.0,
    allow_extensionless: bool = False,
) -> dict:
    command = [
        _ffprobe_path(ffmpeg_path),
        "-v",
        "error",
    ]
    if allow_extensionless:
        command.extend(["-extension_picky", "0"])
    command.extend([
        "-show_entries",
        "format=format_name,duration:stream=codec_type,codec_name",
        "-of",
        "json",
        media_url,
    ])
    try:
        completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return {"returncode": None, "stderr_summary": _safe_stderr(exc.stderr or b"", media_url), "timed_out": True}
    result = {"returncode": completed.returncode, "stderr_summary": _safe_stderr(completed.stderr, media_url)}
    if completed.returncode != 0:
        return result
    try:
        payload = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        result["stderr_summary"] = "ffprobe returned invalid JSON"
        result["returncode"] = 1
        return result
    streams = payload.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    fmt = payload.get("format", {})
    result.update(
        {
            "container": fmt.get("format_name"),
            "duration": float(fmt["duration"]) if fmt.get("duration") else 0.0,
            "video_codec": video.get("codec_name"),
            "audio_codec": audio.get("codec_name"),
        }
    )
    return result


def decode_probe(
    ffmpeg_path: str,
    media_url: str,
    seconds: float = 3.0,
    timeout: float = 20.0,
    allow_extensionless: bool = False,
) -> dict:
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-t",
        str(seconds),
    ]
    if allow_extensionless:
        command.extend(["-extension_picky", "0"])
    command.extend([
        "-i",
        media_url,
        "-map",
        "0:v:0?",
        "-map",
        "0:a:0?",
        "-f",
        "null",
        "-",
    ])
    try:
        completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return {"decoded": False, "returncode": None, "stderr_summary": _safe_stderr(exc.stderr or b"", media_url), "timed_out": True}
    return {
        "decoded": completed.returncode == 0,
        "returncode": completed.returncode,
        "stderr_summary": _safe_stderr(completed.stderr, media_url),
    }


class VirtualMediaFetcher:
    def __init__(self, ffmpeg_path: str | None, timeout: float = 10.0):
        self.ffmpeg_path = ffmpeg_path
        self.timeout = timeout

    def fetch(self, url: str) -> dict:
        started = time.perf_counter()
        request = Request(url, headers={"User-Agent": "virtual-dlna-renderer/1"})
        with urlopen(request, timeout=self.timeout) as response:
            first = response.read(1)
            first_byte_ms = (time.perf_counter() - started) * 1000
            total = len(first)
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
            return {
                "http_status": response.status,
                "content_type": response.headers.get_content_type(),
                "first_byte_ms": first_byte_ms,
                "bytes": total,
            }

    def decode(self, url: str) -> dict:
        if not self.ffmpeg_path:
            raise FileNotFoundError("FFmpeg path is required for decode")
        http = self.fetch(url)
        allow_extensionless = http.get("content_type") in {
            "application/vnd.apple.mpegurl",
            "application/x-mpegURL",
            "audio/mpegurl",
        }
        probe = run_ffprobe(
            self.ffmpeg_path,
            url,
            self.timeout,
            allow_extensionless=allow_extensionless,
        )
        decoded = decode_probe(
            self.ffmpeg_path,
            url,
            timeout=max(20.0, self.timeout),
            allow_extensionless=allow_extensionless,
        )

        result = {**http, **probe, **decoded, "probe": probe, "decode": decoded}
        return result


def start_media_server(root: Path) -> tuple[ThreadingHTTPServer, str]:
    directory = str(root.resolve())

    class Handler(_QuietHandler):
        def __init__(self, *args: Any, **kwargs: Any):
            super().__init__(*args, directory=directory, **kwargs)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


_SOAP_ENV_NS = "http://schemas.xmlsoap.org/soap/envelope/"
_AV_TRANSPORT_NS = "urn:schemas-upnp-org:service:AVTransport:1"
_DEVICE_NS = "urn:schemas-upnp-org:device-1-0"


class VirtualRenderer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        ffmpeg_path: str | None = None,
        fetch_timeout: float = 10.0,
        supported_video_codecs: set[str] | None = None,
        supported_audio_codecs: set[str] | None = None,
    ):
        self.host = host
        self.port = port
        self.ffmpeg_path = ffmpeg_path
        self.fetch_timeout = fetch_timeout
        self.supported_video_codecs = supported_video_codecs
        self.supported_audio_codecs = supported_audio_codecs
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None
        self._lock = Lock()
        self._transport_state = "NO_MEDIA_PRESENT"
        self._transport_status = "OK"
        self._uri = ""
        self._duration = 0.0
        self._position = 0.0
        self._playing_since: float | None = None
        self._error = ""
        self._last_result: dict[str, Any] = {}

    def start(self):
        if self._server is not None:
            return self._descriptor()

        renderer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                if self.path.split("?", 1)[0] in {"/description.xml", "/device.xml"}:
                    self._send_xml(200, renderer._device_description())
                    return
                self.send_error(404)

            def do_POST(self) -> None:
                if self.path.split("?", 1)[0] != "/upnp/control/avtransport1":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    action, arguments = renderer._parse_soap(self.rfile.read(length))
                    response = renderer._handle_action(action, arguments)
                except Exception:
                    response = renderer._soap_fault()
                    self._send_xml(500, response)
                    return
                self._send_xml(200, response)

            def _send_xml(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", 'text/xml; charset="utf-8"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self._descriptor()

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2.0)

    def state(self) -> dict[str, Any]:
        with self._lock:
            position = self._current_position_locked()
            return {
                "state": self._transport_state,
                "status": self._transport_status,
                "speed": "1",
                "uri": self._uri or None,
                "position": int(position),
                "duration": int(self._duration),
                "error": self._error or None,
            }

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._last_result)

    def set_uri(self, uri: str, title: str = "", mime_type: str = "video/mp4") -> None:
        del title, mime_type
        with self._lock:
            self._uri = uri
            self._duration = 0.0
            self._position = 0.0
            self._playing_since = None
            self._error = ""
            self._transport_status = "OK"
            self._transport_state = "TRANSITIONING"

    def play(self) -> None:
        with self._lock:
            if not self._uri:
                self._transport_state = "ERROR"
                self._transport_status = "ERROR_OCCURRED"
                self._error = "No media URI configured"
                raise RuntimeError("No media URI configured")
            if self._transport_state == "PAUSED_PLAYBACK":
                self._playing_since = time.monotonic()
                self._transport_state = "PLAYING"
                return
            uri = self._uri
        try:
            result = VirtualMediaFetcher(self.ffmpeg_path, self.fetch_timeout).decode(uri)
            incompatible = (
                self.supported_video_codecs is not None
                and result.get("video_codec") not in self.supported_video_codecs
            ) or (
                self.supported_audio_codecs is not None
                and result.get("audio_codec") not in self.supported_audio_codecs
            )
            if incompatible:
                result = {**result, "decoded": False, "error": "unsupported renderer codec"}
        except Exception as exc:
            result = {"decoded": False, "error": type(exc).__name__}
        with self._lock:
            self._last_result = dict(result)
        if not result.get("decoded"):
            with self._lock:
                self._transport_state = "ERROR"
                self._transport_status = "ERROR_OCCURRED"
                self._playing_since = None
                self._error = "Media playback failed"
            raise RuntimeError("Media playback failed")
        with self._lock:
            self._duration = max(0.0, float(result.get("duration") or 0.0))
            self._position = min(self._position, self._duration) if self._duration else self._position
            self._playing_since = time.monotonic()
            self._transport_state = "PLAYING"
            self._transport_status = "OK"
            self._error = ""

    def pause(self) -> None:
        with self._lock:
            if self._transport_state == "PLAYING":
                self._position = self._current_position_locked()
                self._playing_since = None
            self._transport_state = "PAUSED_PLAYBACK"

    def stop_playback(self) -> None:
        with self._lock:
            self._position = 0.0
            self._playing_since = None
            self._transport_state = "STOPPED"
            self._transport_status = "OK"

    def seek(self, seconds: int) -> None:
        with self._lock:
            self._position = max(0.0, min(float(seconds), self._duration)) if self._duration else max(0.0, float(seconds))
            if self._transport_state == "PLAYING":
                self._playing_since = time.monotonic()

    def _descriptor(self):
        from framework.dlna import DlnaRenderer

        location = self._base_url() + "/description.xml"
        return DlnaRenderer(
            udn="uuid:virtual-dlna-renderer",
            friendly_name="Virtual DLNA Renderer",
            location=location,
            av_transport_control_url=self._base_url() + "/upnp/control/avtransport1",
            rendering_control_url=None,
            service_type=_AV_TRANSPORT_NS,
        )

    def _base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("Virtual renderer is not started")
        host = self.host
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        return f"http://{host}:{self._server.server_port}"

    def _device_description(self) -> bytes:
        root = ET.Element(f"{{{_DEVICE_NS}}}root", {"configId": "1"})
        ET.SubElement(root, f"{{{_DEVICE_NS}}}specVersion")
        spec = root[0]
        ET.SubElement(spec, f"{{{_DEVICE_NS}}}major").text = "1"
        ET.SubElement(spec, f"{{{_DEVICE_NS}}}minor").text = "0"
        device = ET.SubElement(root, f"{{{_DEVICE_NS}}}device")
        ET.SubElement(device, f"{{{_DEVICE_NS}}}deviceType").text = "urn:schemas-upnp-org:device:MediaRenderer:1"
        ET.SubElement(device, f"{{{_DEVICE_NS}}}friendlyName").text = "Virtual DLNA Renderer"
        ET.SubElement(device, f"{{{_DEVICE_NS}}}manufacturer").text = "claw"
        ET.SubElement(device, f"{{{_DEVICE_NS}}}UDN").text = "uuid:virtual-dlna-renderer"
        services = ET.SubElement(device, f"{{{_DEVICE_NS}}}serviceList")
        service = ET.SubElement(services, f"{{{_DEVICE_NS}}}service")
        ET.SubElement(service, f"{{{_DEVICE_NS}}}serviceType").text = _AV_TRANSPORT_NS
        ET.SubElement(service, f"{{{_DEVICE_NS}}}serviceId").text = "urn:upnp-org:serviceId:AVTransport"
        ET.SubElement(service, f"{{{_DEVICE_NS}}}controlURL").text = "/upnp/control/avtransport1"
        ET.SubElement(service, f"{{{_DEVICE_NS}}}eventSubURL").text = "/upnp/event/avtransport1"
        ET.SubElement(service, f"{{{_DEVICE_NS}}}SCPDURL").text = "/upnp/avtransport1.xml"
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    def _parse_soap(self, payload: bytes) -> tuple[str, dict[str, str]]:
        root = ET.fromstring(payload)
        action_element = next(element for element in root.iter() if element.tag.rsplit("}", 1)[-1] not in {"Envelope", "Body"})
        action = action_element.tag.rsplit("}", 1)[-1]
        arguments = {
            element.tag.rsplit("}", 1)[-1]: element.text or ""
            for element in action_element
        }
        return action, arguments

    def _handle_action(self, action: str, arguments: dict[str, str]) -> bytes:
        if action == "SetAVTransportURI":
            self.set_uri(arguments.get("CurrentURI", ""), arguments.get("CurrentURIMetaData", ""), "")
            return self._soap_response(action)
        if action == "Play":
            self.play()
            return self._soap_response(action)
        if action == "Pause":
            self.pause()
            return self._soap_response(action)
        if action == "Stop":
            self.stop_playback()
            return self._soap_response(action)
        if action == "Seek":
            target = arguments.get("Target", "00:00:00")
            parts = target.split(":")
            self.seek(int(parts[0]) * 3600 + int(parts[1]) * 60 + int(float(parts[2])))
            return self._soap_response(action)
        if action == "GetTransportInfo":
            current = self.state()
            return self._soap_response(action, {
                "CurrentTransportState": current["state"],
                "CurrentTransportStatus": current["status"],
                "CurrentSpeed": current["speed"],
            })
        if action == "GetPositionInfo":
            current = self.state()
            return self._soap_response(action, {
                "Track": "1" if current["uri"] else "0",
                "TrackDuration": _format_duration(current["duration"]),
                "RelTime": _format_duration(current["position"]),
                "TrackURI": current["uri"] or "",
            })
        raise RuntimeError("Unsupported action")

    def _current_position_locked(self) -> float:
        if self._transport_state == "PLAYING" and self._playing_since is not None:
            position = self._position + time.monotonic() - self._playing_since
            return min(position, self._duration) if self._duration else position
        return self._position

    def _soap_response(self, action: str, values: dict[str, str] | None = None) -> bytes:
        envelope = ET.Element(f"{{{_SOAP_ENV_NS}}}Envelope")
        body = ET.SubElement(envelope, f"{{{_SOAP_ENV_NS}}}Body")
        response = ET.SubElement(body, f"{{{_AV_TRANSPORT_NS}}}{action}Response")
        for name, value in (values or {}).items():
            ET.SubElement(response, name).text = value
        return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)

    def _soap_fault(self) -> bytes:
        envelope = ET.Element(f"{{{_SOAP_ENV_NS}}}Envelope")
        body = ET.SubElement(envelope, f"{{{_SOAP_ENV_NS}}}Body")
        fault = ET.SubElement(body, f"{{{_SOAP_ENV_NS}}}Fault")
        ET.SubElement(fault, "faultcode").text = "s:Client"
        ET.SubElement(fault, "faultstring").text = "Action failed"
        detail = ET.SubElement(fault, "detail")
        error = ET.SubElement(detail, "UPnPError")
        ET.SubElement(error, "errorCode").text = "501"
        ET.SubElement(error, "errorDescription").text = "Action failed"
        return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def _format_duration(seconds: int) -> str:
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
