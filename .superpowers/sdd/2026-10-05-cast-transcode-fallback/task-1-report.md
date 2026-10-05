# Task 1：TranscodeSession

## 实现

- 新增 `framework/transcode_session.py`，使用标准库管理 FFmpeg HLS 转码进程与临时目录。
- FFmpeg 输出 H.264/AAC、2 秒 HLS 分片和 `independent_segments`。
- 请求头通过 FFmpeg `-headers` 传入；FFmpeg stderr 仅写入临时日志文件，不进入诊断信息或异常文本。
- `start` 等待 playlist 与首个 `segment*.ts`，超时终止进程并清理临时目录。
- `stop` 幂等终止进程、必要时 kill，并清理临时目录；`diagnostics` 仅暴露状态、启动耗时和脱敏错误。

## TDD 证据

- RED：新增测试后运行目标测试，收集阶段因 `framework.transcode_session` 不存在而失败：`ModuleNotFoundError`。
- GREEN：实现后运行目标测试：`4 passed`。

## 验证

- `C:\Users\alonely\AppData\Local\Programs\Python\Python310\python.exe -m pytest tests/test_transcode_session.py -q`
- `C:\Users\alonely\AppData\Local\Programs\Python\Python310\python.exe -m compileall framework/transcode_session.py tests/test_transcode_session.py`
- `git diff --check`
