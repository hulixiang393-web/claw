# BookStack 站内目录跳转与 MopBook 阅读链路实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 让 BookStack 正文目录链接在应用阅读器内跳转并显示二级目录层级，同时修复 MopBook 详情/章节打开失败。

**Architecture:** 扩展 `Chapter` 的可选层级元数据，BookStack 解析器递归保留 DOM 顺序；NovelView 拦截 BookStack 正文章节链接并复用 `_load_chapter()`。MopBook 继续使用现有 `HttpClient`，通过源级请求配置和 GUI 异步错误路径统一详情、章节访问，禁止绕过验证。

**Tech Stack:** Python 3.10、PySide6、lxml、curl_cffi、pytest。

## Global Constraints

- 不绕过 WAF、验证码或登录验证。
- `Content.fetch_chapter()` 继续返回纯文本；富文本/图片接口保持兼容。
- 普通源目录行为保持不变，BookStack 只增加可选层级显示。
- 正文站内章节链接在阅读器内跳转；未匹配章节的外部/参考链接仍打开浏览器。
- 不修改用户配置文件，不提交无关工作区改动。

---

### Task 1: 章节层级模型与 BookStack 二级目录解析

**Files:**
- Modify: `framework/content.py:73-80`（`Chapter` 定义）
- Modify: `framework/content.py:1021-1053`
- Modify: `tests/test_bookstack_reader.py`

**Interfaces:**
- Produces `Chapter.depth: int = 0`，旧的 `Chapter(title, url)` 调用继续有效。
- BookStack `_fetch_bookstack_chapters()` 返回按 DOM 顺序排列、URL 去重、带 `depth` 的章节列表。

- [x] **Step 1: 写失败测试**

在 `tests/test_bookstack_reader.py` 增加带嵌套 `ul` 的详情 fixture/测试，断言章节标题、URL、顺序和深度为 `[0, 1, 1, 0]`，并断言重复链接只出现一次。

- [x] **Step 2: 运行测试确认失败**

运行：`python -m pytest tests/test_bookstack_reader.py -q`
预期：`Chapter` 没有 `depth` 或深度断言失败。

- [x] **Step 3: 最小实现**

给 `Chapter` 增加默认字段 `depth=0`。递归函数改为 `walk_li(child, depth)`，顶层传 `0`，嵌套列表传 `depth + 1`，构造章节时写入 `depth`。

- [x] **Step 4: 运行测试确认通过**

运行：`python -m pytest tests/test_bookstack_reader.py -q`
预期：全部通过。

---

### Task 2: 阅读器目录缩进与 BookStack 正文站内跳转

**Files:**
- Modify: `gui/pages/reader/novel_view.py:179-182, 305-329, 677-679`
- Modify: `tests/test_bookstack_reader.py`

**Interfaces:**
- 新增 `_on_rich_anchor_clicked(url: QUrl) -> None`。
- 新增 `_find_chapter_index(url: str) -> int`，找不到返回 `-1`。
- `NovelView` 仅对 BookStack 的站内章节 URL 调用 `_load_chapter(index)`。

- [x] **Step 1: 写失败测试**

增加测试：构造含一级/二级章节的 `NovelView`，调用 `_populate_toc()`，断言二级 `QListWidgetItem` 的 `text` 带缩进或 `visualItemRect` 对应缩进数据；再调用 `_on_rich_anchor_clicked(QUrl(chapter_url))`，断言 `_load_chapter` 收到对应索引。另测外部 URL 调用 `webbrowser.open`，不调用 `_load_chapter`。

- [x] **Step 2: 运行测试确认失败**

运行：`python -m pytest tests/test_bookstack_reader.py -q`
预期：正文链接仍由 Qt 自动打开，或目录项没有层级信息。

- [x] **Step 3: 最小实现**

将 `_rich_body.setOpenExternalLinks(False)`，连接 `anchorClicked`。在 `_on_rich_anchor_clicked` 中对 URL 去 fragment、规范化尾斜杠并与章节 URL 比较；匹配时阻止默认行为并调用 `_load_chapter`，未匹配时调用 `webbrowser.open`。目录项写入 `Qt.UserRole + 1` 深度，BookStack 项目按深度设置 `setText("  " * depth + title)` 或 `setTextMargin`，普通源深度为 0。

- [x] **Step 4: 运行测试确认通过**

运行：`python -m pytest tests/test_bookstack_reader.py tests/test_reader_zzz.py -q`
预期：正文链接和目录跳转测试通过。

---

### Task 3: MopBook 请求与详情进入阅读器回归

**Files:**
- Modify: `sources/mopbook.json`
- Modify: `framework/http.py` 或 MopBook 请求适配所在文件（仅在测试证明必要时）
- Modify: `gui/pages/discover_page.py`
- Modify: `tests/test_public_novel_sources.py`
- Modify: `tests/test_mopbook_reader.py`（新建）

**Interfaces:**
- MopBook 发现作品 URL、详情章节 URL 始终为绝对 URL。
- 详情失败时不调用系统浏览器；GUI 状态栏显示 MopBook 请求/验证错误。
- 详情成功时调用现有阅读器打开流程。

- [x] **Step 1: 写失败测试**

添加 MopBook fixture 测试：加载列表作品，调用 `Content.fetch_detail()`，断言详情有章节且首个章节 URL 以 `https://www.mopbook.com/book/` 开头；调用章节正文解析，断言返回非空纯文本。添加 GUI 失败测试，模拟 `AntiScrapeError`，断言状态栏包含验证/请求失败信息且未调用 `webbrowser.open`。

- [x] **Step 2: 运行测试确认失败**

运行：`python -m pytest tests/test_mopbook_reader.py -q`
预期：详情或章节链路失败，或 GUI 失败路径缺少提示。

- [x] **Step 3: 最小实现**

检查 `_FetchDetailTask` 与 `_on_detail_loaded` 的异常传递，确保 MopBook 的 `RequestError`、`AntiScrapeError` 不被转为外部打开动作；在错误回调中保留发现页并设置可读错误。若 `HttpClient` 的 MopBook 请求没有统一 `impersonate="chrome"`，只在源 transport 配置或现有 defaults 入口补齐，不新增绕过逻辑。

- [x] **Step 4: 运行测试确认通过**

运行：`python -m pytest tests/test_mopbook_reader.py tests/test_public_novel_sources.py tests/test_chapter_pagination.py -q`
预期：MopBook 详情/章节和失败回归通过。

---

### Task 4: 全量验证与会话记录

**Files:**
- Modify: `SESSION.md`
- Modify: `learning-log.md`（仅记录可复用的新根因/解决方案）

- [x] **Step 1: 运行相关回归**

运行：`python -m pytest tests/test_bookstack_reader.py tests/test_mopbook_reader.py tests/test_public_novel_sources.py tests/test_source_search.py tests/test_chapter_pagination.py tests/test_reader_zzz.py tests/test_reader_scroll_prefetch.py -q`

- [x] **Step 2: 运行静态检查和 smoke**

运行：`python -m compileall -q framework gui tests`、`git diff --check`、`python -X utf8 gui/_smoke_reader.py`。

- [x] **Step 3: 检查工作区**

运行：`git status --short` 和 `git diff --stat`，确认只包含本任务文件和已有用户改动，不覆盖用户配置。

- [x] **Step 4: 更新会话记录**

记录 BookStack 正文链接拦截、二级目录深度和 MopBook 详情错误处理的结果及测试数量。
