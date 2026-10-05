# Task B 报告：发现页预加载和番茄搜索等待优化

## 变更

- `gui/pages/discover_page.py`
  - 默认预加载页数改为 1，默认并发改为 2。
  - `apply_preload_settings` 将页数上限夹到 2，并发上限夹到 2。
- `framework/settings_manager.py`
  - `discover` 默认值同步为 `preload_pages=1`、`preload_concurrency=2`。
  - `discover_preload_settings` 同步使用页数上限 2、并发上限 2。
  - 用户配置文件仍正常读取，未写回或修改配置文件内容。
- `sources/fanqie.json`
  - 搜索 `wait_until` 从 `networkidle` 改为 `domcontentloaded`。
  - `extra_delay_ms` 从 3000 改为 0。
  - `response_pages` 从 3 改为 1。
- `tests/test_novel_loading_performance.py`
  - 新增 TDD 回归测试，锁定默认值、运行时上限、配置文件不落盘及番茄搜索渲染参数。

## TDD 证据

- RED：旧实现下目标测试为 `4 failed, 1 error`，失败原因分别对应旧默认值、旧上限、`networkidle` 和测试 fixture 初始化问题；修正 fixture 后仍保持目标行为失败。
- GREEN：目标测试 `5 passed`。

## 验证

- 目标及 discovery/search/fanqie/playwright 回归：`47 passed`。
- `py -3.10 -m compileall -q gui/pages/discover_page.py framework/settings_manager.py framework/playwright_helper.py`：通过。
- `git diff --check`（任务文件）：通过。
- 未修改 `app_config.json`、`SESSION.md`、`build.bat`、`启动.bat`、`init.mp4`。
