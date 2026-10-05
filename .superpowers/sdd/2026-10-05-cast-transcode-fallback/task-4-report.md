# Task 4：投屏转码回退

## 完成内容

- 原始投屏启动异常或 8 秒未进入 `PLAYING` 时，对 DASH/未知格式自动启动一次 H.264/AAC HLS 转码。
- 复用后台线程执行 LAN 端点、FFmpeg 启动、转码目录注册和 `start_transcoded`，GUI 主线程只处理 Qt 信号。
- 转码期间保留本地 VLC，状态显示「正在转换为电视兼容格式」；转码投屏确认 `PLAYING` 后才停止本地播放器。
- MP4、M4V、MOV、WebM 和 HLS（`.m3u8`/`.m3u`）不触发转码。
- 使用投屏代数、转码任务身份和轮询任务身份守卫，重复回退、陈旧 signal、关闭/切源后的迟到结果均不会复活会话；迟到转码结果会清理转码会话。
- 转码失败进入明确的 `转码失败：...` 错误态，保留本地播放器并允许重新投屏重试，不形成自动循环。

## TDD 证据

- RED：新增回退、PLAYING 后停播、兼容格式不转码、转码失败终态用例；修改前 `tests/test_video_cast.py` 为 `3 failed, 52 passed`，失败原因为 `_start_cast_transcode_fallback` 不存在。
- GREEN：实现后 `tests/test_video_cast.py` 为 `55 passed`。

## 验证

- `py -3.10 -m pytest tests/test_video_cast.py tests/test_cast_session.py tests/test_transcode_session.py tests/test_transcode_integration.py tests/test_cast_dialog.py -q`：`105 passed`
- `py -3.10 -m compileall -q gui/pages/reader/video_view.py tests/test_video_cast.py`：通过
- `git diff --check`：通过

## 修改范围

- `gui/pages/reader/video_view.py`
- `tests/test_video_cast.py`
- 本报告文件

未修改框架转码、CastSession、MediaProxy API 及用户明确禁止的配置/启动文件。
