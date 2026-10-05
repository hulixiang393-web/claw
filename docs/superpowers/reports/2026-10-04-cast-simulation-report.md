# 真实源投屏诊断报告

测试仅在显式设置 `CAST_REAL_SOURCES=1` 时运行。记录字段限制为 source id、媒体类型、HTTP 状态、首包耗时、probe/decode 结果和分类；未记录媒体 URL、请求头或 token。

| source id | 媒体类型 | HTTP 状态 | 首包耗时（ms） | probe/decode | 分类 |
| --- | --- | ---: | ---: | --- | --- |
| dm84 | video | n/a | n/a | probe=n/a; decode=n/a | proxy_unreachable |
| omofuna | video | 200 | 1139.7723999980371 | probe=0; decode=0 | decoded |

## 结果详情

### dm84

- 分类：`proxy_unreachable`
- probe：`{}`
- decode：`{}`
- 原因：timed out

### omofuna

- 分类：`decoded`
- probe：`{"audio_codec": "aac", "container": "hls", "duration": 1477.030992, "returncode": 0, "stderr_summary": "", "video_codec": "h264"}`
- decode：`{"returncode": 0, "stderr_summary": ""}`

## 独立复测补充（2026-10-04）

随后使用相同项目链路独立复测两次，结果均为：

- `dm84`：`proxy_unreachable`，连接超时。
- `omofuna`：`protocol_error`，虚拟电视实际状态为 `ERROR`，诊断为 `RemoteDisconnected`，未报告 `PLAYING`。

因此首次 omofuna 成功不能代表公网源稳定通过；当前结论是离线格式矩阵稳定，真实源受防盗链、线路和上游连接时效影响，尚不能宣称整体流畅或稳定。

## 自动转码回退验证（2026-10-05）

离线 VirtualRenderer 使用仅支持 H.264/AAC 的模拟电视：

| 输入 | 原始结果 | 转码结果 | 首 playlist | PLAYING | 结论 |
|---|---|---|---:|---:|---|
| WebM VP9/Opus | `ERROR`，unsupported renderer codec | H.264/AAC HLS，实际解码成功 | 376.7 ms | 748.8 ms | 通过 |
| MP4 H.264/AAC | 原始实际解码成功 | 未启动转码 | — | — | 通过 |

该验证经过 `TranscodeSession → MediaProxy LAN directory → DlnaClient → VirtualRenderer → FFmpeg` 完整链路。停止后临时目录和 FFmpeg 进程均清理。

真实公网源本轮仍按前述结果处理：dm84 代理超时；omofuna 独立复测为 RemoteDisconnected。未将公网失败伪造为转码成功。

## 真实源转码回退尝试（2026-10-05）

使用同一 `TranscodeSession → MediaProxy LAN → VirtualRenderer` 链路再次尝试：

- `dm84`：本次原始 HLS 实际 `decoded`，因此未触发转码。
- `omofuna`：原始投屏失败后进入转码，但 FFmpeg 在 HLS 输出就绪前退出，分类为 `TranscodeError: ffmpeg exited before HLS output was ready`。

真实源结果随线路、防盗链、上游连接和源内容变化；离线转码链路已验证，公网源转码仍需针对具体源的请求头/协议进一步适配。