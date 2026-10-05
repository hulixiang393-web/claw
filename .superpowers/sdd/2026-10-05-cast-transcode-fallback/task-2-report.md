# Task 2：LAN 转码输出与 VirtualRenderer 集成

## 实现

- `MediaProxy` 新增公开、线程安全的 `register_transcode_directory` / `unregister_transcode_directory` 接口。
- 转码目录使用独立 `/t/<token>/<relative-path>` 映射，只接受当前活动 LAN endpoint 注册；token 归属 LAN 生命周期。
- playlist 与 TS segment 只读输出，固定 HLS MIME；解析后的路径必须位于注册根目录内，目录穿越返回 404。
- LAN endpoint 停止时清除转码目录映射，但保留本地 `/s/<token>` token；注销接口幂等。
- `TranscodeSession` 可注入 `MediaProxy`，将临时 HLS 目录注册到 LAN URL，停止时注销 token；停止流程继续保证 FFmpeg 进程和临时目录清理。

## TDD 证据

- RED：新增集成测试后运行目标测试，3 项均失败：缺少 `media_proxy` 构造参数、缺少 `register_transcode_directory`，以及转码目录接口不存在。
- GREEN：实现后运行 `tests/test_transcode_integration.py`：`3 passed`。
- VirtualRenderer 通过真实 LAN HLS playlist 与 TS 请求，并由 FFmpeg 实际 fetch/decode 后进入 `PLAYING`。

## 验证

- `C:\Users\alonely\AppData\Local\Programs\Python\Python310\python.exe -m pytest tests/test_transcode_integration.py tests/test_transcode_session.py tests/test_media_proxy_lan.py tests/test_virtual_dlna_renderer.py -q`：`37 passed`。
- Python 3.10 `compileall`：通过。
- `git diff --check`：通过。
