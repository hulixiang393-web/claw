# 视频分集推荐功能 设计文档

日期：2026-09-08
状态：待审批（用户）

## 背景

视频阅读页（`VideoView`）侧栏目前只有「封面 + 分集列表」。需求是**同一播放源内推荐相关视频**：在当前作品分集列表下方展示来源站的推荐/相关作品，点击即可换到另一部作品继续观看。

## 现状调研

- `VideoView` 构造仅接收 `content: Content`（`reader_page.py:147`），不持有 `Search`、`SourceManager`。
- `ReaderPage` 构造接收 `source_manager`/`content`/`reading_progress`/`font_scale`（`reader_page.py:88-95`），不持有 `Search`。
- `app.py:195` 已创建全局 `self.search = Search(...)`，但未注入阅读器。
- Detail 结构（`content.py:82-99`）：含 `title`/`author`/`summary`/`tags`/`content_type`。`fetch_detail` 已解析 `summary`、`tags`（`content.py:338-342`）。
- 换源站（`source_list`）有多个线路 `sid`，但都归属同一源配置；推荐应在「当前源」内搜索（用 `SourceConfig` 本身，不区分 sid）。

## API 支持

- `Search.search_one(source, keyword, http=None, on_page=None)`（`search.py:80`）→ `List[SearchResult]`。HTML 源/API 源均走此入口，没有搜索配置的源返回 `[]`（`search.py:187`）。
- `SearchResult`（`search.py:36`）：含 `source_id`/`title`/`url`/`cover` 等。
- 未接缓存（`search_one` 而非 `search_one_cached`），与用户「搜索实时」决策一致。

## 设计

### 1. 注入 Search 服务

- `ReaderPage.__init__` 增加可选参数 `search=None`（`app.py:_build_reader` 传入 `self.search`）。
- `VideoView.__init__` 增加可选参数 `search=None`。
- `reader_page.py:_build_ui` 处把 `search` 传给 `video_view`。

### 2. 侧栏推荐区

在 `cover_label` 与 `ep_list` 之间（`video_view.py:214-222`）插入：

- `rec_label = QLabel("相关推荐")`（隐藏，样式同封面旁标题，`font-weight: bold`）
- `rec_list = QListWidget`（固定高度约 150，`setIconSize(QSize(76, 50))`、`setWordWrap(True)`、点击 → `_on_rec_clicked`）

推荐区默认隐藏；仅当 `self._search 且 self._source 且 detail.title` 时显示并触发加载。

### 3. 推荐加载流程

- `_RecommendSignals` / `_RecommendTask(QRunnable)`：后台 `search.search_one(source, keyword)`，signal 回传 `(source, results, err, keyword)`。
- 关键词选取：优先 `detail.tags[0]`（站点关键词，命中率高）；无 tag 退 `title`（截断到 30 字防超长）。
- 触发时机：`load()` / `reload_detail()` 成功后（换源完成也要刷新）。
- 结果过滤：剔除 `result.url == detail.url`（自己）；取前 N=8 条。
- 渲染：`rec_list` 填 item（`title` 文本），图标走 `CoverLoader.load(source_id=..., url=result.cover)` 异步按 `RecItem_URL` 回填（复用现有封面加载机制，同源 key 不冲突）。
- 失败/空results：隐藏推荐区（静默，不打断播放）。
- 防重：`_recommend_keyword` 记录当前关键词，重复触发时不重复搜索。

### 4. 点击推荐项

- `_on_rec_clicked(item)`：取 item 存的 `SearchResult`，若 `url != detail.url` 发信号 `recommend_open_requested.emit(source_id, url, content_type)`。
- `ReaderPage` 连接该信号 → `self.open(source_id, url, "video")`（复用现有 open 流程，切书前自动落盘当前进度、后台拉详情、换视图）。

## 明确不动

| 位置 | 说明 |
|---|---|
| `ep_list` 分集列表交互 | 不变 |
| 播放/换源/选集逻辑 | 不变 |
| `Search` 缓存 | `search_one` 直查，不碰 `search_one_cached` |
| 发现/搜索页 | 不动 |

## 验收标准

1. 打开任意有搜索能力的视频源作品 → 分集列表上方显示「相关推荐」，含 1~N 条同站作品（缩略图 + 标题）。
2. 点击推荐项 → 阅读页切到该作品（续读/进度记忆照常），不崩、不卡 UI。
3. 无搜索配置的源（search 返回空）→ 推荐区隐藏，不影响正常播放。
4. 换源（⇄）后推荐区按新源线路刷新。
5. `pytest tests/` 全量通过；`gui/_smoke_reader.py` 通过（新增推荐区断言：search=None 时不出现）。

## 风险

- `CoverLoader` 回填需按结果 URL 索引，要存 `{item: url}` 映射（避免 items 与结果错位）。
- 推荐搜索是后台线程，用户可能连续换作品；用 `_recommend_keyword` 去重即可，不需 epoch（结果按当前 detail 渲染时校验 `url == self._detail.url`）。