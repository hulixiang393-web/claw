# 跨源换源（全类型）设计文档

日期：2026-09-06
状态：待实施计划

## 1. 背景与目标

当前系统仅支持**同源站内多线路切换**（`source_switch`，如 MacCMS 站点的多播放 DFS 线路，经阅读器 `⇄` 菜单切换）。当**整个源站**失效（线路全挂、站点改版、反爬拦截）时，用户无法继续阅读同一作品。

目标：在阅读界面提供「跨源换源」能力——用当前作品标题去**其他同类型源站**搜索同一作品，用户选中候选后无缝切换到新源继续阅读。覆盖全部内容类型（视频/漫画/小说），epub 本地书除外（无源可换）。

## 2. 需求决策（已确认）

| 决策点 | 结论 |
|---|---|
| 触发时机 | 手动点按钮（不自动换源） |
| 匹配方式 | 调用各源搜索接口 → 标题清洗后相似度 ≥90% 才展示 |
| 相似度算法 | 先清洗标题噪音，再 `SequenceMatcher.ratio()>=0.90` |
| 候选呈现 | 弹列表用户点选（即使只有 1 条也要确认） |
| 换源后集数 | 视频保留集数序号 `min(当前序号, 新源分集数)` |
| 搜索范围 | 同内容类型、已启用、有搜索接口、排除当前源 |
| 无结果显示 | 明细弹窗（各源无结果/失败/跳过原因） |
| 架构 | 新建独立模块 `framework/cross_source.py` |
| 入口 | 视频 `⇄` 菜单加项；漫画/小说工具条加 `⇄ 换源` 按钮 |
| 字体修复 | 顺带修复阅读界面列表项文字截断 |

## 3. 架构

```
framework/cross_source.py        # 跨源搜索 + 匹配 + 切换
  find_cross_source(...)         # 并发搜索 + 清洗 + 相似度过滤
  switch_to_source(...)          # 抓新源详情 + 集数序号保留
  clean_title(...)               # 标题噪音清洗规则
  similarity(...)                # SequenceMatcher 封装

gui/pages/reader/cross_source_dialog.py   # 候选选择对话框
  CrossSourceDialog               # 搜索词编辑 + 候选卡列表 + 明细 + 刷新

gui/pages/reader/video_view.py   # ⇄ 菜单加「跨源换源」项（改动）
gui/pages/reader/comic_view.py   # 工具条加「⇄ 换源」按钮（改动）
gui/pages/reader/novel_view.py   # 工具条加「⇄ 换源」按钮（改动）
gui/pages/reader/reader_page.py  # 三视图信号 → 后台换源调度（改动）
```

## 4. Framework 层设计

### 4.1 `find_cross_source(content, source_manager, current_source, keyword) -> CrossSourceResult`

输入：
- `content`：`Content` 实例（复用 `search_one` / `fetch_detail`）
- `source_manager`：源加载管理（取候选源列表）
- `current_source`：当前源 `SourceConfig`（排除自身）
- `keyword`：当前作品标题

处理流程：
1. 候选源过滤：`source_manager.enabled_sources()` 中满足
   - `content_type == current_source.content_type`
   - 有搜索能力（`endpoints.search` 或 `api_endpoints.search` 存在）
   - `source_id != current_source.source_id`
   - 未软删除
2. 并发搜索：同一时刻最多 3 源并行（`ThreadPoolExecutor(max_workers=3)`），
   每源 `content.search_one(source, keyword)` 取结果
3. 每条结果清洗标题 + 算相似度，`>=0.90` 保留为候选
4. 聚合输出，分类记录：

```python
@dataclass
class CrossSourceCandidate:
    source_id: str
    source_name: str
    title: str          # 清洗后标题
    raw_title: str      # 源站原始标题（展示用）
    url: str
    cover: str
    author: str
    similarity: float   # 0~1

@dataclass
class CrossSourceResult:
    matches: List[CrossSourceCandidate]      # 相似度达标候选
    skipped: List[dict]                      # 无搜索能力/被排除的源（含原因）
    failed: List[dict]                       # 搜索抛错的源（含错误信息）
    empty: List[dict]                        # 搜索成功但无结果的源
```

并发保护：
- 单源搜索异常 → 捕获，记录到 `failed`，不影响其他源
- 全源完成时长有上限（每源遵守其自身超时配置），不做额外超时

### 4.2 `clean_title(title) -> str`

标题噪音清洗规则表（正则，按序应用）：
- 去掉首尾空白
- 去除常见源站后缀/前缀噪音：
  - `（更新至XX集）`、`(更新至 XX 集)`、`全集`、`全XX集`
  - `在线观看`、`高清`、`国语`、`中字`、`无修`（保留，同作品确认用）
  - `第X季`/`Season X`（**保留**——季区分用，勿删）
  - 视频源常见的 `[XX]` 站名前缀
  - 连续重复标点压缩

注意：`第X季` 是作品同一性的一部分（S1/S2 不是同一作品），**必须保留**。

### 4.3 `similarity(a, b) -> float`

```
difflib.SequenceMatcher(None, clean_a, clean_b).ratio()
```
阈值 0.90（可配置常量 `SIMILARITY_THRESHOLD = 0.90`）。
比对双方都先清洗（原始标题与当前标题）。

### 4.4 `switch_to_source(content, candidate, current_ep_no) -> tuple[Detail, list]`

- `content.fetch_detail(candidate.source_id 对应的 SourceConfig, candidate.url)` 取新源详情
- 视频类型：`chapters = detail.chapters`，目标集序号（0 基）`min(max(0, current_ep_no), len(chapters) - 1)`；`current_ep_no` 为 0 基（VideoView getter 直传）
- 漫画/小说：返回完整详情 + 章节列表（不保留页/章序号——不同源分卷粒度差异大，UI 层从第 0 章加载）
- 视频无分集（单章）或集数序号越界 → 从第 0 集加载

## 5. UI 层设计

### 5.1 `CrossSourceDialog`（新增 `gui/pages/reader/cross_source_dialog.py`）

模态对话框：
- 顶部：`QLineEdit` 搜索词（默认当前作品标题，可改）+ 「重新搜索」按钮
- 中部：候选卡列表（`QListWidget`，卡片含封面缩略图 + `源名 · 相似度%` + 清洗标题 + 原文标题），无候选时显示「未找到相似度≥90% 的作品」
- 底部：「查看明细」按钮 → 展开/独立框显示 `skipped/failed/empty` 明细；「取消」
- 交互：点击候选行 → `selected` 信号发出选中的 `CrossSourceCandidate`，对话框关闭

异步：打开时立即后台启动搜索（`QThreadPool`），`matches` 边搜边追加到列表（`signal.progress` 逐源增量上屏），全部结束后 `signal.done`。

### 5.2 视频入口（`video_view.py` 改动）

- `_refresh_source_menu()` 里，source_menu 末尾加分隔线 + `「⇄ 跨源换源」` Action（始终可见，不依赖多线路）
- 触发 → 弹 `CrossSourceDialog` → 选中候选 → emit 新信号
  `cross_source_chosen = Signal(object)`，payload = 选中的 `CrossSourceCandidate`（含相似度/源/URL/cover，不含集数）
- ReaderPage 监听 → 后台 `switch_to_source` → 成功后重置视频视图内容到新源
- 集数序号由 VideoView 提供公开 getter `current_episode_no()`（返回当前 0 基集序号），ReaderPage 调 `switch_to_source` 前读取传入——避免跨层直接触私有字段 `_current_idx`

### 5.3 漫画/小说入口

- `comic_view.py`：工具栏 `mode_btn`/`bg_btn` 旁加 `self.source_btn = QPushButton("⇄ 换源")`，信号 `source_switch_requested = Signal(object)`（发当前 detail）
- `novel_view.py`：工具栏 `font_up`/`bg_btn` 旁加同样 `source_btn` + 信号
- 两视图按钮默认隐藏，`load(detail)` 有 detail 时显示、空 detail 隐藏（无内容不可换源）

### 5.4 ReaderPage 换源调度（`reader_page.py` 改动）

三种视图共用一个后台任务模式（参考 `_SwitchSourceTask`）：
- `_CrossSourceTask(QtCore.QRunnable)`：调 `cross_source.switch_to_source(candidate, ep_no)`，信号 `done(SourceConfig, Detail, chapters, ep_no)` / `error(str)`
- 成功后的视图切换（新源 source + detail 一并传入 `load`）：
  - 视频 → `self.video_view.load(new_source, detail, start_ep_url=..., restore_position=None)`（`load` 支持新源整体加载；`current_ep_no` 由 `_CrossSourceTask` 前一帧从 `video_view._current_idx` 读取传入）
  - 漫画 → `self.comic_view.load(new_source, detail)`（从第 0 章）
  - 小说 → `self.novel_view.load(new_source, detail)`（从第 0 章）
  - 成功后各视图的正常续读事件（chapter_changed/episode_changed → `_on_progress_signal`）自动用新源 URL 落盘，无需额外迁移
- 失败 → 状态条提示「跨源换源失败：原因」，阅读界面保持原状，用户可再换

## 6. 字体修复

### 6.1 问题
阅读界面列表项长文字被截断（`QListWidget` 默认单行省略）：
- `video_view.py`：`ep_list`（分集列表，200px 侧栏，84px 图标挤占文字）
- `novel_view.py` / `comic_view.py` / `epub_view.py`：`toc_list`（目录 180px）

### 6.2 方案
- 所有列表 `setWordWrap(True)` 让长标题换行完整显示
- `ep_list`：图标 `84×56` → `76×50`，标题获得更多水平空间
- `toc_list`：`setWordWrap(True)` + 关闭 `setUniformItemSizes`（若启用）让行高自适应
- 确认文本颜色通过 palette，不额外设颜色

## 7. 错误处理

| 场景 | 处理 |
|---|---|
| 某源搜索抛错 | 记 `failed`，其他源继续搜 |
| 全部源无 ≥90% 候选 | 明细弹窗展示各源结果 |
| 换源抓详情失败 | 状态条提示原因，阅读保持原状，可再换 |
| 换源成功但该集播放失败 | 用户可再次触发跨源换源迭代 |
| 无候选源（如仅 1 个同类型源） | 直接提示「没有可搜索的其他同类型源」 |

## 8. 测试计划

- `tests/test_cross_source.py`（新增）：
  - `clean_title`：噪音去除、`第X季` 保留、空/None 输入
  - `similarity`：相同=1.0、`第X季` 不同时 <0.9、带噪音真实标题 >0.9
  - `find_cross_source`（mock）：候选源过滤逻辑、并发上限、异常隔离、结聚合
  - `switch_to_source`：详情抓取、视频集数序号保留、边界（0 集）
- GUI 冒烟：
  - `gui/_smoke_reader.py` 扩展：三视图 `source_btn` 存在/可见性、对话框打开、候选渲染、无结果分支
- 回归：`pytest tests`（当前 106 全绿基线）

## 9. 范围裁剪（YAGNI）

- 不做自动换源 / 失败自动触发
- 不做跨类型转换（视频不换去漫画源等）
- 不做 epub 本地书换源（无搜索源）
- 不做跨源书签/续读迁移（新源 URL 正常走已有续读记忆）
- 不做「记住用户上次换源偏好」
- 不对相似度结果做多个版本的加权排序（列表让用户自己选）

## 10. 验收标准

1. 视频阅读器 `⇄` 菜单出现「跨源换源」，点击弹候选对话框，选中后播放器/分集/封面切到新源，集数序号智能保留
2. 漫画/小说阅读器工具条出现「⇄ 换源」，点击弹候选对话框，选中后章节列表/正文切到新源
3. 候选仅展示相似度 ≥90%（清洗后）结果，候选卡含封面+源名+相似度
4. 全源无结果 → 明细弹窗展示各源原因
5. 阅读界面所有列表项长标题换行完整显示，不再截断
6. 现有 106 个测试全部通过；新增测试覆盖核心逻辑