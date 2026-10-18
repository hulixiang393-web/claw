# Task 4：投屏性能与设备搜索回归

## 执行结果

- 设备对话框、代理指标、source tuning：`32 passed`。
- CastSession、DLNA、VirtualRenderer、格式矩阵：`42 passed`。
- 转码、视频投屏和 media proxy stream tuning：`80 passed，1 failed`。
- 显式联网 `CAST_REAL_SOURCES=1`：`9 passed，1 skipped`。
- `tests/test_hls_throughput_tuning.py`：`20 passed，5 failed`，失败均为已知基线差异，未修改无关配置。

## 失败与已知基线

- `test_cast_with_history_shows_device_dialog`：异步状态断言失败，`view._cast_active` 仍为 `False`；未修改相关实现。
- HLS 基线失败：默认缓冲实际为 60s 而断言为 8s；ikanpp 实际配置为 30s 而断言为 45s；prefetch hard cap 实际为 depth=64 而断言为 16。按要求未修改 `app_config.json`、源配置或测试。

## 显式联网

- `dm84`：分类 `proxy_unreachable`，取流阶段超时；脱敏指标 count=1、HTTP 200、bytes=159711248、首包均值约 87.48 ms。
- `omofuna`：分类 `protocol_error`，Cast Play 返回 501；脱敏指标 count=2、bytes=218824720、首包均值约 87.48 ms，1 次 `proxy_failure`。
- 未记录完整 URL、token、Cookie 或请求头；真实源失败未转写为成功。

## 报告更新

- 更新 `docs/superpowers/reports/2026-10-04-cast-simulation-report.md`：补充手动搜索验证、代理指标、B 站 30 秒缓冲与关闭预取、真实源分类及限制。
- 追加 `SESSION.md`，未覆盖已有内容。

## 最终检查

- `py -3.10 -m compileall -q framework gui tests tools`：通过。
- `git diff --check`：通过。
- 仅 Task 4 报告应被 stage；用户已有 `SESSION.md`、`app_config.json`、`build.bat`、`启动.bat` 及其他改动不提交。
