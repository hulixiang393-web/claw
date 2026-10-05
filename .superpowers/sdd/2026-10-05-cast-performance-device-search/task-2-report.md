# Task 2：投屏代理传输指标

## 实现

- `MediaProxy.transport_metrics()` 返回仅包含 `kind`、`status`、`first_byte_ms`、`bytes`、`duration_ms` 的脱敏快照；异常时额外返回分类字段 `failure_category`。
- `MediaProxy.clear_transport_metrics()` 清空内存指标队列。
- 复用现有线程安全上游诊断队列，队列继续受 `_UPSTREAM_DIAGNOSTICS_MAX` 限制；新增累计字节数记录。
- `tools/run_cast_real_sources.py` 仅在显式设置 `CAST_REAL_SOURCES=1` 并实际运行真实源时输出每源传输指标汇总，汇总不含 URL、token、Cookie 或请求头。

## TDD 证据

1. 先新增 `tests/test_media_proxy_metrics.py`。
2. RED：新测试运行结果为 `1 failed, 2 errors`，失败原因是 `transport_metrics` 与 `clear_transport_metrics` 尚不存在。
3. GREEN：实现后目标测试运行结果为 `4 passed`；最终指定测试为 `24 passed, 2 skipped`。

## 验证

- `py -3.10 -m pytest tests/test_media_proxy_metrics.py tests/test_media_proxy_stream_tuning.py tests/test_cast_real_sources.py -q`：`24 passed, 2 skipped`。
- `py -3.10 -m compileall -q framework/media_proxy.py tools/run_cast_real_sources.py tests/test_media_proxy_metrics.py`：通过。
- `git diff --check`：Task 2 文件无 diff 错误。

## 范围

仅修改：

- `framework/media_proxy.py`
- `tests/test_media_proxy_metrics.py`
- `tools/run_cast_real_sources.py`
- 本报告文件

未修改用户明确排除的 `app_config.json`、`SESSION.md`、`build.bat`、`启动.bat`。
