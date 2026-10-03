import re
import socket
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urljoin


_SSDP_ADDRESS = ("239.255.255.250", 1900)
_MEDIA_RENDERER_ST = "urn:schemas-upnp-org:device:MediaRenderer:1"
_AV_TRANSPORT = "AVTransport"
_RENDERING_CONTROL = "RenderingControl"
_SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
_DIDL = "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
_DC = "http://purl.org/dc/elements/1.1/"
_UPNP = "urn:schemas-upnp-org:metadata-1-0/upnp/"


@dataclass(frozen=True)
class DlnaRenderer:
    udn: str
    friendly_name: str
    location: str
    av_transport_control_url: str
    rendering_control_url: str | None
    service_type: str


class DlnaError(Exception):
    def __init__(self, action: str, code: int | str, description: str):
        self.action = action
        self.code = code
        self.description = description
        super().__init__(f"{action} failed ({code}): {description}")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(element: ET.Element | None) -> str:
    return "" if element is None or element.text is None else element.text.strip()


def parse_ssdp_response(data: bytes) -> dict[str, str]:
    result = {}
    for line in data.decode("latin-1", errors="replace").splitlines():
        key, separator, value = line.partition(":")
        if separator:
            result[key.strip().lower()] = value.strip()
    return result


def parse_device_description(location: str, body: bytes) -> DlnaRenderer:
    root = ET.fromstring(body)
    device = next((element for element in root.iter() if _local_name(element.tag) == "device"), None)
    if device is None:
        raise ValueError("device description has no device")

    values = {
        _local_name(element.tag): _text(element)
        for element in device
        if _local_name(element.tag) in {"UDN", "friendlyName"}
    }
    services = [
        element for element in device.iter()
        if _local_name(element.tag) == "service"
    ]
    av_transport = None
    rendering = None
    for service in services:
        fields = {
            _local_name(element.tag): _text(element)
            for element in service
        }
        service_type = fields.get("serviceType", "")
        control_url = fields.get("controlURL", "")
        if service_type.endswith(":AVTransport:1"):
            av_transport = (service_type, control_url)
        elif service_type.endswith(":RenderingControl:1"):
            rendering = control_url

    if not values.get("UDN") or not av_transport or not av_transport[1]:
        raise ValueError("device description has no AVTransport service")

    return DlnaRenderer(
        udn=values["UDN"],
        friendly_name=values.get("friendlyName", values["UDN"]),
        location=location,
        av_transport_control_url=urljoin(location, av_transport[1]),
        rendering_control_url=urljoin(location, rendering) if rendering else None,
        service_type=av_transport[0],
    )


def discover_renderers(timeout: float = 2.0) -> list[DlnaRenderer]:
    request = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 1\r\n"
        f"ST: {_MEDIA_RENDERER_ST}\r\n\r\n"
    ).encode("ascii")
    responses = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(request, _SSDP_ADDRESS)
        while True:
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                break
            except OSError:
                break
            parsed = parse_ssdp_response(data)
            location = parsed.get("location")
            if location:
                usn = parsed.get("usn", "")
                udn = usn.split("::", 1)[0] if usn else location
                if udn not in {item[0] for item in responses}:
                    responses.append((udn, location))

    renderers = []
    seen = set()
    for _, location in responses:
        try:
            req = urllib.request.Request(location)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                renderer = parse_device_description(location, response.read())
        except (OSError, ValueError, ET.ParseError, urllib.error.URLError):
            continue
        if renderer.udn not in seen:
            seen.add(renderer.udn)
            renderers.append(renderer)
    return renderers


def format_dlna_time(seconds: int) -> str:
    if not isinstance(seconds, int) or seconds < 0:
        raise ValueError("seconds must be a non-negative integer")
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def parse_dlna_time(value: str) -> int | None:
    if not value or not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2})", value.strip())
    if not match:
        return None
    hours, minutes, seconds = (int(part) for part in match.groups())
    if minutes > 59 or seconds > 59:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _metadata(uri: str, title: str, mime_type: str) -> str:
    item = ET.Element(f"{{{_DIDL}}}DIDL-Lite")
    container = ET.SubElement(item, f"{{{_DIDL}}}item", {"id": "0", "parentID": "-1", "restricted": "1"})
    title_element = ET.SubElement(container, f"{{{_DC}}}title")
    title_element.text = title
    class_element = ET.SubElement(container, f"{{{_UPNP}}}class")
    class_element.text = "object.item.videoItem"
    resource = ET.SubElement(container, f"{{{_DIDL}}}res", {"protocolInfo": f"http-get:*:{mime_type}:*"})
    resource.text = uri
    return ET.tostring(item, encoding="unicode")


def _children_by_name(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element.iter() if _local_name(child.tag) == name]


class DlnaClient:
    def __init__(self, renderer: DlnaRenderer, timeout: float = 5.0):
        self.renderer = renderer
        self.timeout = timeout

    def _soap(self, action: str, arguments: dict[str, str] | None = None) -> ET.Element:
        envelope = ET.Element(f"{{{_SOAP_ENV}}}Envelope")
        body = ET.SubElement(envelope, f"{{{_SOAP_ENV}}}Body")
        action_element = ET.SubElement(body, f"{{{self.renderer.service_type}}}{action}")
        for name, value in (arguments or {}).items():
            child = ET.SubElement(action_element, name)
            child.text = value
        payload = ET.tostring(envelope, encoding="utf-8", xml_declaration=True)
        soap_action = f'"{self.renderer.service_type}#{action}"'
        request = urllib.request.Request(
            self.renderer.av_transport_control_url,
            data=payload,
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": soap_action,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response_body = response.read()
        except urllib.error.HTTPError as error:
            response_body = error.read()
        root = ET.fromstring(response_body)
        fault = next((element for element in root.iter() if _local_name(element.tag) == "Fault"), None)
        if fault is not None:
            code_text = _text(next((item for item in fault.iter() if _local_name(item.tag) == "errorCode"), None))
            description = _text(next((item for item in fault.iter() if _local_name(item.tag) == "errorDescription"), None))
            if not description:
                description = _text(next((item for item in fault.iter() if _local_name(item.tag) == "faultstring"), None))
            try:
                code: int | str = int(code_text)
            except ValueError:
                code = code_text
            raise DlnaError(action, code, description)
        return root

    def set_av_transport_uri(self, uri: str, title: str, mime_type: str) -> None:
        self._soap(
            "SetAVTransportURI",
            {
                "InstanceID": "0",
                "CurrentURI": uri,
                "CurrentURIMetaData": _metadata(uri, title, mime_type),
            },
        )

    def set_next_av_transport_uri(self, uri: str, title: str, mime_type: str) -> None:
        self._soap(
            "SetNextAVTransportURI",
            {
                "InstanceID": "0",
                "NextURI": uri,
                "NextURIMetaData": _metadata(uri, title, mime_type),
            },
        )

    def play(self) -> None:
        self._soap("Play", {"InstanceID": "0", "Speed": "1"})

    def pause(self) -> None:
        self._soap("Pause", {"InstanceID": "0"})

    def stop(self) -> None:
        self._soap("Stop", {"InstanceID": "0"})

    def seek(self, seconds: int) -> None:
        self._soap("Seek", {"InstanceID": "0", "Unit": "REL_TIME", "Target": format_dlna_time(seconds)})

    def get_transport_info(self) -> dict:
        root = self._soap("GetTransportInfo", {"InstanceID": "0"})
        values = {}
        for element in root.iter():
            name = _local_name(element.tag)
            if name in {"CurrentTransportState", "CurrentTransportStatus", "CurrentSpeed"}:
                values[name] = _text(element)
        return {
            "state": values.get("CurrentTransportState", ""),
            **({"status": values["CurrentTransportStatus"]} if "CurrentTransportStatus" in values else {}),
            **({"speed": values["CurrentSpeed"]} if "CurrentSpeed" in values else {}),
        }

    def get_position_info(self) -> dict:
        root = self._soap("GetPositionInfo", {"InstanceID": "0"})
        values = {}
        for element in root.iter():
            name = _local_name(element.tag)
            if name in {"RelTime", "TrackDuration", "Track", "TrackURI"}:
                values[name] = _text(element)
        track = None
        if values.get("Track", "").isdigit():
            track = int(values["Track"])
        return {
            "position": parse_dlna_time(values.get("RelTime", "")),
            "duration": parse_dlna_time(values.get("TrackDuration", "")),
            "track": track,
            "uri": values.get("TrackURI", "") or None,
        }
