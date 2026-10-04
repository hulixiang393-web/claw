# Task 2 报告：VirtualRenderer SOAP／状态机

## 实现

- 在 `tools/virtual_dlna_renderer.py` 新增 `VirtualRenderer`，支持 `start()`、`stop()`、`state()`、`set_uri()`、`play()`、`pause()`、`stop_playback()`、`seek()`。
- 使用标准库 `ThreadingHTTPServer` 暴露 `/description.xml` 和 `/upnp/control/avtransport1`，现有 `framework.dlna.DlnaClient` 可直接发现描述并调用 AVTransport SOAP。
- `SetAVTransportURI` 进入 `TRANSITIONING`；`Play` 必须依次真实 fetch、ffprobe 和 ffmpeg 短解码成功后才进入 `PLAYING`。
- 播放失败进入 `ERROR`，SOAP 错误响应只返回通用错误，不回显完整媒体 URL、查询参数或请求头。
- 实现暂停、恢复播放、停止、相对时间 Seek，以及 `GetTransportInfo`／`GetPositionInfo` 所需字段。
- `stop()` 会关闭 HTTP 服务、回收线程，不阻塞后续测试。

## 测试

先补测试并确认 RED：

```text
ImportError: cannot import name 'VirtualRenderer'
```

实现后验证：

```text
Python310 -m pytest tests/test_virtual_dlna_renderer.py tests/test_dlna.py -q
19 passed in 9.97s
```

其它验证：

- `Python310 -m compileall -q tools/virtual_dlna_renderer.py tests/test_virtual_dlna_renderer.py`：通过。
- `git diff --check`：通过。

## 改动范围

本任务新增／修改：

- `tools/virtual_dlna_renderer.py`
- `tests/test_virtual_dlna_renderer.py`
- `.superpowers/sdd/2026-10-04-virtual-dlna-renderer/task-2-report.md`

未修改 `app_config.json`、`启动.bat`、`build.bat`，未提交 Git commit。
