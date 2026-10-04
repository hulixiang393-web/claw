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
