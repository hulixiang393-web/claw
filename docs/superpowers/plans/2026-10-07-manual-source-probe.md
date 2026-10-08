# 手动播放源可用性检测实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在阅读界面提供手动检测播放源功能，最多串行检测 5 个线路，并标记成功线路。

**Architecture:** 使用通用 `media.source_probe` 配置控制功能；VideoView 通过后台 QRunnable 调用现有取流解析器，使用作品/换源代数丢弃迟到结果；成功状态仅更新播放源下拉框，不自动切换当前线路。

**Tech Stack:** Python 3.10、PySide6、现有 Content/StreamResolver、pytest。

## Global Constraints

- 不自动检测，只有用户点击「检测播放源」按钮后才发请求。
- 一次最多检测 5 个不同播放源，默认串行，避免触发反爬。
- 失败或找不到播放地址不标记，不影响当前播放。
- 不绕过 WAF、验证码、登录或防盗链。
- 不记录完整播放 URL、签名参数或 Cookie。
- 不修改用户配置文件，不提交 commit，不添加代码注释。

---

### Task 1: 通用播放源检测配置

**Files:**
- Modify: `framework/settings_manager.py` 或通用媒体配置读取位置
- Modify: `framework/config.py`（仅在配置 schema 校验需要时）
- Test: `tests/test_media_source_probe_config.py`

**Interfaces:**
- `media_source_probe_settings(settings) -> dict` 返回 `enabled`、`max_sources`、`concurrency`、`timeout_ms`。
- 默认值：`enabled=True`、`max_sources=5`、`concurrency=1`、`timeout_ms=30000`；该超时按单个播放源计算。
- `max_sources` 限制在 1～5，`concurrency` 限制为 1。

- [ ] 写失败测试：默认配置、禁用配置、上限裁剪。
- [ ] 运行 `python -m pytest tests/test_media_source_probe_config.py -q` 确认 RED。
- [ ] 实现通用配置读取，不为任何具体源写特殊配置。
- [ ] 运行定向测试确认通过。

---

### Task 2: VideoView 手动检测任务

**Files:**
- Modify: `gui/pages/reader/video_view.py`
- Test: `tests/test_video_source_probe.py`

**Interfaces:**
- 新增 `_ProbeSourcesTask(QRunnable)`，按源顺序串行执行，结果信号返回 `{source_id: bool}` 和错误数量。
- 新增 `VideoView._probe_sources()` 手动入口。
- 新增 `VideoView._on_sources_probed(results, generation)`，只处理当前作品/换源代数。

- [ ] 写失败测试：进入阅读界面不自动触发；点击入口后才创建任务；最多 5 个、去重、串行；取消/换源后迟到结果丢弃。
- [ ] 运行测试确认 RED。
- [ ] 实现后台任务：每个候选调用现有 `Content.fetch_video_streams` 或 resolver；单个候选最多等待 30000ms，超时记为「超时」并继续下一个；成功且返回非空媒体 URL才记 True；捕获普通失败为 False；取消异常立即停止。
- [ ] 运行定向测试确认通过。

---

### Task 3: 播放源下拉框标记与交互

**Files:**
- Modify: `gui/pages/reader/video_view.py`
- Modify: `gui/pages/reader_page.py`（仅在按钮布局由外层管理时）
- Test: `tests/test_video_source_probe.py`

**Interfaces:**
- 播放源下拉框成功项显示 `源名称（可播放）`，失败项保持原名称。
- 新增按钮「检测播放源」，检测期间禁用，完成后恢复。
- 配置 `enabled=False` 时按钮隐藏或禁用且不发请求。

- [ ] 写失败测试：成功标记、失败不标记、按钮状态恢复、禁用配置不发请求。
- [ ] 实现 UI 更新，保留源 ID 在 itemData 中，不通过显示文本反推源 ID。
- [ ] 换作品/换源/停止播放时清理标记和取消在途任务。
- [ ] 运行 UI 定向测试和 reader/video smoke。

---

### Task 4: 集成验证

**Files:**
- No user configuration changes.

- [ ] 运行 `tests/test_media_source_probe_config.py tests/test_video_source_probe.py tests/test_video_series_playlist.py tests/test_video_single_chapter.py tests/test_video_next_ep.py`。
- [ ] 运行 `python -m compileall -q framework gui tests`。
- [ ] 运行视频/阅读器 smoke。
- [ ] 运行 `git diff --check` 和 `git status --short`，确认无无关文件被覆盖。
