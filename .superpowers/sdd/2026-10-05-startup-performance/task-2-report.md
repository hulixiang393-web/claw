# 启动性能 Task 2 报告

## 变更

- `MainWindow` 构造期间不再同步调用初始主题 QSS。
- 使用一次 `QTimer.singleShot(0, ...)` 在事件循环空闲后应用初始主题。
- 增加待处理闩锁，显式主题切换仍立即应用，并取消尚未执行的初始应用，避免重复调用。
- 主题内容和用户主题切换行为未改变。

## TDD

- RED：新增 `tests/test_app_startup.py` 后，构造窗口立即记录到 `sakura`，断言失败。
- GREEN：延后初始主题应用后目标测试通过。
- 回归：补充空闲只应用一次，以及切换优先且不重复应用的断言。

## 验证

- `py -3.10 -m pytest tests/test_app_startup.py tests/test_app_background.py -q`：5 passed。
- `py -3.10 gui/_smoke.py`：通过。
- `py -3.10 gui/_smoke_app.py`：失败。该既有 smoke 在未处理事件前同步断言 QSS 已存在，与本任务要求的延后初始化冲突；按约束未修改该文件。
- `py -3.10 -m compileall -q framework gui tests`：通过。
- `git diff --check`：通过。

## 文件范围

本任务仅修改：

- `gui/app.py`
- `tests/test_app_startup.py`
- `.superpowers/sdd/2026-10-05-startup-performance/task-2-report.md`

工作区已有的 `app_config.json`、`SESSION.md`、`build.bat`、`启动.bat`、`init.mp4` 及其他用户改动均未修改。
