import socket
import urllib.error

import pytest

import framework.dlna as dlna


SSDP_RESPONSE = (
    b"HTTP/1.1 200 OK\r\n"
    b"LOCATION: http://192.168.1.20:80/root.xml\r\n"
    b"ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
    b"USN: uuid:renderer-1::urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n"
)

DEVICE_XML = """<root xmlns='urn:schemas-upnp-org:device-1-0'>
  <device>
    <UDN>uuid:tv-1</UDN>
    <friendlyName>客厅电视</friendlyName>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
        <controlURL>/upnp/control/avtransport</controlURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
        <controlURL>upnp/control/rendering</controlURL>
      </service>
    </serviceList>
  </device>
</root>""".encode("utf-8")


class _Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.body


def test_parse_ssdp_response_extracts_location_and_usn():
    parsed = dlna.parse_ssdp_response(SSDP_RESPONSE)
    assert parsed["location"] == "http://192.168.1.20:80/root.xml"
    assert parsed["usn"] == (
        "uuid:renderer-1::urn:schemas-upnp-org:device:MediaRenderer:1"
    )


def test_parse_device_description_finds_services_and_resolves_relative_url():
    renderer = dlna.parse_device_description(
        "http://192.168.1.20:80/root.xml", DEVICE_XML
    )
    assert renderer.udn == "uuid:tv-1"
    assert renderer.friendly_name == "客厅电视"
    assert renderer.av_transport_control_url == (
        "http://192.168.1.20:80/upnp/control/avtransport"
    )
    assert renderer.rendering_control_url == (
        "http://192.168.1.20:80/upnp/control/rendering"
    )


def test_dlna_time_round_trips_and_invalid_values_return_none():
    assert dlna.format_dlna_time(3723) == "01:02:03"
    assert dlna.parse_dlna_time("01:02:03") == 3723
    assert dlna.parse_dlna_time("") is None
    assert dlna.parse_dlna_time("bad") is None
    assert dlna.parse_dlna_time("01:60:00") is None


def test_soap_request_contains_action_instance_uri_title_and_mime(monkeypatch):
    renderer = dlna.DlnaRenderer(
        "uuid:tv-1",
        "客厅电视",
        "http://192.168.1.20:80/root.xml",
        "http://192.168.1.20:80/control",
        None,
        "urn:schemas-upnp-org:service:AVTransport:1",
    )
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response(
            b"<s:Envelope xmlns:s='http://schemas.xmlsoap.org/soap/envelope/'>"
            b"<s:Body><u:SetAVTransportURIResponse "
            b"xmlns:u='urn:schemas-upnp-org:service:AVTransport:1'/></s:Body></s:Envelope>"
        )

    monkeypatch.setattr(dlna.urllib.request, "urlopen", fake_urlopen)
    dlna.DlnaClient(renderer, timeout=4).set_av_transport_uri(
        "http://192.168.1.10:45678/video.m3u8", "第一集 & 预告", "application/vnd.apple.mpegurl"
    )

    body = captured["request"].data.decode("utf-8")
    assert captured["timeout"] == 4
    assert captured["request"].full_url == renderer.av_transport_control_url
    assert captured["request"].get_header("Content-type") == "text/xml; charset=\"utf-8\""
    assert captured["request"].get_header("Soapaction") == (
        '"urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI"'
    )
    assert "<InstanceID>0</InstanceID>" in body
    assert "http://192.168.1.10:45678/video.m3u8" in body
    assert "第一集 &amp;amp; 预告" in body
    assert "object.item.videoItem" in body
    assert "application/vnd.apple.mpegurl" in body


def test_soap_fault_exposes_code_and_description(monkeypatch):
    renderer = dlna.DlnaRenderer(
        "uuid:tv-1", "电视", "http://tv/root.xml", "http://tv/control", None,
        "urn:schemas-upnp-org:service:AVTransport:1",
    )
    fault = b"""<s:Envelope xmlns:s='http://schemas.xmlsoap.org/soap/envelope/'>
      <s:Body><s:Fault>
        <faultcode>s:Client</faultcode><faultstring>Invalid Action</faultstring>
        <detail><UPnPError xmlns='urn:schemas-upnp-org:control-1-0'>
          <errorCode>401</errorCode><errorDescription>Invalid Action</errorDescription>
        </UPnPError></detail>
      </s:Fault></s:Body>
    </s:Envelope>"""
    monkeypatch.setattr(dlna.urllib.request, "urlopen", lambda request, timeout: _Response(fault))

    with pytest.raises(dlna.DlnaError) as raised:
        dlna.DlnaClient(renderer).play()

    assert raised.value.action == "Play"
    assert raised.value.code == 401
    assert raised.value.description == "Invalid Action"


def test_discovery_deduplicates_udn_and_skips_bad_device(monkeypatch):
    packets = iter(
        [
            SSDP_RESPONSE,
            SSDP_RESPONSE,
            b"HTTP/1.1 200 OK\r\nLOCATION: http://bad/root.xml\r\n"
            b"USN: uuid:bad::urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n",
        ]
    )

    class _Socket:
        def __init__(self, *args):
            self.sent = []

        def settimeout(self, timeout):
            self.timeout = timeout

        def sendto(self, data, target):
            self.sent.append((data, target))

        def recvfrom(self, size):
            try:
                return next(packets), ("192.168.1.20", 1900)
            except StopIteration:
                raise socket.timeout

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.close()

    monkeypatch.setattr(dlna.socket, "socket", _Socket)

    def fake_urlopen(request, timeout):
        if request.full_url.startswith("http://bad"):
            raise urllib.error.URLError("bad device")
        return _Response(DEVICE_XML)

    monkeypatch.setattr(dlna.urllib.request, "urlopen", fake_urlopen)
    renderers = dlna.discover_renderers(timeout=0.25)

    assert len(renderers) == 1
    assert renderers[0].udn == "uuid:tv-1"


def test_get_position_info_normalizes_time_track_and_uri(monkeypatch):
    renderer = dlna.DlnaRenderer(
        "uuid:tv-1", "电视", "http://tv/root.xml", "http://tv/control", None,
        "urn:schemas-upnp-org:service:AVTransport:1",
    )
    body = b"""<s:Envelope xmlns:s='http://schemas.xmlsoap.org/soap/envelope/'>
      <s:Body><u:GetPositionInfoResponse xmlns:u='urn:schemas-upnp-org:service:AVTransport:1'>
        <Track>2</Track><TrackDuration>01:02:03</TrackDuration>
        <RelTime>00:01:05</RelTime><TrackURI>http://tv/video.m3u8</TrackURI>
      </u:GetPositionInfoResponse></s:Body>
    </s:Envelope>"""
    monkeypatch.setattr(dlna.urllib.request, "urlopen", lambda request, timeout: _Response(body))

    result = dlna.DlnaClient(renderer).get_position_info()

    assert result == {
        "position": 65,
        "duration": 3723,
        "track": 2,
        "uri": "http://tv/video.m3u8",
    }
