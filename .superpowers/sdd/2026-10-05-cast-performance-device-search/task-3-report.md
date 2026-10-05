# Task 3 报告：投屏源缓冲调优

## 实现

- `sources/bilibili.json` 增加 `media.cast`：30 秒投屏缓冲，预取显式关闭，depth=4、workers=2。
- `VideoView` 从当前 `source.raw` 读取 `media.cast`，仅在 CastSession 启动前调用现有 `MediaProxy.configure_prefetch`；缺少兼容接口时安全跳过。
- `external_player.open_with_player` 既有签名与本地 VLC 参数保持不变；投屏链路直接复用 `CastSession` 启动前的 MediaProxy 配置接口。
- MediaProxy 继续使用现有硬上限：depth 64、workers 8；新增测试覆盖显式开关和边界夹紧。

## TDD 证据

- RED：`tests/test_cast_source_tuning.py` 首次运行 `3 failed, 1 passed`，失败分别对应 B 站配置缺失、VideoView 读取接口缺失、投屏 tuning 传递接口缺失。
- GREEN：新增测试最终 `4 passed`。

## 验证

- `py -3.10 -m pytest tests/test_cast_source_tuning.py tests/test_external_player_playlist.py -q`：53 passed。
- `py -3.10 -m compileall -q framework gui tests`：通过。
- `git diff --check`：通过。

## 已知工作区问题

- 指定组合回归中，`tests/test_video_cast.py` 有 1 个异步状态断言失败，位置为已有历史设备投屏用例；其余失败来自工作区已有的吞吐调优并行改动（B 站/ikanpp 缓冲与预取边界），不是本 Task 新增测试或允许修改文件造成。
- 未修改 `app_config.json`、`SESSION.md`、`build.bat`、`启动.bat`；也未修改 `framework/media_proxy.py`。
