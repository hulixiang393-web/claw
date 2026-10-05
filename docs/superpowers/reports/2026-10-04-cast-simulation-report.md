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

## Task 4 回归验证（2026-10-05）

### 手动搜索已验证

- `tests/test_cast_dialog.py`：`24 passed`（Task 1 记录）；对话框打开不自动扫描，点击「搜索设备」后才启动搜索。
- 本轮设备对话框、代理指标和 source tuning 组合：`32 passed`。
- 历史设备仍置顶、默认选中；扫描期间搜索/刷新按钮禁用，结果或错误后恢复。

### 代理指标与流调优

- `tests/test_media_proxy_metrics.py tests/test_media_proxy_stream_tuning.py`：相关用例通过；指标只记录 kind、status、first_byte_ms、bytes、duration_ms 和分类，不记录 URL、token、Cookie 或请求头。
- 代理指标联网输出为脱敏汇总：`dm84` count=1、status=200、bytes=159711248、首包均值约 87.48 ms；`omofuna` count=2、bytes=218824720、首包均值约 87.48 ms，另有 1 次分类为 `proxy_failure`。这些数值只用于本次诊断，不代表长期带宽保证。
- `tests/test_hls_throughput_tuning.py`：`20 passed，5 failed`。既有基线失败为默认 60s vs 8s、ikanpp 30s vs 45s、prefetch hard cap 64 vs 16；未修改无关配置、源文件或测试。

### B 站投屏缓冲与预取

- `sources/bilibili.json` 的投屏调优已由 Task 3 测试覆盖：投屏缓冲 30s，预取显式关闭，depth=4、workers=2。
- 本报告不把预取关闭误报为性能成功；关闭是当前默认安全策略，避免改变源站请求模式和扩大风控面。

### 真实源分类

| source id | 本轮结果 | 脱敏指标 | 分类 |
| --- | --- | --- | --- |
| dm84 | 取流阶段超时 | count=1；HTTP 200；bytes=159711248；首包均值约 87.48 ms | `proxy_unreachable` |
| omofuna | Cast Play 返回 501 | count=2；bytes=218824720；首包均值约 87.48 ms；1 次 `proxy_failure` | `protocol_error` |

本轮显式联网命令为 `CAST_REAL_SOURCES=1 py -3.10 -m pytest tests/test_cast_real_sources.py -q -s`，结果 `9 passed，1 skipped`。真实源失败未转写为成功；`dm84` 未取得可解码媒体，`omofuna` 未进入 `PLAYING`。

### 回归范围与已知限制

- CastSession、DLNA、VirtualRenderer、格式矩阵：`42 passed`。
- 转码、视频投屏和 media proxy stream tuning：`80 passed，1 failed`；失败为既有 `test_cast_with_history_shows_device_dialog` 异步状态断言（`view._cast_active` 仍为 False），未修改相关实现。
- 真实链路受防盗链、线路时效、站点风控、代理出口、设备协议实现和源内容变化影响；离线通过不代表真实电视或所有公网源稳定通过。
- 编译检查与 `git diff --check` 在本 Task 收口时执行；不修改 `app_config.json`、`build.bat`、`启动.bat`。
