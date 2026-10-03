# DLNA 投屏 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在播放界面新增 DLNA 投屏模式，把当前集安全投到同一局域网智能电视，并支持 App 与电视遥控器双向控制。

**Architecture:** 保留现有本地 VLC 和 `127.0.0.1` 代理不变，给 `MediaProxy` 增加投屏专用局域网端点。新增无 Qt 依赖的 `framework/dlna.py` 负责 SSDP 发现和 DLNA SOAP，`video_view.py` 负责 Qt 状态机、设备选择、控制路由和状态轮询。

**Tech Stack:** Python 标准库 `socket`、`urllib`、`xml.etree.ElementTree`、`http.server`；PySide6 `QDialog`、`QThreadPool`、Qt signals；现有 `MediaProxy`、`Content` 和外部 VLC 集成。

## Global Constraints

- 不改变现有本地 VLC 播放逻辑和默认 `127.0.0.1` 代理行为。
- 不新增第三方 DLNA 依赖。
- 所有 SSDP、SOAP、电视状态轮询必须在后台线程执行。
- 只有电视确认进入 `PLAYING` 后才关闭本地 VLC。
- 投屏失败时必须保留本地 VLC。
- 局域网端点绑定指定本机局域网 IP，不绑定 `0.0.0.0`。
- HLS m3u8、分片、密钥地址必须继续通过局域网代理可达。
- 每个任务必须先写失败测试，再做最小实现，再运行直接测试。

---

### Task 1: 建立 DLNA 协议基础与测试替身

**Files:**
- Create: `framework/dlna.py`
- Test: `tests/test_dlna.py`

**Interfaces:**
- Produces `DlnaRenderer`, `discover_renderers()`, `DlnaClient`，供后续 GUI 任务调用。
- `DlnaClient` 不依赖 Qt，不维护 GUI 状态。

- [ ] **Step 1: 写 SSDP 响应解析失败测试**

```python
def test_parse_ssdp_response_extracts_location_and_usn():
    response = (
        b"HTTP/1.1 200 OK\\r\\n"
        b"LOCATION: http://192.168.1.20:80/root.xml\\r\\n"
        b"ST: urn:schemas-upnp-org:device:MediaRenderer:1\\r\\n"
        b"USN: uuid:renderer-1::urn:schemas-upnp-org:device:MediaRenderer:1\\r\\n\\r\\n"
    )
    parsed = parse_ssdp_response(response)
    assert parsed["location"] == "http://192.168.1.20:80/root.xml"
    assert parsed["usn"] == "uuid:renderer-1::urn:schemas-upnp-org:device:MediaRenderer:1"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_dlna.py::test_parse_ssdp_response_extracts_location_and_usn -q`
Expected: FAIL because `framework.dlna` and `parse_ssdp_response` do not exist.

- [ ] **Step 3: 写最小协议模块**

实现以下内容：

```python
@dataclass(frozen=True)
class DlnaRenderer:
    udn: str
    friendly_name: str
    location: str
    av_transport_control_url: str
    rendering_control_url: str | None
    service_type: str


def parse_ssdp_response(data: bytes) -> dict[str, str]:
    raise NotImplementedError


def discover_renderers(timeout: float = 2.0) -> list[DlnaRenderer]:
    raise NotImplementedError
```

SSDP 使用 `socket.AF_INET` UDP，目标 `239.255.255.250:1900`，请求包含 `MAN: "ssdp:discover"`、`MX: 1` 和 MediaRenderer `ST`。所有 socket 设置超时，单个设备失败不能中断整体发现。

- [ ] **Step 4: 添加设备描述解析测试**

```python
def test_parse_device_description_finds_av_transport_service():
    xml = """<root xmlns='urn:schemas-upnp-org:device-1-0'>
      <device><UDN>uuid:tv-1</UDN><friendlyName>客厅电视</friendlyName>
        <serviceList><service>
          <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
          <controlURL>/upnp/control/avtransport</controlURL>
        </service></serviceList>
      </device>
    </root>"""
    renderer = parse_device_description(
        "http://192.168.1.20:80/root.xml", xml.encode("utf-8"))
    assert renderer.friendly_name == "客厅电视"
    assert renderer.av_transport_control_url.endswith("/upnp/control/avtransport")
```

- [ ] **Step 5: 实现设备描述、SOAP 和时间解析**

实现：

```python
def parse_device_description(location: str, body: bytes) -> DlnaRenderer:
    raise NotImplementedError


def format_dlna_time(seconds: int) -> str:
    raise NotImplementedError


def parse_dlna_time(value: str) -> int | None:
    raise NotImplementedError


class DlnaClient:
    def set_av_transport_uri(self, uri: str, title: str, mime_type: str) -> None:
        raise NotImplementedError

    def set_next_av_transport_uri(self, uri: str, title: str, mime_type: str) -> None:
        raise NotImplementedError

    def play(self) -> None:
        raise NotImplementedError

    def pause(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def seek(self, seconds: int) -> None:
        raise NotImplementedError

    def get_transport_info(self) -> dict:
        raise NotImplementedError

    def get_position_info(self) -> dict:
        raise NotImplementedError
```

SOAP 请求使用 `urllib.request.Request`，设置 `Content-Type: text/xml; charset="utf-8"`、正确 `SOAPACTION`、连接/读取超时。SOAP Fault 转换为 `DlnaError(action, code, description)`。

- [ ] **Step 6: 运行协议测试确认通过**

Run: `python -m pytest tests/test_dlna.py -q`
Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add framework/dlna.py tests/test_dlna.py
git commit -m "feat(dlna): add renderer discovery and SOAP controls"
```

---

### Task 2: 增加投屏专用局域网代理端点

**Files:**
- Modify: `framework/media_proxy.py:1195-1213, 1345-1355, 2000-2081`
- Create: `tests/test_media_proxy_lan.py`

**Interfaces:**
- Consumes: `DlnaRenderer` 的 IP 或显式 `bind_host`。
- Produces:

```python
def start_lan_endpoint(self, bind_host: str) -> str:
    raise NotImplementedError


def stop_lan_endpoint(self) -> None:
    raise NotImplementedError


def build_url(
    self,
    target_url: str,
    headers: dict | None = None,
    ad_block: dict | None = None,
    force_proxy: bool = False,
    public_base: str | None = None,
) -> str:
    raise NotImplementedError


def release_lan_tokens(self) -> None:
    raise NotImplementedError
```

- [ ] **Step 1: 写默认行为和局域网 URL 失败测试**

```python
def test_build_url_keeps_localhost_by_default(proxy_ctx):
    url = proxy_ctx.build_url("https://cdn.example/v.mp4")
    assert "127.0.0.1" in url


def test_build_url_uses_lan_base_when_requested(proxy_ctx):
    proxy_ctx.start_lan_endpoint("127.0.0.1")
    url = proxy_ctx.build_url(
        "https://cdn.example/v.mp4", public_base="http://127.0.0.1:45678")
    assert url.startswith("http://127.0.0.1:45678/s/")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_media_proxy_lan.py -q`
Expected: FAIL because the LAN endpoint and `public_base` parameter do not exist.

- [ ] **Step 3: 实现并行 HTTP 端点**

在 `MediaProxy` 增加 `_lan_server`、`_lan_thread`、`_lan_base`、`_lan_tokens`。使用相同 `_ProxyHandler` 和同一个 `MediaProxy` 实例：

```python
server = ThreadingHTTPServer((bind_host, 0), _ProxyHandler)
server.proxy = self
server.public_base = f"http://{bind_host}:{server.server_address[1]}"
```

当 `build_url` 传入非空 `public_base` 创建 token 时加入 `_lan_tokens`；m3u8 派生的 `/s/` 和 `/c/` token 也加入该集合。不要改动现有 `_server` 的 `127.0.0.1` 绑定。`stop_lan_endpoint()` 只关闭 LAN server 并清理 `_lan_tokens`，不清理本地 server、local tokens 或本地 VLC 租约。

- [ ] **Step 4: 写 m3u8 内部地址测试**

```python
def test_rewritten_m3u8_uses_lan_base_for_segments(proxy_ctx, monkeypatch):
    rewritten = proxy_ctx._rewrite_cache_or_token(
        "segment-1.ts", "https://cdn.example/master.m3u8", {},
        None, False, None, public_base="http://192.168.1.10:45678")
    assert rewritten.startswith("http://192.168.1.10:45678/")
    assert "127.0.0.1" not in rewritten
```

- [ ] **Step 5: 让重写链路携带 `public_base`**

扩展 `_proxy_url()`、`_rewrite_cache_or_token()`、`_serve_m3u8_text()` 等内部接口。默认 `public_base=None` 时继续使用本地 base；来自 LAN handler 的请求使用 `handler.server.public_base`。所有 `/s/`、`/c/`、分片、密钥 URL 都必须通过这个 base 生成。

- [ ] **Step 6: 运行代理测试与既有媒体代理测试**

Run: `python -m pytest tests/test_media_proxy_lan.py tests/test_media_proxy_series.py -q`
Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add framework/media_proxy.py tests/test_media_proxy_lan.py
git commit -m "feat(media-proxy): expose scoped LAN endpoint for casting"
```

---

### Task 3: 实现 CastSession 与后台轮询

**Files:**
- Create: `framework/cast_session.py`
- Test: `tests/test_cast_session.py`

**Interfaces:**
- Consumes: `DlnaClient`、`MediaProxy`、已取到的当前集流地址。
- Produces:

```python
class CastSession:
    def __init__(self, proxy, client_factory, on_local_player_stop=None):
        raise NotImplementedError

    def start(self, renderer, episode_index: int, media_url, title,
              headers, ad_block, force_proxy) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def play(self) -> None:
        raise NotImplementedError

    def pause(self) -> None:
        raise NotImplementedError

    def seek(self, seconds: int) -> None:
        raise NotImplementedError

    def set_episode(self, episode_index: int, media_url, title,
                    headers, ad_block, force_proxy) -> None:
        raise NotImplementedError

    def poll_state(self) -> dict:
        raise NotImplementedError
```

- [ ] **Step 1: 写启动顺序失败测试**

```python
class FakeRenderer:
    def __init__(self, states):
        self.states = iter(states)
        self.calls = []

    def set_av_transport_uri(self, uri, metadata):
        self.calls.append(("set_uri", uri))

    def set_next_av_transport_uri(self, uri, metadata):
        self.calls.append(("set_next_uri", uri))

    def play(self):
        self.calls.append(("play",))

    def pause(self):
        self.calls.append(("pause",))

    def stop(self):
        self.calls.append(("stop",))

    def seek(self, seconds):
        self.calls.append(("seek", seconds))

    def get_transport_info(self):
        return {"state": next(self.states)}

    def get_position_info(self):
        return {"position": 0, "duration": 120}


class FakeProxy:
    def __init__(self):
        self.stopped = False

    def start_lan_endpoint(self, bind_host):
        return "http://192.168.1.10:45678"

    def build_url(self, target_url, **kwargs):
        return "http://192.168.1.10:45678/s/cast-token"

    def stop_lan_endpoint(self):
        self.stopped = True


def make_session(renderer, on_local_player_stop=None):
    proxy = FakeProxy()
    session = CastSession(
        proxy=proxy,
        client_factory=lambda _renderer: renderer,
        on_local_player_stop=on_local_player_stop,
    )
    session.proxy = proxy
    return session


def test_cast_session_closes_local_player_only_after_playing(monkeypatch):
    events = []
    renderer = FakeRenderer(states=["TRANSITIONING", "PLAYING"])
    session = CastSession(
        proxy=FakeProxy(),
        client_factory=lambda r: renderer,
        on_local_player_stop=lambda: events.append("local-stop"),
    )
    session.start(renderer, 0, "https://cdn/v.mp4", "第1集", {}, {}, False)
    assert events == []
    session.poll_state()
    assert events == []
    session.poll_state()
    assert events == ["local-stop"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_cast_session.py::test_cast_session_closes_local_player_only_after_playing -q`
Expected: FAIL because `CastSession` does not exist.

- [ ] **Step 3: 实现最小 CastSession**

启动逻辑严格为：

```text
获取出站 IP
→ start_lan_endpoint
→ build_url(public_base=lan_base)
→ SetAVTransportURI
→ Play
→ 状态轮询确认 PLAYING
→ on_local_player_stop
```

`CastSession` 持有 renderer、client、LAN endpoint、current index、current URL 和 `active` 状态。所有异常清理 LAN endpoint，并通过异常返回给 GUI；不得自动停止本地 VLC。

- [ ] **Step 4: 添加状态、Seek 和切集测试**

覆盖以下可执行断言：

```python
def test_pause_and_seek_delegate_to_dlna():
    renderer = FakeRenderer(states=["PLAYING"])
    session = make_session(renderer)
    session.pause()
    session.seek(42)
    assert renderer.calls[-2:] == [("pause",), ("seek", 42)]


def test_set_episode_sets_uri_then_plays():
    renderer = FakeRenderer(states=["PLAYING"])
    session = make_session(renderer)
    session.set_episode(2, "https://cdn/e3.mp4", "第3集", {}, {}, False)
    assert renderer.calls[-2:] == [("set_uri", "http://192.168.1.10:45678/s/cast-token"),
                                    ("play",)]
    assert session.current_episode_index == 2


def test_start_failure_keeps_local_player_running():
    renderer = FakeRenderer(states=["STOPPED"])
    stopped = []
    session = make_session(renderer, on_local_player_stop=lambda: stopped.append(True))
    session.start(renderer, 0, "https://cdn/e1.mp4", "第1集", {}, {}, False)
    session.poll_state()
    assert stopped == []


def test_stop_releases_lan_endpoint():
    renderer = FakeRenderer(states=["PLAYING"])
    session = make_session(renderer)
    session.stop()
    assert session.proxy.stopped is True
```

- [ ] **Step 5: 运行 CastSession 测试**

Run: `python -m pytest tests/test_cast_session.py -q`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add framework/cast_session.py tests/test_cast_session.py
git commit -m "feat(cast): add DLNA session lifecycle and state polling"
```

---

### Task 4: 增加投屏设备选择对话框

**Files:**
- Create: `gui/components/cast_dialog.py`
- Test: `tests/test_cast_dialog.py`

**Interfaces:**
- Consumes: `discover_renderers()`。
- Produces: 选中的 `DlnaRenderer` 或取消结果。

- [ ] **Step 1: 写设备列表和空状态测试**

在测试文件中先定义：

```python
def fake_renderer(name):
    return DlnaRenderer(
        udn=f"uuid:{name}",
        friendly_name=name,
        location="http://192.168.1.20:80/root.xml",
        av_transport_control_url="http://192.168.1.20:80/upnp/control/avtransport",
        rendering_control_url=None,
        service_type="urn:schemas-upnp-org:service:AVTransport:1",
    )
```

并让 `CastDialog.device_names()` 返回列表中的 `friendly_name`，`empty_message()` 返回空列表时的提示文本。

```python
def test_dialog_lists_renderer_names(qapp):
    dialog = CastDialog([fake_renderer("客厅电视"), fake_renderer("卧室盒子")])
    assert dialog.device_names() == ["客厅电视", "卧室盒子"]


def test_dialog_shows_same_lan_hint_when_empty(qapp):
    dialog = CastDialog([])
    assert "同一局域网" in dialog.empty_message()
```

- [ ] **Step 2: 实现对话框**

沿用 `gui/components/source_select_dialog.py` 的 Qt 组件风格：

- 标题「选择投屏设备」；
- 显示当前集标题；
- 设备列表、刷新、取消；
- 发现操作通过后台 worker 执行，不在 Qt 主线程发 UDP 或 HTTP；
- 设备为空时给出同网和防火墙提示。

- [ ] **Step 3: 运行对话框测试**

Run: `python -m pytest tests/test_cast_dialog.py -q`
Expected: PASS。

- [ ] **Step 4: Commit**

```bash
git add gui/components/cast_dialog.py tests/test_cast_dialog.py
git commit -m "feat(cast): add renderer selection dialog"
```

---

### Task 5: 接入播放界面和双向控制

**Files:**
- Modify: `gui/pages/reader/video_view.py:552-606, 650-665, 815-850, 1420-1535, 1541-1605, 1820-1860`
- Test: `tests/test_video_cast.py`

**Interfaces:**
- Consumes: `CastSession`、`CastDialog`、`DlnaRenderer`。
- Produces: `cast_btn`、投屏状态机、Qt 控制路由。

- [ ] **Step 1: 写当前集和失败回退测试**

```python
def test_cast_uses_current_episode_index(qapp, monkeypatch):
    view = make_video_view(current_idx=2)
    fake_session = FakeCastSession()
    install_cast(view, fake_session)
    view._start_cast()
    assert fake_session.started_with.index == 2


def test_cast_failure_does_not_stop_local_vlc(qapp, monkeypatch):
    view = make_video_view(current_idx=1)
    stopped = []
    monkeypatch.setattr(view, "_stop_local_player", lambda: stopped.append(True))
    install_cast_failure(view)
    view._start_cast()
    assert stopped == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_video_cast.py -q`
Expected: FAIL because the cast button, state machine and routing do not exist.

- [ ] **Step 3: 增加控制条投屏按钮**

在换源按钮附近增加 `self.cast_btn`：

- idle：`📺` / 「投屏」；
- active：`📺` / 「停止投屏」；
- 只有存在当前 source、episode 和可播放状态时启用；
- 单集、多集都支持，目标始终为 `_current_idx`。

- [ ] **Step 4: 接入投屏启动流程**

`_start_cast()`：

1. 打开设备选择对话框；
2. 读取 `_stream_cache[(ep.url, quality)]`，无缓存则复用现有 `_FetchStreamTask`；
3. 构造 `CastSession`；
4. 传入当前集标题、流地址、请求头、`ad_block`、`force_proxy`；
5. 等待 session 信号；
6. 只有收到 `PLAYING` 才调用公共的本地播放器停止入口；
7. 设置 `_cast_active=True` 并刷新控件。

- [ ] **Step 5: 接入 App 控制和电视状态同步**

投屏模式下：

```text
play_btn → CastSession.play/pause
progress.sliderReleased → CastSession.seek
prev/next → 获取目标集流 → CastSession.set_episode
```

后台每 1 秒轮询 `poll_state()`，通过 Qt signal 更新播放按钮、进度条、时间标签和当前集高亮。外部 VLC 模式保持原有禁用逻辑，不与投屏模式混用。

- [ ] **Step 6: 接入生命周期清理**

以下路径调用 `_stop_cast()`：

- 切换作品；
- 切换源；
- 关闭视频页；
- 应用退出；
- 用户点击「停止投屏」；
- DLNA 连续失败 3 次。

清理顺序：停止轮询 → Stop/释放 DLNA session → 注销投屏 token → 关闭 LAN endpoint → 清理 UI 状态。不要自动重启本地 VLC。

- [ ] **Step 7: 运行视频界面测试**

Run: `python -m pytest tests/test_video_cast.py tests/test_video_series_playlist.py -q`
Expected: PASS。

- [ ] **Step 8: Commit**

```bash
git add gui/pages/reader/video_view.py tests/test_video_cast.py
git commit -m "feat(video): add DLNA casting controls to player view"
```

---

### Task 6: 增加媒体兼容性与回归验证

**Files:**
- Modify: `tests/test_media_proxy_lan.py`
- Modify: `tests/test_dlna.py`
- Modify: `tests/test_video_cast.py`

**Interfaces:**
- Consumes all previous task interfaces.
- Produces a verified end-to-end feature without changing local VLC behavior.

- [ ] **Step 1: 添加 mp4 和 HLS 代理 URL 回归测试**

```python
def test_cast_mp4_url_is_lan_reachable(proxy_ctx):
    base = proxy_ctx.start_lan_endpoint("127.0.0.1")
    url = proxy_ctx.build_url(
        "https://cdn.example/v.mp4", public_base=base)
    assert url.startswith(base + "/s/")


def test_cast_hls_manifest_rewrites_segments_to_lan_base(proxy_ctx):
    base = "http://192.168.1.10:45678"
    rewritten = proxy_ctx._rewrite_cache_or_token(
        "segment-1.ts", "https://cdn.example/master.m3u8", {},
        None, False, None, public_base=base)
    assert rewritten.startswith(base + "/")
    assert "127.0.0.1" not in rewritten


def test_local_vlc_url_remains_localhost(proxy_ctx):
    url = proxy_ctx.build_url("https://cdn.example/v.mp4")
    assert "127.0.0.1" in url
```

- [ ] **Step 2: 添加 DLNA action 回归测试**

验证 `SetAVTransportURI`、`Play`、`Pause`、`Seek` 的 SOAP action、service type、超时和 Fault 解析。

- [ ] **Step 3: 添加 GUI 状态回归测试**

验证：

- 投屏成功后本地 VLC 才停止；
- 投屏失败时本地 VLC 保持；
- 电视遥控器暂停/播放状态能同步；
- App 切换下一集不重启本地 VLC；
- 结束投屏后端口和 token 清理。

- [ ] **Step 4: 运行目标测试集**

Run: `python -m pytest tests/test_dlna.py tests/test_media_proxy_lan.py tests/test_cast_session.py tests/test_cast_dialog.py tests/test_video_cast.py -q`
Expected: PASS。

- [ ] **Step 5: 运行完整回归**

Run: `python -m pytest tests -q`
Expected: 新增测试全部通过；既有失败数量不增加。记录当前既有失败基线，并确认 `app_config.json` 未被测试污染。

- [ ] **Step 6: 手工验收**

在同一局域网真实电视上验证：

1. dm84 mp4 投屏；
2. omofuna HLS 投屏；
3. App 播放/暂停/Seek/下一集；
4. 电视遥控器播放/暂停/Seek；
5. 投屏失败和电视断网时本地 VLC 保持；
6. Windows 防火墙阻断时错误提示明确。

- [ ] **Step 7: Commit**

```bash
git add tests/test_dlna.py tests/test_media_proxy_lan.py tests/test_cast_session.py tests/test_cast_dialog.py tests/test_video_cast.py
git commit -m "test(cast): verify DLNA playback and proxy compatibility"
```

## Verification Checklist

- [ ] 本地 VLC 未投屏时行为和地址保持不变。
- [ ] 电视能访问 LAN `/s/` URL。
- [ ] m3u8 内部 URL 不再指向 `127.0.0.1`。
- [ ] App 与电视遥控器状态能双向同步。
- [ ] 投屏失败不关闭本地 VLC。
- [ ] 投屏成功后本地 VLC 停止。
- [ ] 关闭投屏清理 token、端口和轮询线程。
- [ ] 全量测试无新增失败。
