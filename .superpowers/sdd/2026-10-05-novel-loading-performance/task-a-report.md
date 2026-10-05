# Task A 报告：小说目录分批渲染与阅读预取降载

日期：2026-10-05

## 改动

- `NovelView._populate_toc()` 首次只创建 200 个目录项，使用 `QTimer` 分批追加剩余章节。
- 目录批次增加 generation 校验，切书或重新填充目录时旧批次不会继续追加；当前章节选择在目录项可用后同步。
- 小说正文加载、前后预取回调携带加载 generation，旧书异步结果不会写入当前书内容。
- 预取默认改为 `ahead=1、behind=0`，`set_prefetch_config()` 将 ahead 安全上限限制为 1，并关闭 behind。
- `reader_prefetch_settings()` 使用同样的默认值和安全上限；未修改 `app_config.json`。

## TDD 结果

1. 先新增失败测试并运行 RED：4 failed，失败原因分别为目录同步创建、缺失目录 generation、默认预取 3+1、设置读取未限幅。
2. 实现后目标测试 GREEN：`tests/test_reader_zzz.py` 8 passed。
3. 相关 reader 回归：
   - `tests/test_reader_zzz.py tests/test_read_restore_cross_book.py tests/test_reader_download_range.py tests/test_reading_progress_shelf.py`：37 passed。
4. 缓存相关回归：2 个既有失败，未修改相关文件：
   - `tests/test_cache_service.py::test_content_precache_chapters`
   - `tests/test_cache_service.py::test_get_session_cache_singleton_not_persisted`
5. `py -3.10 -m compileall -q framework gui tests`：通过。
6. `git diff --check`（本子任务文件）：通过。

## 提交

提交信息：`perf(reader): batch novel toc and limit prefetch`
