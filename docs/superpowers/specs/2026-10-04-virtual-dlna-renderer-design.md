# 虚拟 DLNA 电视与多格式投屏验证设计

日期：2026-10-04
状态：用户已批准
范围：无真实电视的投屏链路、格式兼容性和真实源验证

## 1. 目标

建立一个不依赖第三方硬件的虚拟 DLNA MediaRenderer，实际访问 MediaProxy 生成的媒体 URL，并使用本机 FFmpeg 验证媒体是否能被解码。只有虚拟电视完成实际解码后，才报告 `PLAYING`。

验证目标：

1. 检查 SSDP/SOAP/DIDL/状态轮询/Seek/Pause 链路。
2. 检查 LAN 代理 URL 对虚拟电视可达。
3. 检查 MP4、HLS、DASH、WebM、HEVC 等格式的实际解码结果。
4. 检查不支持格式、失效 URL、超时、无时长时的错误处理。
5. 使用项目真实源进行联网投屏验证，并将网络源失败与代码失败区分。
6. 为后续「电脑端统一转码」提供兼容性证据，不承诺 DRM、设备厂商扩展和所有 HDR 格式均可转换。

## 2. 非目标

- 不模拟真实电视厂商的全部私有行为。
- 不保证 DRM、加密流、受限地理区域资源可播放。
- 不在本任务直接实现生产转码网关。
- 不引入 Python 第三方媒体库；使用已安装或用户显式提供的 FFmpeg 可执行文件。

## 3. 方案

新增 `tools/virtual_dlna_renderer.py`，作为测试工具而非生产 GUI 模块。工具包含：

- `VirtualRenderer`：实现 AVTransport 状态机和 SOAP action。
- `VirtualRendererServer`：本地 HTTP 控制服务，供现有 `DlnaClient` 调用。
- `VirtualMediaFetcher`：实际请求 `SetAVTransportURI` 提供的 URL，跟随重定向，记录状态码、Content-Type、字节数和首包时间。
- `FfmpegDecoder`：调用 `ffprobe` 获取容器/编码/时长，调用 `ffmpeg -t 3 -f null -` 解码前 3 秒。
- 可选 SSDP 广播适配：默认集成测试直接注入 `DlnaRenderer`，另提供本机发现模式用于手工验证，避免组播在 Windows 环境下造成不稳定。

工具只依赖 Python 标准库和 FFmpeg 命令行。FFmpeg 路径通过 `--ffmpeg` 指定，未指定时从 PATH 查找；找不到时测试明确标记为环境缺失，不伪造通过。

## 4. 状态机

```text
NO_MEDIA_PRESENT
  -> TRANSITIONING  SetAVTransportURI
  -> PLAYING        Play + fetch + ffmpeg decode succeeds
  -> ERROR          fetch/decode/format failure

PLAYING -> PAUSED_PLAYBACK  Pause
PAUSED_PLAYBACK -> PLAYING  Play + decoder remains valid
PLAYING/PAUSED_PLAYBACK -> STOPPED  Stop
PLAYING -> TRANSITIONING -> PLAYING  Seek
```

`GetTransportInfo` 返回 `CurrentTransportState`；`GetPositionInfo` 返回 `RelTime`、`TrackDuration`、`TrackURI`。虚拟电视不因 SOAP `Play` 被调用就直接返回 `PLAYING`，必须先完成真实 URL 请求和 FFmpeg 解码。

## 5. 格式验证矩阵

### 离线样本

使用 FFmpeg 生成短样本，固定 3-5 秒、720p、无外部资源：

- MP4 H.264/AAC。
- MP4 HEVC/AAC（环境 FFmpeg 支持时）。
- HLS MPEG-TS/H.264/AAC。
- HLS fMP4/H.264/AAC。
- DASH fMP4/H.264/AAC。
- WebM VP9/Opus。
- 失效容器、错误 URL、空响应、超时。

每个样本通过本地 HTTP server 提供，避免测试依赖公网。

### 真实源

通过项目现有取流逻辑获得 dm84/omofuna 等当前可用源，进入同一个 `MediaProxy` 和 VirtualRenderer 链路。记录：

- 源标识、媒体 URL 类型，不记录完整 token/请求头。
- HTTP 首包耗时、下载状态、Content-Type。
- FFmpeg probe/decode 结果、首 3 秒解码耗时。
- SOAP 状态和错误信息。

真实源失败必须分类为：源失效、防盗链/请求头、LAN 代理不可达、格式不支持、FFmpeg 解码失败或协议逻辑失败。

## 6. 集成链路

```text
CastSession.start()
  -> MediaProxy.start_lan_endpoint()
  -> MediaProxy.build_url(public_base=LAN)
  -> DlnaClient.SetAVTransportURI()
  -> VirtualRenderer fetches LAN URL
  -> FFmpeg probe + decode
  -> VirtualRenderer state PLAYING
  -> CastSession.poll_state()
  -> VideoView stops local player only after PLAYING
```

测试必须验证：

- LAN URL 非 `127.0.0.1` 且虚拟电视可访问。
- m3u8 内部分片/密钥/嵌套播放列表可访问。
- VirtualRenderer 在解码失败时不报告 PLAYING。
- App 只在 PLAYING 后停止本地播放器。
- Pause/Play/Seek 改变虚拟电视状态和位置。
- 8 秒启动超时、连续轮询失败、停止清理能正确触发。

## 7. 测试文件与命令

新增：

- `tools/virtual_dlna_renderer.py`。
- `tests/test_virtual_dlna_renderer.py`：状态机、SOAP、HTTP 拉流、FFmpeg 成功/失败、Pause/Seek。
- `tests/test_cast_format_matrix.py`：离线多格式矩阵。
- `tests/test_cast_real_sources.py`：联网真实源测试，默认显式标记为网络测试，可通过环境变量启用。
- `tools/generate_cast_samples.py`：生成离线样本，不将二进制媒体提交进仓库。

建议命令：

```text
python tools/generate_cast_samples.py --output data/cast_samples
python -m pytest tests/test_virtual_dlna_renderer.py tests/test_cast_format_matrix.py -q
CAST_REAL_SOURCES=1 python -m pytest tests/test_cast_real_sources.py -q
python -m pytest tests/test_cast_session.py tests/test_media_proxy_lan.py tests/test_video_cast.py -q
```

真实源测试不得成为默认全量测试的一部分，以免网络波动污染回归结果；CI 运行离线矩阵，手工验收或显式环境变量运行联网测试。

## 8. 兼容性决策

当前实现先验证原始流是否可播放。若某电视/虚拟电视不支持 HLS、DASH、HEVC 或 WebM，产品层应采用「电脑端转码为 H.264/AAC」的兼容模式，而不是继续把原始 URL 宣称为通用格式。

转码模式的后续接口预留为：

```text
source URL -> MediaProxy -> optional FFmpeg transcode -> LAN HTTP output -> DLNA renderer
```

转码属于后续独立任务，必须单独测量 CPU、延迟、缓冲和多集切换，不在本次模拟器任务中隐式启用。

## 9. 验收标准

- 虚拟电视能通过现有 `DlnaClient` 完成 SetURI、Play、Pause、Seek、GetPositionInfo。
- 至少 MP4 H.264/AAC、HLS TS、HLS fMP4 离线样本实际解码通过。
- 不支持格式或错误 URL 不会报告 PLAYING，App 显示失败且保留本地播放。
- 多格式测试结果包含明确的格式、编码、耗时和失败原因。
- 真实源测试至少执行一次；若源失效，报告写明环境原因，不伪造通过。
- 现有 DLNA、LAN proxy、CastSession、VideoView 测试无新增失败。
