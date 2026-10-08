# BookStack 全屏阅读布局实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让 BookStack 多级目录以树形侧栏显示，正文占据剩余全部阅读区域，并正确显示正文图片。

**Architecture:** BookStack 使用 QTreeWidget 作为任意深度目录，普通源继续使用 QListWidget；正文 QTextBrowser 保持结构化 HTML，通过 CoverLoader 下载图片并用 QTextDocument.addResource 回填。

**Tech Stack:** Python 3.10、PySide6、lxml、pytest。

## Global Constraints

- 普通源目录和正文行为不改变。
- BookStack 站内章节链接继续在阅读器主体内跳转，外部链接打开浏览器。
- 图片下载必须复用现有 CoverLoader/HttpClient、Referer 和代次保护。
- 图片失败不能导致正文文字消失。
- 不修改用户配置文件，不提交 commit。

---

### Task 1: BookStack 树形多级目录

**Files:**
- Modify: `gui/pages/reader/novel_view.py`
- Test: `tests/test_bookstack_reader.py`

**Interfaces:**
- BookStack 创建 `QTreeWidget`，章节节点保存 `Qt.UserRole` 索引。
- 普通源保留现有 `QListWidget`。
- `_on_toc_clicked` 或树节点处理器继续调用 `_load_chapter(index)`。

- [ ] 写失败测试：三级章节生成树节点、父子关系、索引正确；正文区域和目录均能占据父布局高度。
- [ ] 运行 `python -m pytest tests/test_bookstack_reader.py -q` 确认失败。
- [ ] 实现 BookStack 专用树目录，宽度 260px，`QSizePolicy.Expanding`，目录按钮折叠/展开；按 `Chapter.depth` 构造嵌套节点。
- [ ] 运行定向测试确认通过。

---

### Task 2: BookStack 正文全宽与图片资源回填

**Files:**
- Modify: `gui/pages/reader/novel_view.py`
- Modify: `gui/components/cover_loader.py`（仅在需要公开图片完成回调时）
- Test: `tests/test_bookstack_reader.py`

**Interfaces:**
- 新增图片资源回填方法，例如 `_set_rich_image_resource(url: str, pixmap: QPixmap, generation: int)`。
- 当前 QTextDocument 通过 `addResource(QTextDocument.ImageResource, QUrl(url), pixmap)` 获得图片。

- [ ] 写失败测试：模拟 CoverLoader 图片回调，断言当前文档资源可取；换章后旧 generation 回调被忽略；正文 HTML 无图时文字仍显示。
- [ ] 运行定向测试确认失败。
- [ ] 正文区域设置伸展策略和最小尺寸为 0，清除重复图片 QLabel，富文本控件占满剩余区域；图片加载完成后 addResource 并刷新文档布局。
- [ ] 运行定向测试确认通过。

---

### Task 3: 集成回归

**Files:**
- Modify: `SESSION.md`（仅在用户允许时；当前默认不改）

- [ ] 运行 `python -m pytest tests/test_bookstack_reader.py tests/test_reader_zzz.py tests/test_reader_scroll_prefetch.py tests/test_public_novel_sources.py -q`。
- [ ] 运行 `python -m compileall -q framework gui tests`。
- [ ] 运行 `python -X utf8 gui/_smoke_reader.py`。
- [ ] 运行 `git diff --check`，确认无无关文件被覆盖。
