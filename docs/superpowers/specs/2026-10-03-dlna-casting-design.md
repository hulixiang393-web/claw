# DLNA 投屏设计

日期：2026-10-03
状态：用户已批准暂定方案 A
相关区域：`gui/pages/reader/video_view.py`、`framework/media_proxy.py`、新增 `framework/dlna.py`

## 1. 背景与目标

用户希望在播放界面把当前正在播放的集投到同一局域网内的智能电视或盒子，并同时支持 App 控制和电视遥控器控制。投屏成功后停止电脑本地 VLC，只保留电视播放。

目标：

1. 控制条增加投屏入口，默认投当前选中集 `self._current_idx`。
2. 支持 DLNA/UPnP MediaRenderer 设备发现。
3. App 支持播放、暂停、停止、Seek、上一集、下一集。
4. 电视遥控器的播放状态和进度同步回 App。
5. 复用现有媒体代理的防盗链请求头、HLS 重写、广告过滤、缓存和取流逻辑。
6. 投屏失败时不影响当前本地 VLC；只有电视确认进入 `PLAYING` 后才关闭本地 VLC。
7. 不改变现有本地 VLC 播放链路和默认地址。

非目标：

- 不实现 Chromecast、AirPlay、Miracast。
- 不支持跨公网投屏。
- 不支持多台电视同时播放。
- 不新增投屏历史和账号绑定。
- 不要求所有电视都支持 HLS；电视拒绝格式时应明确提示并保留本地播放。

## 2. 已确认决策

1. 投屏协议：DLNA/UPnP。
2. 控制模式：App 与电视遥控器双向控制。
3. 投屏入口：播放界面控制条按钮。
4. 投屏目标：当前选中/正在播放的集。
5. 本地行为：电视确认播放后停止本地 VLC。
6. 网络方案：使用投屏专用局域网代理端口，不改变本地 `127.0.0.1` 代理。
7. 依赖策略：优先使用 Python 标准库实现 SSDP、SOAP、XML 解析，不新增第三方 DLNA 依赖。

## 3. 现有架构约束

### 3.1 本地代理

`framework/media_proxy.py:1352` 的 `MediaProxy` 当前通过 `ThreadingHTTPServer(("127.0.0.1", 0), ...)` 提供本地媒体代理。`build_url()` 在 `:1195` 返回 `http://127.0.0.1:<port>/s/<token>`。

代理已经具备：

- token 化媒体 URL；
- 上游 `Referer`、`User-Agent` 等请求头保存；
- mp4 Range/206 透传；
- m3u8 重写；
- 广告段过滤；
- 本地媒体缓存；
- HLS 分片预取。

### 3.2 外部播放器

`gui/pages/reader/video_view.py:552-606` 构造播放控制条。外部 VLC 模式下，App 自身的进度、音量等控件当前被禁用，操作交给 VLC。

投屏模式是独立模式：不修改外部 VLC 的现有行为；仅在投屏会话存在时重新启用需要的控制项，并将操作路由到 DLNA。

## 4. 总体架构

```text
video_view._on_cast_clicked()
  ├─ DlnaDiscovery.discover()
  ├─ CastDeviceDialog 选择 MediaRenderer
  ├─ 获取当前集流地址（复用 _stream_cache / _FetchStreamTask）
  ├─ MediaProxy.start_lan_endpoint(local_ip)
  ├─ MediaProxy.build_url(..., public_base=lan_base)
  ├─ DlnaClient.set_av_transport_uri(lan_url, metadata)
  ├─ DlnaClient.play()
  ├─ CastPollWorker 确认 PLAYING
  └─ stop_local_player()

CastPollWorker
  ├─ GetTransportInfo
  ├─ GetPositionInfo
  └─ Qt signal → VideoView 更新状态、进度和当前集
```

### 4.1 投屏专用代理端点

不把现有本地服务器改成 `0.0.0.0`，而是在 `MediaProxy` 中增加一个与本地服务器并行的局域网端点：

```python
lan_base = proxy.start_lan_endpoint(bind_host="192.168.1.10")
url = proxy.build_url(
    target_url,
    headers=headers,
    ad_block=ad_block,
    force_proxy=force_proxy,
    public_base=lan_base,
)
proxy.stop_lan_endpoint()
```

设计要求：

- 局域网端点与本地端点共享同一个 `MediaProxy`、token 表、系列表和缓存对象。
- `build_url()` 默认行为不变，仍返回 `127.0.0.1` 地址。
- `public_base` 非空时只改变生成 URL 的主机和端口，不改变 token 内容和上游逻辑，并把 token 记入投屏 token 集合。
- m3u8 内部 `/s/`、`/c/`、分片及密钥 URL 必须使用收到请求对应端点的局域网 base，不能硬编码 `127.0.0.1`；这些派生 token 也必须记入同一投屏 token 集合。
- 局域网端点绑定指定本机局域网 IP，不直接绑定 `0.0.0.0`。
- 端点停止前等待活动请求结束；停止后只注销投屏 token，不清理本地 VLC 正在使用的 token。
- 若绑定失败、没有有效局域网 IP 或 Windows 防火墙阻断，向 UI 返回明确错误。

新增或扩展接口建议：

```python
def start_lan_endpoint(self, bind_host: str) -> str:
    """启动投屏端点并返回 http://<lan_ip>:<port>。"""

def stop_lan_endpoint(self) -> None:
    """停止投屏端点，不影响本地端点。"""

def build_url(
    self,
    target_url: str,
    headers: dict | None = None,
    ad_block: dict | None = None,
    force_proxy: bool = False,
    public_base: str | None = None,
) -> str:
    """默认生成本地 URL；传 public_base 时生成局域网 URL并登记投屏 token。"""

def release_lan_tokens(self) -> None:
    """只清理投屏端点生成的 token，不影响本地 VLC token。"""
```

`_rewrite_cache_or_token()`、`_proxy_url()`、`_serve_m3u8_text()` 和相关内部重写方法必须接收当前请求端点的 `public_base`，这样电视获取 m3u8 后继续请求的所有资源仍可达。

### 4.2 局域网地址选择

根据已选电视的 IP 选择出站网卡：

```python
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.connect((renderer_ip, 1900))
local_ip = sock.getsockname()[0]
```

此连接只用于系统路由选择，不发送媒体数据。若电视地址无法解析，则回退到本机非回环 IPv4 地址列表；多个地址时由用户在错误提示中看到需要保证电视和电脑处于同一局域网。

## 5. DLNA 模块设计

新增 `framework/dlna.py`，只负责协议和网络，不直接依赖 Qt。

### 5.1 设备发现

```python
@dataclass(frozen=True)
class DlnaRenderer:
    udn: str
    friendly_name: str
    location: str
    av_transport_control_url: str
    rendering_control_url: str | None
    service_type: str


def discover_renderers(timeout: float = 2.0) -> list[DlnaRenderer]:
    """通过 SSDP M-SEARCH 发现局域网 MediaRenderer。"""
```

实现：

1. 向 `239.255.255.250:1900` 发送 `M-SEARCH * HTTP/1.1`。
2. 使用 `ST: urn:schemas-upnp-org:device:MediaRenderer:1`，必要时再发送 `upnp:rootdevice` 兜底。
3. 解析响应中的 `LOCATION`，下载 device description XML。
4. 找到 `AVTransport:1` 控制服务和可选 `RenderingControl:1` 服务。
5. 使用 UDN 去重，保留 friendly name、IP、控制 URL。
6. 单设备请求失败不影响其它设备返回。

### 5.2 控制接口

```python
class DlnaClient:
    def set_av_transport_uri(self, uri: str, title: str, mime_type: str) -> None: ...
    def set_next_av_transport_uri(self, uri: str, title: str, mime_type: str) -> None: ...
    def play(self) -> None: ...
    def pause(self) -> None: ...
    def stop(self) -> None: ...
    def seek(self, seconds: int) -> None: ...
    def get_transport_info(self) -> dict: ...
    def get_position_info(self) -> dict: ...
```

SOAP 请求要求：

- `SOAPACTION` 使用设备实际 service type，不写死厂商扩展值。
- XML 使用 `xml.etree.ElementTree` 生成和解析。
- SOAP 错误转换为带 action、错误码和错误描述的 Python 异常。
- 网络请求全部设置连接和读取超时，不能阻塞 Qt 主线程。
- `RelTime` / `TrackDuration` 转换为秒；无法解析时使用 `None`。

DIDL-Lite metadata 至少包含：

- `dc:title`：当前集标题；
- `upnp:class`：`object.item.videoItem`；
- `res protocolInfo`：按 `.m3u8` 使用 `application/vnd.apple.mpegurl`，其它媒体默认 `video/mp4`，若可从响应头判断则优先使用响应头；
- `res`：局域网代理 URL。

### 5.3 双向状态同步

`video_view.py` 使用 `QThreadPool` 或现有任务模式启动轮询 worker，每 1 秒获取：

- `CurrentTransportState`：PLAYING、PAUSED_PLAYBACK、STOPPED、TRANSITIONING、NO_MEDIA_PRESENT；
- `RelTime`、`TrackDuration`；
- `Track`、`TrackURI`。

worker 只发 Qt signal，不直接操作控件。主线程根据状态更新：

- 播放/暂停按钮；
- `progress` slider 与 `time_label`；
- 当前集选中状态；
- 投屏设备名和错误状态。

电视遥控器发起的播放/暂停/Seek 通过轮询反映到 App。不同电视对 `NextAVTransportURI` 支持不一致，因此：

- App 的上一集/下一集始终可靠；
- App 尝试设置下一集 URI；
- 电视遥控器下一集仅在设备支持时生效；
- 不伪造上一集状态，设备不支持时显示兼容性提示。

## 6. 播放界面设计

### 6.1 控制条

在 `video_view.py:600` 附近增加 `cast_btn`，位于换源按钮附近：

- 未投屏：图标 `📺`，提示「投屏」；
- 投屏中：图标保持但提示变为「停止投屏」；
- 无源/无当前集：禁用；
- 外部 VLC 模式：按钮仍可用；
- 投屏模式：启用播放、暂停、进度、上一集、下一集控制；音量仅在发现 `RenderingControl` 时启用，否则保持禁用。

### 6.2 设备选择对话框

新增 `gui/components/cast_dialog.py`，沿用现有 `source_select_dialog.py` 的组件风格：

- 标题：「选择投屏设备」；
- 顶部显示当前集标题；
- 列表显示电视 friendly name 和 IP；
- 「刷新」重新 SSDP 扫描；
- 「取消」关闭；
- 设备列表为空时显示「请确认电脑和电视处于同一局域网，并允许应用通过 Windows 防火墙」。

### 6.3 投屏状态

投屏状态分为：

```text
idle
scanning
preparing_stream
connecting
playing
paused
stopped
error
```

状态变化只通过主线程槽更新 UI。显示内容包括：

- 设备名；
- 当前集标题；
- 「正在准备播放流」「正在连接电视」「投屏中」「投屏失败」等短消息；
- 错误时提供「重试」和「关闭投屏」操作。

### 6.4 投屏启动与本地 VLC 关闭顺序

```text
1. 保留本地 VLC，不立即关闭。
2. 发现设备并获取当前集真实流。
3. 启动局域网代理端点。
4. SetAVTransportURI。
5. Play。
6. 轮询直到 PLAYING 或超时 8 秒。
7. PLAYING：停止本地 VLC，进入 cast_active。
8. 失败：关闭投屏端点，保留本地 VLC，并显示错误。
```

这样电视连接失败不会造成黑屏或丢失本地播放。

### 6.5 App 控制路由

```text
cast_active == True
  ├─ 播放/暂停 → DlnaClient.play/pause
  ├─ 进度条释放 → DlnaClient.seek
  ├─ 上一集/下一集 → 获取缓存或取流 → SetAVTransportURI + Play
  └─ 停止投屏 → DlnaClient.stop + 清理 CastSession

cast_active == False
  └─ 保持现有本地 VLC / 外部播放器逻辑
```

上一集/下一集切换时，先保留当前电视状态；新集 URL 准备失败则继续当前集，不自动关闭投屏。

## 7. 生命周期与错误处理

- 切换作品、切换源、关闭播放页、应用退出时清理 `CastSession`。
- 电视断网或 SOAP 请求连续失败 3 次后标记 `error`，停止轮询并关闭投屏端点。
- 不自动重启本地 VLC，避免用户未要求时重复播放；用户可点击播放重新启动。
- 所有网络操作设置超时，Qt 主线程只处理信号。
- 不记录完整 token URL 和请求头；日志只记录设备名、动作和错误码。
- 端点只在投屏会话期间开放；停止后清理 cast token、series 和端口。

## 8. 测试方案

### 单元测试

- `tests/test_dlna.py`
  - SSDP 响应解析、设备去重、无效 XML 跳过；
  - SOAP action 请求体和错误码解析；
  - 时间格式解析；
  - DIDL-Lite metadata 生成。
- `tests/test_media_proxy_lan.py`
  - 本地 URL 默认仍为 `127.0.0.1`；
  - `public_base` 生成局域网 URL；
  - m3u8 内部资源全部使用局域网 base；
  - 局域网端点停止不清理本地端点；
  - token、请求头和广告过滤仍正确。
- `tests/test_video_cast.py`
  - 点击投屏使用当前 `_current_idx`；
  - 当前流缓存命中时不重复取流；
  - 电视确认 PLAYING 后才停止本地 VLC；
  - DLNA 失败时本地 VLC 不关闭；
  - App 播放/暂停/Seek/切集路由到 DLNA；
  - 轮询状态能更新 UI 信号。

### 手工验收

1. dm84 mp4：发现电视、投当前集、播放、暂停、Seek、App 下一集。
2. omofuna HLS：验证 m3u8 和分片都能从电视访问。
3. 电视遥控器暂停/播放/Seek，App 状态跟随变化。
4. 投屏过程中拔掉电视网络：App 显示错误，本地 VLC 不被误关或自动重启。
5. 投屏失败、Windows 防火墙拒绝、电视不支持 HLS 时，错误提示明确。

## 9. 实现边界

本设计只增加投屏模式，不改本地 VLC 的播放、系列列表、代理 token 和现有取流行为。LAN proxy 只在投屏时启动；投屏停止后恢复原有本地代理生命周期。