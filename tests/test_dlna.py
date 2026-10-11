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
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
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

NESTED_DEVICE_XML = """<root xmlns='urn:schemas-upnp-org:device-1-0'>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <UDN>uuid:parent</UDN>
    <friendlyName>媒体服务器</friendlyName>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
        <controlURL>/parent/avtransport</controlURL>
      </service>
    </serviceList>
    <deviceList>
      <device>
        <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
        <UDN>uuid:renderer-child</UDN>
        <friendlyName>嵌套电视</friendlyName>
        <serviceList>
          <service>
            <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
            <controlURL>/child/avtransport</controlURL>
          </service>
          <service>
            <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
            <controlURL>/child/rendering</controlURL>
          </service>
        </serviceList>
      </device>
    </deviceList>
  </device>
</root>""".encode("utf-8")

NON_RENDERER_DEVICE_XML = """<root xmlns='urn:schemas-upnp-org:device-1-0'>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <UDN>uuid:server-1</UDN>
    <friendlyName>媒体服务器</friendlyName>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
        <controlURL>/server/avtransport</controlURL>
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


def _renderer():
    return dlna.DlnaRenderer(
        "uuid:tv-1",
        "客厅电视",
        "http://192.168.1.20:80/root.xml",
        "http://192.168.1.20:80/control",
        None,
        "urn:schemas-upnp-org:service:AVTransport:1",
    )


def _soap_response(action, fields=""):
    return (
        "<s:Envelope xmlns:s='http://schemas.xmlsoap.org/soap/envelope/'>"
        f"<s:Body><u:{action}Response "
        "xmlns:u='urn:schemas-upnp-org:service:AVTransport:1'>"
        f"{fields}</u:{action}Response></s:Body></s:Envelope>"
    ).encode("utf-8")


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


def test_parse_device_description_uses_only_target_renderer_services():
    renderer = dlna.parse_device_description(
        "http://192.168.1.20:80/root.xml", NESTED_DEVICE_XML
    )
    assert renderer.udn == "uuid:renderer-child"
    assert renderer.friendly_name == "嵌套电视"
    assert renderer.av_transport_control_url == (
        "http://192.168.1.20:80/child/avtransport"
    )
    assert renderer.rendering_control_url == (
        "http://192.168.1.20:80/child/rendering"
    )


def test_parse_device_description_requires_media_renderer_device():
    with pytest.raises(ValueError, match="MediaRenderer"):
        dlna.parse_device_description(
            "http://192.168.1.20:80/root.xml", NON_RENDERER_DEVICE_XML
        )


def test_dlna_time_round_trips_and_invalid_values_return_none():
    assert dlna.format_dlna_time(3723) == "01:02:03"
    assert dlna.parse_dlna_time("01:02:03") == 3723
    assert dlna.parse_dlna_time("") is None
    assert dlna.parse_dlna_time("bad") is None
    assert dlna.parse_dlna_time("01:60:00") is None


def test_required_soap_actions_send_expected_requests(monkeypatch):
    captured = []

    def fake_urlopen(request, timeout):
        captured.append((request, timeout))
        action = request.get_header("Soapaction").split("#", 1)[1].rstrip('"')
        if action == "GetTransportInfo":
            body = "<CurrentTransportState>PLAYING</CurrentTransportState>"
        else:
            body = ""
        return _Response(_soap_response(action, body))

    monkeypatch.setattr(dlna.urllib.request, "urlopen", fake_urlopen)
    client = dlna.DlnaClient(_renderer(), timeout=4)
    client.set_av_transport_uri("http://tv/current.m3u8", "当前集", "application/vnd.apple.mpegurl")
    client.set_next_av_transport_uri("http://tv/next.mp4", "下一集", "video/mp4")
    client.play()
    client.pause()
    client.stop()
    client.seek(65)
    assert client.get_transport_info() == {"state": "PLAYING"}

    actions = []
    for request, timeout in captured:
        assert request.method == "POST"
        assert timeout == 4
        assert request.full_url == _renderer().av_transport_control_url
        assert request.get_header("Content-type") == "text/xml; charset=\"utf-8\""
        action = request.get_header("Soapaction")
        actions.append(action)
        body = request.data.decode("utf-8")
        assert "<InstanceID>0</InstanceID>" in body
        if "SetAVTransportURI\"" in action:
            assert "CurrentURI" in body
            assert "CurrentURIMetaData" in body
            assert "当前集" in body
            assert "application/vnd.apple.mpegurl" in body
        if "SetNextAVTransportURI\"" in action:
            assert "NextURI" in body
            assert "NextURIMetaData" in body
            assert "下一集" in body
            assert "video/mp4" in body
        if "Play\"" in action:
            assert "<Speed>1</Speed>" in body
        if "Seek\"" in action:
            assert "<Unit>REL_TIME</Unit>" in body
            assert "<Target>00:01:05</Target>" in body

    assert actions == [
        '"urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI"',
        '"urn:schemas-upnp-org:service:AVTransport:1#SetNextAVTransportURI"',
        '"urn:schemas-upnp-org:service:AVTransport:1#Play"',
        '"urn:schemas-upnp-org:service:AVTransport:1#Pause"',
        '"urn:schemas-upnp-org:service:AVTransport:1#Stop"',
        '"urn:schemas-upnp-org:service:AVTransport:1#Seek"',
        '"urn:schemas-upnp-org:service:AVTransport:1#GetTransportInfo"',
    ]


def test_soap_fault_exposes_upnp_code_and_description(monkeypatch):
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
        dlna.DlnaClient(_renderer()).play()

    assert raised.value.action == "Play"
    assert raised.value.code == 401
    assert raised.value.description == "Invalid Action"


def test_generic_soap_fault_falls_back_to_faultcode(monkeypatch):
    fault = b"""<s:Envelope xmlns:s='http://schemas.xmlsoap.org/soap/envelope/'>
      <s:Body><s:Fault>
        <faultcode>s:Client</faultcode><faultstring>Invalid Action</faultstring>
      </s:Fault></s:Body>
    </s:Envelope>"""
    monkeypatch.setattr(dlna.urllib.request, "urlopen", lambda request, timeout: _Response(fault))

    with pytest.raises(dlna.DlnaError) as raised:
        dlna.DlnaClient(_renderer()).play()

    assert raised.value.code == "s:Client"
    assert raised.value.description == "Invalid Action"


def test_soap_timeout_is_propagated(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["timeout"] = timeout
        raise socket.timeout("timed out")

    monkeypatch.setattr(dlna.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(socket.timeout):
        dlna.DlnaClient(_renderer(), timeout=3.5).pause()

    assert captured["timeout"] == 3.5


def test_soap_http_error_with_fault_body_becomes_dlna_error(monkeypatch):
    fault = b"""<s:Envelope xmlns:s='http://schemas.xmlsoap.org/soap/envelope/'>
      <s:Body><s:Fault>
        <faultcode>s:Server</faultcode><faultstring>Rejected</faultstring>
      </s:Fault></s:Body>
    </s:Envelope>"""
    error = urllib.error.HTTPError(
        "http://tv/control", 500, "Internal Server Error", {}, _Response(fault)
    )
    monkeypatch.setattr(dlna.urllib.request, "urlopen", lambda request, timeout: (_ for _ in ()).throw(error))

    with pytest.raises(dlna.DlnaError) as raised:
        dlna.DlnaClient(_renderer()).stop()

    assert raised.value.action == "Stop"
    assert raised.value.code == "s:Server"
    assert raised.value.description == "Rejected"


def test_discovery_sends_ssdp_request_with_timeout_and_keeps_fallback_locations(monkeypatch):
    packets = iter(
        [
            SSDP_RESPONSE.replace(
                b"192.168.1.20:80/root.xml", b"bad.example/root.xml"
            ),
            SSDP_RESPONSE,
        ]
    )
    captured_socket = {}

    class _Socket:
        def __init__(self, *args):
            captured_socket["instance"] = self
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
    fetches = []

    def fake_urlopen(request, timeout):
        fetches.append((request.full_url, timeout))
        if request.full_url.startswith("http://bad.example"):
            raise urllib.error.URLError("bad device")
        return _Response(DEVICE_XML)

    monkeypatch.setattr(dlna.urllib.request, "urlopen", fake_urlopen)
    renderers = dlna.discover_renderers(timeout=0.25)

    sent_data, sent_target = captured_socket["instance"].sent[0]
    request = sent_data.decode("ascii")
    assert sent_target == ("239.255.255.250", 1900)
    assert "M-SEARCH * HTTP/1.1\r\n" in request
    assert "MAN: \"ssdp:discover\"\r\n" in request
    assert "MX: 1\r\n" in request
    assert "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n" in request
    assert captured_socket["instance"].timeout == 0.25
    assert fetches == [
        ("http://bad.example/root.xml", 0.25),
        ("http://192.168.1.20:80/root.xml", 0.25),
    ]
    assert len(renderers) == 1
    assert renderers[0].udn == "uuid:tv-1"


def test_discovery_rejects_wrong_st_but_accepts_missing_st_for_compatibility(monkeypatch):
    wrong_st = SSDP_RESPONSE.replace(
        b"urn:schemas-upnp-org:device:MediaRenderer:1",
        b"urn:schemas-upnp-org:device:MediaServer:1",
        1,
    )
    missing_st = SSDP_RESPONSE.replace(
        b"ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n", b""
    ).replace(b"uuid:renderer-1", b"uuid:renderer-missing")
    packets = iter([wrong_st, missing_st])
    fetches = []

    class _Socket:
        def __init__(self, *args):
            pass

        def settimeout(self, timeout):
            self.timeout = timeout

        def sendto(self, data, target):
            pass

        def recvfrom(self, size):
            try:
                return next(packets), ("192.168.1.20", 1900)
            except StopIteration:
                raise socket.timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            pass

    monkeypatch.setattr(dlna.socket, "socket", _Socket)

    def fake_urlopen(request, timeout):
        fetches.append((request.full_url, timeout))
        return _Response(DEVICE_XML)

    monkeypatch.setattr(dlna.urllib.request, "urlopen", fake_urlopen)

    renderers = dlna.discover_renderers(timeout=0.5)

    assert len(renderers) == 1
    assert fetches == [("http://192.168.1.20:80/root.xml", 0.5)]


def test_get_position_info_normalizes_values_and_sends_expected_request(monkeypatch):
    body = b"""<s:Envelope xmlns:s='http://schemas.xmlsoap.org/soap/envelope/'>
      <s:Body><u:GetPositionInfoResponse xmlns:u='urn:schemas-upnp-org:service:AVTransport:1'>
        <Track>2</Track><TrackDuration>01:02:03</TrackDuration>
        <RelTime>00:01:05</RelTime><TrackURI>http://tv/video.m3u8</TrackURI>
      </u:GetPositionInfoResponse></s:Body>
    </s:Envelope>"""
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response(body)

    monkeypatch.setattr(dlna.urllib.request, "urlopen", fake_urlopen)

    result = dlna.DlnaClient(_renderer(), timeout=2.75).get_position_info()

    request = captured["request"]
    request_body = request.data.decode("utf-8")
    assert request.full_url == _renderer().av_transport_control_url
    assert request.method == "POST"
    assert request.get_header("Soapaction") == (
        '"urn:schemas-upnp-org:service:AVTransport:1#GetPositionInfo"'
    )
    assert request.get_header("Content-type") == "text/xml; charset=\"utf-8\""
    assert "<InstanceID>0</InstanceID>" in request_body
    assert captured["timeout"] == 2.75
    assert result == {
        "position": 65,
        "duration": 3723,
        "track": 2,
        "uri": "http://tv/video.m3u8",
    }


def _rendering_renderer():
    return dlna.parse_device_description(
        "http://192.168.1.20:80/root.xml", DEVICE_XML
    )


def test_set_volume_routes_to_rendering_control_service(monkeypatch):
    """E2：音量动作必须投到 RenderingControl 控制点（而非 AVTransport）。"""
    renderer = _rendering_renderer()
    captured = []

    def fake_urlopen(request, timeout):
        captured.append(request)
        return _Response(_soap_response("SetVolume"))

    monkeypatch.setattr(dlna.urllib.request, "urlopen", fake_urlopen)
    dlna.DlnaClient(renderer, timeout=4).set_volume(42)

    request = captured[0]
    assert request.full_url == renderer.rendering_control_url
    assert request.full_url != renderer.av_transport_control_url
    assert request.get_header("Soapaction") == (
        '"urn:schemas-upnp-org:service:RenderingControl:1#SetVolume"'
    )
    body = request.data.decode("utf-8")
    assert "<InstanceID>0</InstanceID>" in body
    assert "<Channel>Master</Channel>" in body
    assert "<DesiredVolume>42</DesiredVolume>" in body


def test_get_volume_parses_current_volume(monkeypatch):
    renderer = _rendering_renderer()
    monkeypatch.setattr(
        dlna.urllib.request, "urlopen",
        lambda request, timeout: _Response(
            _soap_response("GetVolume", "<CurrentVolume>33</CurrentVolume>")),
    )
    assert dlna.DlnaClient(renderer).get_volume() == 33


def test_set_volume_without_rendering_control_raises_unsupported(monkeypatch):
    """E2：设备无 RenderingControl 服务 → 受控失败（不发起网络请求）。"""
    def _boom(request, timeout):
        raise AssertionError("不应发起 SOAP 请求")

    monkeypatch.setattr(dlna.urllib.request, "urlopen", _boom)
    client = dlna.DlnaClient(_renderer())
    with pytest.raises(dlna.DlnaError) as raised:
        client.set_volume(50)
    assert raised.value.action == "SetVolume"
    assert raised.value.code == "unsupported"


@pytest.mark.parametrize("level", [-1, 101, "loud", None, 3.5])
def test_set_volume_rejects_invalid_level_without_request(monkeypatch, level):
    def _boom(request, timeout):
        raise AssertionError("非法音量不应发起 SOAP 请求")

    monkeypatch.setattr(dlna.urllib.request, "urlopen", _boom)
    with pytest.raises(ValueError):
        dlna.DlnaClient(_rendering_renderer()).set_volume(level)
