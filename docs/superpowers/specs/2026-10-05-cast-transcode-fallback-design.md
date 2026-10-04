# 投屏自动转码回退设计

日期：2026-10-05
状态：用户已批准

## 目标

当原始媒体流无法被 DLNA 电视播放时，电脑端自动使用 FFmpeg 转换为 H.264/AAC 兼容流，再通过现有 LAN MediaProxy 投屏。原始流优先，转码只在启动失败或 8 秒内未进入 PLAYING 时尝试一次。

## 范围

- 新增独立的 FFmpeg 转码会话模块，不改变本地 VLC 默认播放。
- 复用现有媒体请求头、防盗链和 MediaProxy LAN 端点。
- 输出兼容的 HLS：H.264 + AAC、MPEG-TS 分片、短分片低延迟。
- 转码失败时保留本地 VLC，显示明确错误，不报告虚假 PLAYING。
- 支持停止、清理临时目录、FFmpeg 进程终止和重复启动保护。
- 测试使用本地样本、VirtualRenderer 和项目真实源诊断。

## 非目标

- 不保证 DRM、加密硬件解码、特殊 HDR 或所有厂商私有协议。
- 不始终转码；用户已选择原始流优先。
- 不新增 Python 第三方依赖；仅调用现有 FFmpeg 命令行。
- 不在本任务实现真实电视厂商兼容矩阵。

## 架构

```text
原始 URL + headers
  -> CastSession 原始投屏
  -> SetURI/Play 失败或 8s 未 PLAYING
  -> TranscodeSession 启动 FFmpeg
  -> HLS playlist + H.264/AAC TS segments
  -> LAN HTTP endpoint
  -> SetURI(transcoded playlist) + Play
  -> VirtualRenderer/真实电视 PLAYING
```

`TranscodeSession` 负责：

- 创建私有临时目录；
- 启动 FFmpeg 子进程；
- 传入输入 URL 和安全请求头；
- 输出 HLS playlist/segments；
- 等待 playlist 和首个 segment 出现；
- 提供输出 playlist URL；
- stop 时终止进程并删除临时资源；
- 记录首帧/首片段耗时和 stderr 摘要，不记录完整 URL、token 或请求头。

## FFmpeg 输出

输入：现有源 URL，使用 `-headers` 传递 `Referer`、`User-Agent` 等请求头。

输出核心参数：

```text
-c:v libx264 -preset veryfast -tune zerolatency
-pix_fmt yuv420p -profile:v main -level 4.0
-c:a aac -b:a 128k -ar 48000 -ac 2
-f hls -hls_time 2 -hls_list_size 6
-hls_flags delete_segments+independent_segments
```

输出 MIME 为 `application/vnd.apple.mpegurl`；HLS 内部片段由 LAN endpoint 提供。转码模式的 Seek 采用重启/重定位 FFmpeg 进程的后续扩展；本任务先保证播放、暂停和停止，进度控制失败时显示明确提示，不伪造成功。

## 回退状态机

```text
ORIGINAL_CONNECTING
  -> ORIGINAL_PLAYING
  -> ORIGINAL_FAILED
  -> TRANSCODING
  -> TRANSCODE_PLAYING
  -> ERROR
```

- 原始投屏收到 `PLAYING`：停止本地播放器，结束。
- 原始投屏 start 异常或 8 秒未 PLAYING：停止原始会话，保留本地播放器，启动转码。
- 转码输出准备失败：进入 ERROR，保留本地播放器。
- 转码投屏收到 `PLAYING`：停止本地播放器，进入活动状态。
- 用户停止投屏/关闭页面：同时停止 DLNA、FFmpeg、转码 token 和临时目录。
- 每次投屏最多自动回退一次，防止循环。

## 测试

1. `TranscodeSession` 单测：命令参数、请求头脱敏、playlist 就绪、进程终止、超时、stderr 分类。
2. VirtualRenderer 集成：原始不支持样本触发转码后，H.264/AAC HLS 实际解码并报告 PLAYING。
3. CastSession/UI：原始失败后转码回退只发生一次；成功后才停止本地播放器；两种失败都保留本地播放器。
4. 真实源：对当前 dm84/omofuna 记录原始和转码结果、首片段耗时、分类；网络源失败不伪造通过。
5. 回归：DLNA、LAN proxy、CastSession、VideoView 和离线矩阵无新增失败。

## 验收

- 本地 VirtualRenderer 上原始失败流可经转码变为 PLAYING。
- 转码启动延迟、首片段耗时和进程 CPU 可记录。
- 失败时无 FFmpeg 孤儿进程、无临时目录泄漏、无虚假 PLAYING。
- 原始兼容流仍走低延迟原始路径。
- 真实公网源结果如实记录；不承诺所有 DRM/厂商格式通用。
