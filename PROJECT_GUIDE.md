# 多源阅读项目产品与开发总说明

版本：1.2
适用范围：当前仓库的桌面阅读客户端、通用抓取框架、源配置、缓存、下载与阅读器
维护原则：本文档是项目唯一的产品设计、架构说明和源制作参考文档。

## 1. 文档目的

本文档把项目的产品定位、交互界面、功能边界、代码职责、数据流、缓存策略、源配置规范、测试方法和已知限制统一记录。后续修改功能或制作新源时，先以本文档和当前代码为准，再检查目标网站的实时结构；不要把旧的站点分析、临时修复计划或历史推测当成实现事实。

文档中的状态约定：

| 标记 | 含义 |
|---|---|
| 已实现 | 当前代码已有对应逻辑，并且可以通过界面或测试验证 |
| 预留 | 配置或接口已经存在，但当前界面/运行链路尚未完整启用 |
| 受限 | 功能存在，但受目标站点、登录、反爬、付费或外部播放器影响 |

### 1.1 修改前必读

1. 先读本文件中与目标功能对应的界面、框架模块和数据流。
2. 再读目标源 JSON 和通用解析器；源问题优先修源配置，不要在页面层写站点特判。
3. 修改共享解析逻辑时，搜索所有调用方并增加最小回归测试。
4. 站点结构变化必须以真实响应、真实 DOM 或真实 API 为证据；不能只根据 HTTP 状态码判断可读性。
5. 任何缓存字段、排序规则、标题清洗和正文格式化的改动，都要验证详情、目录、首章、末章和重新打开缓存的行为。

## 2. 产品定位与设计理念

### 2.1 产品定位

这是一个配置驱动的多源内容阅读客户端：用户可以从多个小说、漫画和视频站点发现内容、搜索作品、查看详情、阅读/播放、下载并加入书架。站点差异尽可能通过 `sources/*.json` 描述，Python 框架提供通用的请求、解析、缓存、解密、渲染和调度能力。

核心口号：**改站点主要改 JSON，改跨站能力才改 Python。**

### 2.2 设计原则

| 原则 | 设计含义 | 代码落点 |
|---|---|---|
| 配置驱动 | 选择器、URL、请求头、分页、渲染和解密目标由源声明 | `sources/*.json`、`framework/config.py` |
| 统一模型 | 不同站点的作品、详情、章节、正文和媒体流转换为统一对象 | `framework/discovery.py`、`framework/content/` |
| 页面与抓取解耦 | Qt 页面只负责状态、展示和交互，网络工作放入后台任务 | `gui/pages/`、`QThreadPool`/`QRunnable` |
| 单源隔离 | 一个源超时、改版或反爬时，不阻塞其它源 | `framework/errors.py`、健康状态、页面错误回传 |
| 缓存优先但不假缓存 | 可复用的详情、目录、正文、图片和发现结果持久化；首次数据仍遵循源的真实加载规则 | `framework/cache_service.py`、`framework/shelf_cache_repository.py` |
| 按需加载 | 首屏先可见，重任务后台执行；非当前页面不提前创建 | `gui/app.py`、页面惰性构建 |
| 可观察失败 | 解析为空、源结构变更、网络失败和付费正文要显示原因，不伪造成功 | `ContentMissingError`、诊断页、错误状态 |
| 资源安全 | Cookie、缓存、下载和导出路径必须边界明确；不把用户凭据写入源 JSON | `SourceConfig.request_headers()`、原子写入 |
| 阅读优先 | 正文层次、图片、标题、章节顺序和恢复位置比装饰性动画更重要 | 各 Reader View、阅读进度 |

### 2.3 不应该做的事

- 不在 GUI 卡片、阅读器或源页面中硬编码站点选择器。
- 不为了“看起来成功”把空正文、验证码页或 VIP 截断内容当成完整内容。
- 不在应用启动时同步请求所有源、所有详情或所有正文。
- 不为尚未测量的性能问题引入复杂缓存、线程或第三方依赖。
- 不把登录 Cookie、Cloudflare 验证 Cookie、代理密码和个人数据提交到源配置或测试夹具。

## 3. 用户能力地图

```text
启动
  └─ 首页（立即可见）
       ├─ 搜索历史 / 观影历史
       ├─ 发现
       │    ├─ 类型筛选 ─ 源筛选 ─ 分类 ─ 卡片 ─ 详情
       │    └─ 按需分页 / 封面补齐 / 源健康状态
       ├─ 搜索
       │    ├─ 小说 / 漫画 / 视频
       │    ├─ 单源或多源 ─ 分源状态 ─ 结果卡片
       │    └─ 详情 ─ 阅读器 / 播放器 / 下载 / 收藏
       ├─ 阅读器
       │    ├─ 小说 / 漫画 / 视频 / 本地 EPUB
       │    └─ 目录、章节切换、阅读设置、进度恢复
       ├─ 下载
       ├─ 书架
       ├─ 源管理 / 源编辑器
       └─ 设置
```

统一的作品闭环是：

`发现或搜索 -> Work 卡片 -> Detail 详情 -> Chapter/Media -> Reader/Player -> 阅读进度/书架/下载`

## 4. 系统架构

### 4.1 分层

```text
┌──────────────────────────────────────────┐
│ Qt GUI：MainWindow、页面、卡片、阅读器      │
├──────────────────────────────────────────┤
│ 业务编排：Discovery、Search、Content、下载  │
├──────────────────────────────────────────┤
│ 通用执行：HTTP、Parser、Playwright、解密     │
├──────────────────────────────────────────┤
│ 持久化：源配置、搜索/发现缓存、书架缓存、进度 │
└──────────────────────────────────────────┘
```

依赖方向应保持：

`GUI -> 业务编排 -> 通用执行 -> 配置/持久化`

框架层不依赖具体 GUI 控件；源 JSON 不依赖页面实现。跨层需求应通过明确的数据对象、信号或服务方法连接，而不是互相读取私有控件。

### 4.2 关键目录

| 路径 | 职责 |
|---|---|
| `gui/app.py` | 应用入口、主窗口、运行时服务、主题、生命周期、启动顺序 |
| `gui/pages/` | 首页、发现、搜索、阅读、下载、书架、源管理、设置 |
| `gui/pages/reader/` | 小说、漫画、视频、EPUB 阅读/播放视图 |
| `gui/components/` | 卡片、封面加载、通用控件、最近搜索等 |
| `framework/config.py` | 读取和验证源配置，提供源便捷访问 |
| `framework/source_manager.py` | 扫描源、过滤启用状态、类型索引、健康状态和源编辑变更 |
| `framework/http.py` | 会话、请求头、Cookie、超时、重试、间隔、代理和响应处理 |
| `framework/parser.py` | CSS/XPath/fallback 选择器、属性、HTML、正则和 URL 归一化提取 |
| `framework/discovery.py` | 分类、发现列表、卡片作品、API/HTML/Playwright 列表解析 |
| `framework/search.py` | 单源/多源搜索、分页、结果合并、失败隔离 |
| `framework/content/` | 详情、章节、正文、漫画页、视频流、图片和 BookStack Markdown |
| `framework/decrypter.py` | 配置驱动的解密调用和自定义解密策略 |
| `framework/adblock.py` | 正文、图片和视频广告过滤规则 |
| `framework/cache_service.py` | 内存缓存、搜索/发现缓存和缓存池管理 |
| `framework/shelf_cache_repository.py` | 书架作品、详情、章节、图片、媒体位置的持久化缓存 |
| `framework/download_queue.py` / `downloader.py` | 下载任务、暂停/继续/重试/取消和文件落盘 |
| `framework/library_store.py` | 书架、收藏夹、锁定、导入导出和状态 |
| `framework/reading_progress.py` | 小说/漫画/视频当天续读位置和视频播放位置 |
| `framework/playwright_helper.py` | 浏览器复用、渲染线程、Cookie、页面和图片/文本抓取 |
| `sources/*.json` | 各网站的唯一站点知识 |
| `tests/` | 单元、配置、离线夹具、源回归和 GUI 行为测试 |

### 4.3 统一数据对象

| 对象 | 主要字段 | 用途 |
|---|---|---|
| `SourceConfig` | `source_id/source_name/content_type/raw/enabled/weight` | 一个源的结构化配置和运行时访问器 |
| `Category` | `title/url` | 发现页分类按钮 |
| `Work` | `title/url/cover/author/update/source_id` | 发现/搜索卡片的统一作品 |
| `SearchResult` | 作品字段 + 搜索源信息 | 搜索结果卡片和跨源合并 |
| `Detail` | `title/author/cover/summary/tags/status/chapters` | 详情抽屉和阅读器入口 |
| `Chapter` | `title/url/index` | 目录、章节切换、下载和进度 |
| `BookStackChapterResult` | `body_html/text/image_urls` | Markdown 富文本正文和纯文本兼容 |
| `ReadingProgress` 记录 | `book_url/chapter_url/page/position/location` | 重新打开作品时恢复章节与位置 |
| `DownloadTask` | 作品、选中章节、状态、进度、失败章节 | 下载页和持久化任务历史 |

## 5. 启动、生命周期与加载顺序

### 5.1 启动目标

启动不能把所有网络请求串在窗口显示之前。主窗口和首页先创建，运行时服务在 `_StartupTask` 中后台准备；服务发布成功后，再按优先级构造其它页面。

当前顺序：

1. 创建主窗口和首页骨架，首页不等待所有源完成。
2. 后台准备配置、HTTP、源管理、搜索缓存、书架缓存、阅读进度、下载队列等运行时服务；网络服务发布后，书架迁移和载荷索引扫描继续在后台执行，不阻塞源/发现首屏。
3. 运行时服务原子发布后，构造发现页；发现页把 BookStack 排在源列表第一位并触发其首个分类请求。
4. 下一轮 UI 事件构造源管理页。
5. 再下一轮构造书架页；书架相关持久化缓存服务已经在运行时服务阶段准备。
6. 最后构造设置页。
7. 用户切换到其它标签时，只刷新当前页需要的数据；页面已经存在时不重复创建。

这是“首页优先、BookStack 发现优先、源管理其次、书架/缓存随后、设置最后”的实现策略。它不代表所有内容都在启动时抓取：源列表、分类、卡片详情和正文仍按需请求，缓存命中才直接复用。

### 5.2 失败与重试

- 运行时服务构造失败时，不能发布半套服务；`MainWindow` 保留失败状态并允许重试。
- 关闭应用时停止任务、关闭 HTTP 和书架缓存仓库；后台清理不应阻塞首页退出。
- 迟到的后台结果必须带 generation/epoch 校验，不能覆盖新一轮页面状态。
- 页面初始化失败应显示可读错误，不影响其它标签。

### 5.3 页面加载状态

每个异步页面至少区分：`未加载`、`加载中`、`已有缓存`、`成功`、`空结果`、`网络失败`、`结构变更/反爬`。加载卡片时保持网格列数、滚动比例和卡片占位尺寸，避免因逐个插入控件造成窗口抖动。

## 6. 界面与交互说明

### 6.1 主窗口

实现位置：`gui/app.py`。

职责：

- 提供主导航和页面容器。
- 管理主题、全局字体缩放、背景图和窗口级 QSS。
- 创建运行时服务并注入页面。
- 处理从首页、搜索、发现、书架到阅读器的导航请求。
- 统一处理应用关闭、任务回收和未完成操作。

设计理念：导航只负责切换和依赖注入，不负责抓取站点；页面间通过统一作品/详情对象和事件总线通信。

### 6.2 首页

实现位置：`gui/pages/home_page.py`。

可见能力：

- 品牌入口和快捷进入发现、搜索、设置。
- 最近搜索历史：点击重新搜索，支持单条删除和清空。
- 观影/阅读历史：显示最近记录，支持单条移除和清空。
- 源健康概览和批量更新检查入口。
- 无数据、运行时准备中、源不可用等状态提示。

设计理念：首页是冷启动的“可用证明”，先显示静态/缓存内容，网络失败不能让窗口空白。历史数据是轻量导航，不把首页变成全量推荐瀑布流。

### 6.3 发现页

实现位置：`gui/pages/discover_page.py`。

交互层级：内容类型 -> 源 -> 分类 -> 作品卡片 -> 详情。

功能：

- 小说、漫画、视频类型筛选。
- 源下拉/按钮选择，BookStack 作为当前启动优先源。
- 源分类加载和“全部”入口。
- 分页、滚动加载、缓存快照恢复和滚动位置恢复。
- 作品卡片显示标题、封面、作者/更新信息和源标签。
- 封面缺失时按配置进行详情回填、解密或备用封面恢复。
- 源健康图标、挑战/反爬提示和单源错误隔离。

设计理念：分类和列表先可用，详情只在用户点击时加载；卡片按批次插入，先确定网格列数和布局再填充，防止加载过程产生窗口抖动。

### 6.4 搜索页

实现位置：`gui/pages/search_page.py`。

功能：

- 小说、漫画、视频类型切换。
- 单源、全类型多源或选定源组合搜索。
- 各源独立状态：等待、加载、成功、空结果、失败、挑战阻断。
- 跨源结果合并、源筛选、分页和批量选择。
- 结果卡片打开详情、加入书架、批量下载。
- 封面解密和详情回填在后台补齐。

设计理念：多源搜索不能因一个站点失败而整体失败；搜索页面先稳定显示结果批次，再追加后续批次。搜索历史写入轻量存储，详细正文不在搜索阶段抓取。

### 6.5 详情视图与阅读入口

详情通常由发现/搜索页打开，显示标题、作者、封面、简介、标签、状态、来源和章节目录。

操作：

- 打开小说、漫画、视频或本地 EPUB 阅读器。
- 收藏/取消收藏。
- 选择章节并开始阅读。
- 进入下载选择。
- 刷新详情和源切换（视频支持更换播放源）。

设计理念：详情是内容确认层，目录可以完整展开后排序；阅读器不应依赖卡片列表里的不完整数据。

### 6.6 统一阅读页

实现位置：`gui/pages/reader_page.py`。

顶部公共区域：作品标题、源名称、下载、收藏、刷新内容、打开源详情、本地 EPUB 入口，以及具体 Reader View 的主体。

通用状态：加载中、正文为空、源不存在、请求失败、章节不存在、切换源失败。阅读器切换时应保留作品上下文、章节索引和阅读进度。

### 6.7 小说阅读器

实现位置：`gui/pages/reader/novel_view.py`，正文获取主要在 `framework/content/chapter.py`。

功能：

- 章节目录按钮，章节完整展开并使用通用章节排序。
- 书名和当前章节标题固定在正文顶部，避免标题与章节信息错位或显示不完整。
- 滚动模式与翻页模式。
- 上一章/下一章、翻页模式的上一页/下一页和页码指示。
- 字号增减、字体样式、背景切换、阅读设置、全屏。
- 自动滚动及速度调节。
- 首行缩进按两个汉字宽度处理，不使用过大的固定缩进。
- 正文段落清洗、空白归一化、广告过滤、图片提取和正文图片显示。
- 章节预加载、向前/向后缓存和阅读位置恢复。
- BookStack Markdown 正文使用富文本 HTML 渲染，保留标题、列表、代码块、链接和图片。

排版原则：正文渲染使用真正的段落节点或已分段的内容，不能把整章拼成一个 QLabel 文本导致所有段落堆叠；图片作为段落流中的独立资源，不能覆盖或挤乱文字。正文颜色必须由阅读背景和主题共同决定，不能让按钮样式遮挡“背景”文本。

### 6.8 漫画阅读器

实现位置：`gui/pages/reader/comic_view.py`。

功能：

- 章节目录、上一话/下一话、章节进度和图片数量。
- 纵向画廊与横向模式切换。
- 图片懒加载、失败提示、缩放和全屏。
- 自动滚动及速度控制。
- 图片 URL 解密、懒加载属性识别、广告图过滤和页面顺序恢复。

设计理念：漫画顺序以源目录和页面序号为准；网络加载、解密和图片解码在后台完成，UI 只接收当前章节的稳定快照。

### 6.9 视频播放器

实现位置：`gui/pages/reader/video_view.py`，流解析主要在 `framework/content/video.py`。

功能：

- 播放/暂停、进度、音量、全屏。
- 选集、相关推荐和播放源切换。
- 播放源检测、质量选择和外部播放器回退。
- HLS/DASH/MP4 等候选流解析、广告帧/广告图片过滤。
- 播放位置记录和重新打开续播。
- 下一集预取：当前集结束前 4 分钟（`PREFETCH_BEFORE_END_S`）后台解析下一集播放链接，缓存时长覆盖预取窗口（普通链接 6 分钟，带时效签名按 `expire` 计算），并预热媒体代理 series memo；手动下一集与播完自动续播都应直接命中，不再现场解析。完整规则见 §6.9.1。
- 投屏/外部播放相关入口按当前环境和播放器能力启用。

限制：站点需要登录、付费、验证码、特殊 DRM 或外部播放器时，框架只能显示实际可取得的流和失败原因，不承诺绕过访问控制。

### 6.9.1 下一集预取（提前 4 分钟）的完整契约

实现位置：`gui/pages/reader/video_view.py` 的 `_prefetch_*` / `_progress_position` 槽，
媒体代理侧 `framework/media_proxy.py::series_episode_cached`。

预取的唯一目标是「用户切到下一集时不再等取流」，所以整条链路必须满足
**不延误本集播放**：

1. **时基两路**：进度回写（`on_progress` → `_progress_position` 信号，主线程槽）
   是主驱动；`PREFETCH_TICK_MS`（20s）巡检是兜底——VLC 在缓冲/暂停/静音时
   可能整段不发进度，只靠前者会让 4 分钟窗口整段空转。巡检只读一次
   `player_status()`，没有外播会话立即返回。
2. **锁存**：`(当前集下标, 画质) → (触发位置, 已用重试次数)`。同一集在同一个
   播放位置只触发一次；位置回拖超过 `PREFETCH_LATCH_TOLERANCE_S`（2s）才重新
   判定。没有锁存时，进度在窗口边界来回进出会反复取流，这是「预取实际没生效」
   的根因之一。锁存键是**触发时的当前集**，命中下一集的缓存要用 `idx + 1` 比较。
3. **串行**：同时只允许一路预取（`_prefetch_idx >= 0` 即挡）。预取请求与本集
   取流共用源站配额，并发会抢带宽，表现为「预取一开就卡」。
4. **不重复解析同一集**：进入下一集时 `_on_stream_loaded` 调
   `_cancel_prefetch_for` 取消正在跑的同一集预取（`task.cancel()` 会 interrupt
   网络请求）；媒体代理 memo 里已有该集映射（`series_episode_cached`）时预取
   直接跳过——播放端不会再跑 resolver，预取无收益。
5. **失败语义**：`PREFETCH_MAX_ATTEMPTS`（3）次，每次退避翻倍
   （`PREFETCH_RETRY_S` 起），额度用完写 `_prefetch_hopeless`，该集在换集/换源
   （`_cancel_stream_tasks`）前不再尝试。冷却到点的重试由巡检或下一次进窗口
   补发——不能只依赖「再次进入窗口」，真实进度那时早跑完窗口了。
6. **缓存 TTL**：普通链接按 `窗口 + PREFETCH_CACHE_MARGIN_S` 缓存；带时效签名
   按 `expire` 推算；TTL 覆盖不了窗口时只缓存、不预热代理（否则自动续播会
   拿到过期地址）。取到链接后把它写进 `_stream_cache`，下一集点开即命中。

验收：`tests/test_video_prefetch_timing.py` 覆盖窗口边界、锁存、巡检兜底与
重试提额、串行、memo 命中跳过、与本集取流去重。

### 6.9.1.1 外部播放器的「下一集」（播放列表）

- `VideoView._play` 播放前注册惰性系列（`MediaProxy.register_series`），并把整部
  作品写成 m3u 播放列表交给播放后端：当前集用已铸好的 `/s/<token>`，其余集是
  `/e/<key>/<idx>`。播放器因此自带「下一集」，不必由 App 现场取流。
- mpv 拿整份列表 + `--playlist-start`（`player_session._prepare_playlist`），
  `PlayerSession.playlist_next/playlist_prev` 走 JSON IPC 的
  `playlist-next/prev weak`，`>`/`<` 由 `keybind` 绑定。VLC/PotPlayer 不认起始项，
  列表裁到当前集开头。
- 播放器切集时 `/e/` 的 `on_play` 回调经 `_series_played` 信号回主线程，更新
  `_current_idx`/选集高亮/`episode_changed`（进度记忆）；App 的切集按钮优先让
  播放器换项，播放器不支持时才退回 App 取流重开。因此播放列表模式下
  `_on_media_ended` **不得**再调下一集（会跳过一集），也不重开播放器。
- 预取命中链路：4 分钟窗口里取到的流地址经 `_warm_next_episode` 写进 series
  memo；播放器请求 `/e/<key>/<idx>` 时直接 302 到该地址（秒开）。TTL 覆盖不了
  窗口时不预热（§6.9.1 第 6 条）。
- DASH 双流（`audio_url` 非空）与 yt-dlp 引擎（`_source_needs_merged`）不给
  播放列表，退回原「App 取流 + 重开播放器」。
- **续播点不能交给 `--start`**：`start` 是**逐文件**选项，会应用到列表的每一项
  （2026-11-04 实测 mpv v0.41：`--start=4` + 两项列表，`playlist-next` 之后第 2
  项仍从第 4 秒起播）。续到 20 分钟再点下一集 = 每集都被切掉开头 20 分钟。
  列表模式下 `play()` 对播放器传 `start_s=0`，把续播点记在 `_pending_resume_s`，
  等当前集 `file-loaded` 之后发一次 `seek <秒> absolute`（只发一次，切集不重复
  应用；没有 IPC 的播放器照旧走 `--start`）。
- 验收：`tests/test_player_playlist.py`（m3u 生成/裁剪、mpv 起始项、下一集交给
  播放器、播放器切集回写当前集、续播点走 IPC seek 而非 `--start`）。

### 6.9.1.2 播放/阅读历史保留上限

- `ReadingProgress(max_items=…)`：未入书架的记录按最近使用时间倒序，最多保留
  N 条（影视+小说+漫画合计）；书架收藏永久保留且不占名额；0 = 不限制。
- 设置页「历史」→「最多保留(部)」（`history.max_items`，默认 20）；应用后
  `MainWindow._schedule_settings_refresh` 即时 `set_max_items` 裁剪，无需重启。
- 验收：`tests/test_history_limit.py`。

### 6.9.2 「卡顿恢复后跳帧/丢音」的责任边界

这个现象要先分清是**数据坏了**还是**播放器追帧**，否则会一直改错层。判定顺序：

1. **缓存分片完整性**：每个 188B 包按 PID 校验 TS 连续计数器（CC 应恒为 +1
   或 0，仅 adaptation-only 包不递增）。实测 669 片 0 缺包。
2. **音频是否被 remux 动过**：逐片对比音/视频包数与首末 PTS。实测音频包数
   逐片一致（173/173、201/201…）、音视频时长差 0.000s。
3. **PCR 是否与 PTS 同步平移**：PCR 跳变会让播放器丢弃音频重新对表。实测
   `PCR-PTS` 全程漂移 0.000。
4. **跨片 DTS 单调性**：整理后跨片不得倒退。实测 0 次倒退。

四项都干净 → 丢音发生在**播放侧**：输入饥饿后 VLC 的 aout 落后主时钟，只能
丢音频追帧（视频侧有 `--no-drop-late-frames`，音频没有等价开关）。因此
`external_player.open_with_player` 固定带两项音频加固（**不要删**）：

- `--clock-jitter=0`：走本地代理时时间戳已由 remux 对齐，VLC 的抖动平滑只会
  累积相位误差，饥饿恢复瞬间被判「迟到」→ 丢音频；关掉后直接用时间戳。
- `--audio-time-stretch`：显式开启变速追帧（默认即开，写死防止便携版 vlcrc
  关掉——关掉就是硬丢音频）。

**根治方向是「别让输入饥饿」**，不是继续调播放器：见 §6.9.1 预取与
`MediaProxy.prime_episode_segments`（把下一集首几片提前落盘）。验收：
`tests/test_vlc_audio_flags.py`（只断言参数，**不启动 VLC**——启动播放器会抢
用户焦点，任何验证都必须在纯 Python 层完成）。

#### 6.9.2.1 「看着看着音画不同步」= 播放侧时钟漂移，用 avsync 看门狗治（2026-11 omofuna）

现场（2026-11-02，`data/cache/video/cc6d9e63…`，339 片、1372s 一集）：

- 缓存目录里**没有** `.remuxed` 标记 → 这一集走的是纯透传，`_ts_shift_bytes`
  一次都没跑。也就是说这次不同步与代理的时间戳整理无关，不要再去改 remux。
- 逐片量首末 PTS：音视频偏移全程 0.000~0.023s、0 次 TS 连续计数错误 →
  分片数据本身是同步的。
- 清单 `#EXTINF` 一律 3.00s，而真实帧周期是 3.12s（75 帧@24fps，477 片累计少算
  约 57s）→ **清单时间轴与媒体时间轴差约 4%**。

结论：数据侧干净、清单侧偏短、播放器以音频为主时钟，长时间播放/多次续播之后
"音频比画面快一点"会累积到肉眼可辨。这不是丢帧（丢帧只影响流畅度），换线路、
换清晰度、重开播放器都只是重新计时，治不了根。

修法在 `framework/player_session.py` 的 `_on_avsync`（看门狗）：

- 观察 mpv 的 `avsync`（它自己算的音视频差值，正 = 音频领先）与 `audio-delay`；
- `audio-delay += avsync` 就是把音频拉回画面，纠偏量经 `sync_corrected` 信号报出；
- 防抖必须齐全，否则用户听到的是"声音一顿一顿跳"：起播 5s 内不判（起播抖动
  会报 0.2~0.5s 假偏差）、0.25~0.6s 需连续两次采样、两次纠偏间隔 ≥12s、
  一次会话最多 4 次（超了说明片源时序本身坏了，保持原样让用户换线路）；
- 用户附加参数里的 `--audio-delay=` 是**基线**，纠偏在其上叠加，不能抹成 0。
- **预热按墙钟算，不按 `time-pos`**（2026-11-04 加固）：续播/列表模式会把
  `time-pos` 直接顶到几十分钟，只看 `time-pos < 5000` 等于「一开播就算预热完」。
  现场日志 `音画漂移纠偏：avsync=-3.616s → audio-delay=-3.000s（第 1/4 次，
  播放到 5.0s）` 就是播放到 5.0s 的**第一个**采样被当真，声音被推飞 3 秒。
  现在 `_sync_armed_at` 在 `file-loaded` 记 monotonic，预热 5s（`AVSYNC_WARMUP_S`）。
- **单次采样 >1.5s 的偏差一律当起播噪声丢弃**（`AVSYNC_MAX_PLAUSIBLE_S`）：
  真漂移是慢慢累积的，不会一步跳到 3s；这类样本也不占用纠偏次数。
  累计纠偏量仍封顶 ±`AVSYNC_MAX_DELAY_S`。

验收：`tests/test_player_avsync.py`（预热期/暂停期不动作、单次尖峰不动作、
硬阈值一次到位、负数回退、基线叠加、冷却期、次数上限、切集重置、无 IPC 静默、
续播位置不跳过预热、>1.5s 噪声样本被丢弃）。

限界：`avsync` 是 mpv ≥0.36 的属性；观察失败只是收不到事件，不影响播放。
清单 `#EXTINF` 的 4% 偏差本身**没有**改（改它要按源抓分片实测再缩放整份清单，
每集多一轮请求、且引入新失败面），它的可见影响是时长/续播定位偏 4%，不在
不同步这条线上；若要动，必须先有「实测分片时长」的持久化测量再谈。

复核命令（都离线、不启播放器）：

```powershell
python tools\_probe_extinf.py <缓存目录>   # EXTINF vs 真实帧跨度、片内 a-v 偏移
python tools\_probe_av_sync.py <缓存目录>  # 逐片 a-v 偏移表 + 跨片连续性
```

`_probe_extinf.py` 在 `cc6d9e63…` 上的输出（2026-11-02）：周期样本 286，清单
`EXTINF` 中位 3.000s vs 真实周期中位 3.120s（**−3.8%**）；片内 a-v 偏移**恒定**
为 −0.072s（音频首 PTS 比视频早约 1.7 帧）。恒定偏移不会累积，属于站点封装
口径，**不触发**看门狗（阈值 0.25s）——若用户仍觉得"一直有点对不上嘴"，那是
固定唇音差，正确做法是在播放器附加参数里给 `--audio-delay`（现在会被当作基线
保留），不要为此把阈值调低。

#### 6.9.2.2 「画面重影」先分清片源混帧还是显示链路混帧（2026-11-04）

同一个词对应两种完全不同的根因，判据必须落在像素上：

1. **片源/文件混帧**：错帧率转换、blend 去隔行 → 每一帧都是相邻两帧的平均值。
   文件侧能直接测（时间混合测试）。
2. **显示链路混帧**：hwdec / GPU 合成 / 远程串流 / 合成器缩放 → 文件干净，屏幕上
   那一帧却是两帧的混合。只有把**屏幕实际像素**与解码帧对齐才能判。

本次实测（`downloads\苍兰诀2 影三界篇\苍兰诀2 影三界篇_001_1.mp4`，正在播放，
mpv v0.41 `--hwdec=auto-safe`）：

- 文件侧：1920x1080 / yuv420p / **progressive** / 25fps CFR；`idet` 多帧 TFF/BFF
  均为 0；视频包 35776 个、DTS 步长恒 0.04s、0 次倒退；4s 窗口内无重复帧；
  640s 处时间混合测试 `d(blend)/d(邻帧) = 3.4`（>1 即"不是混帧"）。
- 显示侧：抓 mpv 窗口 → 在当前位置前后抽解码帧做最佳匹配，14 个样本中运动足够
  的样本 `d_self` 1.1~2.2、`d_blend` 明显更大（例：motion=18.1 时
  `d_self=1.30` / `d_blend=21.4`）→ 屏幕上那一帧就是**单帧**。

结论：当时这条链路（本地文件 + mpv + 本机 GPU）**没有**重影，别再凭感觉改渲染
参数。复现时按下面命令取证：

```powershell
python tools\_probe_ghosting.py                        # 自动找 mpv 窗口与 IPC
python tools\_probe_ghosting.py --samples 20 --interval 1.0
```

判据：运动足够（相邻帧差 >1.5）且 `d_blend < min(d_prev,d_next)` → 混帧（显示
链路）；`d_self` 最小 → 单帧（正常）；静止镜头样本没有判定力，探针标
`inconclusive`。探针只读截屏、不改播放状态、不启动播放器（要先在 App 里开播）。

#### 6.9.2.3 播放中进度落盘按 5 秒节流（UI 线程别整块写盘）

`MainWindow._play_local_video` 的 `changed()` 原来每次 `time-pos` 都调
`reading_progress.save`，而落盘是 `shelf_cache_repository.upsert_book` +
`prune_video_history` + `atomic_io.atomic_write_json` 的整块 JSON 写。`ui_perf`
日志 16:00–16:10 有 **173 次 STALL**（单次 573~1337ms），栈全在
`reading_progress.save` → 播放中界面等于幻灯片。

现在按「播放进度前进 5 秒」节流（`save_mark`）；切集（`followed_position`）重置
标记立刻落盘，暂停/开头仍走 `handed_off()`。代价是最多丢最后 5 秒的位置。

### 6.9.3 内嵌播放器（外播 → 内嵌的架构约定）

播放**不再拉起外部 VLC 进程**，画面由 `framework/vlc_player.VlcPlayer`
（libvlc `set_hwnd`）嵌进 `VideoView._video_frame`。外部播放器时代的第二窗口、
HTTP 控制接口、惰性系列握手全部退场。

**播放链路（唯一入口 `VideoView._play`）**

1. 取源 `request_headers()` → `_configure_playback_prefetch(video)`（HLS 预取）。
2. `_progress_options()` 读一次续播位置：`seconds` 优先，否则用 `position` 比例
   落到 `_pending_seek_s/_pending_seek_ratio`；同时取出回写回调 `_progress_cb`。
   **只能取一次**（该函数有「消费即清」语义）。
3. `_embedded_play_url()` 决定直连或走本地代理：HLS、带 Referer/Cookie、
   强制代理三种情况都走 `MediaProxy.build_url`（广告过滤 + 分片缓存 + 补头）。
4. `_ensure_player(hdrs)` 取/建播放器（referer/UA 变化才重建）→ `play(url, audio)`。
5. 成功才置 `_has_played=True` 并解禁控制条；`play()` 抛异常不记播放。

**进度与预取的两个驱动源**

- 事件：`time_changed` → `_on_player_time`（主驱动）。
- 兜底：`PROGRESS_POLL_MS`（1s）定时读 `get_time/get_length`，覆盖不发
  TimeChanged 的容器。
- 回写节流 `POSITION_SAVE_INTERVAL_S`（2s）；**预取时基不节流**（锁存负责去重）。
  回写时 payload 带 `engine: "embedded"`，`reader_page` 据此跳过外播会话校验。

**续播 seek** 必须等时长到手：`_on_player_length` 里按比例 `set_position`；
目标落在片尾 1s 内则从头播（跳到末尾等于立刻结束）。

**自动续集**：`ended` → `_on_media_ended` → 下一集；末集只提示。

**切集**：`_select_episode` → `_load_episode` → `_play`（播放器实例复用，只换
media）。**不再有** `pl_play` 指挥外部进程那条路径。

**释放**：`_stop_player()` 只 `release()`（停网络/断代理/放解码器，实例留着）；
`shutdown_video()` 才 `close()`。

验收：`tests/test_embedded_player.py`（21 项，全部用桩对象，**绝不创建 libvlc
实例**）。已删除 `tests/test_video_series_playlist.py`——它锁的是已移除的惰性
系列/握手架构，且会真实创建 libvlc 并发出播放请求。

**本地视频（书架里打开）走同一个引擎**：`gui/local_video_player.py` 的
`LocalVideoPlayer`（QDialog + `VlcPlayer`）。

- 全屏用**窗口自身** `showFullScreen()`，**不 reparent** → HWND 全程不变，
  不需要 `rehook`。VideoView 那条 reparent 全屏必须重挂，本地播放没必要。
- 回调：`on_progress(seconds, duration, position)`（2s 节流）、`on_handoff()`
  （开播落一次「最近阅读」）、`on_finished()`（播完）。
- 同一时刻只留一个本地播放窗口：换视频先 `close()` 旧的，避免两路音频叠加。
- App 退出：`aboutToQuit` → `_shutdown_local_player()`（先关窗口再
  `shutdown_vlc()`，避免两者抢同一个 Instance 的释放顺序）。
- 父级传非 `QWidget`（测试里的轻量窗口替身）时退化为无父窗口，不抛 TypeError。

**进度回写的归属校验**（`reader_page._on_progress_signal` 与
`app.MainWindow._on_local_progress` 同一套规则）：

- 内嵌播放的 payload 带 `engine: "embedded"` → 跳过外播会话校验，只按
  「这部作品/这一集」与观读状态（`_local_progress_allowed`）判定。
- 不带该标记的（外播历史路径、其它来源）仍必须 `session_id == id(player_session())`。

验收：`tests/test_local_video_player.py`（16 项，桩掉 `VlcPlayer`，不创建实例）。

### 6.10 EPUB 阅读器

实现位置：`gui/pages/reader/epub_view.py`。

功能：打开本地 EPUB、目录、章节切换、字号、背景、全屏和进度显示。它是本地内容路径，不依赖远程源，但复用统一阅读器的视觉和导航理念。

### 6.11 下载页

实现位置：`gui/pages/download_page.py`、`framework/download_queue.py`、`framework/downloader.py`。

功能：

- 显示等待、下载中、暂停、完成、失败、取消任务。
- 进度、速度、耗时、当前章节和错误摘要。
- 并发数调节。
- 暂停/继续、取消、重试失败章节、从头重下。
- 重启后从下载历史恢复任务状态。
- 小说保存文本/EPUB 路径，漫画保存图片，视频保存可用媒体或流地址清单，具体由下载器和源能力决定。

设计理念：下载是可恢复的后台任务，不阻塞阅读；同源章节并发受全局设置和源 `constraints.max_concurrency` 的较小值限制，反爬源应优先保证完整性。

### 6.12 书架页

实现位置：`gui/pages/library_page.py`、`framework/library_store.py`。

功能：

- 收藏夹、文件夹新建/重命名/删除。
- 文件夹锁定、密码/恢复码管理。
- 作品卡片、阅读状态、缓存章节数和继续阅读。
- 批量选择、移除收藏、清空当前收藏夹。
- 书架导出/导入和备份检查。
- 本地内容与远程作品统一展示。

设计理念：书架是用户长期数据，元数据和阅读位置应持久化；网络不可用时仍显示缓存卡片，进入正文时再报告是否有离线内容。

卡片与子控件必须在构造时就带 Qt 父对象（`_ShelfCard(rec, parent)`、`QLabel(text, parent)`），不能先创建无父控件再交给布局 reparent：无父 `QWidget` 在 Qt 中就是顶层窗口，批量建卡时会短暂冒出大量标题为 `pythonw` 的系统窗口（用户看到的「弹窗」），随后才被布局清除。已锁定收藏夹首次加载时只弹一次密码框；用户取消后直接还原下拉选择，不再连环弹「管理员解锁 / 设置密码 / 恢复码」。

### 6.13 源管理页

实现位置：`gui/pages/source_page.py`。

功能：

- 查看源名称、类型、启用状态、权重、健康历史。
- 全启用、全禁用、成人源显示控制。
- 添加源、导入 JSON、编辑、删除。
- 单源诊断和全部诊断。
- 登录入口（源声明支持时）和源版本/描述展示。

设计理念：源是可替换插件，管理页提供可见的健康反馈；删除源前需要确认，诊断失败不等同于立即删除源。

### 6.14 源编辑器

源编辑器由源管理页打开，负责读取、编辑、校验和保存单个 JSON。保存前应验证 `$id`、`$type`、必填字段和 JSON 结构；编辑器必须保留未认识的扩展字段，避免往返保存损坏媒体、解密或站点自定义配置。

编辑流程：加载 -> 修改 -> 校验 -> 预览/诊断 -> 保存 -> 刷新 SourceManager。配置变更后要增加缓存版本或清理受影响缓存，避免旧 HTML 继续被当作新结构。

### 6.15 设置页

实现位置：`gui/pages/settings_page.py`。

分区和主要字段：

| 分区 | 设置 |
|---|---|
| 网络 | User-Agent、超时、重试、请求间隔、代理、并发搜索、图片大小上限 |
| UI | 主题、全局字体缩放、封面缓存大小、默认页大小、背景图、透明度、阅读背景色、阅读字号、缓存清理 |
| 下载 | 输出目录、命名模板、合并章节、跳过已存在、下载并发、同源章节并发 |
| 书架 | 书架导出目录、WebDAV 地址（预留） |
| 诊断 | 日志级别/目录、坏源告警间隔、自动禁用阈值、自检策略 |
| 广告规则 | 额外规则目录 |
| 内容 | 是否显示 18+ 源 |
| 阅读 | 章节预加载、向前/向后预加载、正文图片 |

设计理念：全局设置只描述用户偏好和运行默认值，站点差异仍在源 JSON；设置页统一应用主题、字号和缓存策略，首页、源管理和设置文字保持黑色可读，阅读正文则根据背景选择对比色。

独立加载：`SettingsPage` 在加载时记录每个设置项的基线值，并为每项维护布尔修改标记 `_dirty_flags`。点「应用」时逐项比较，只把值发生变化的设置写回 `app_config.json` 并通过 `settings_applied(frozenset(changed))` 通知 App 层；App 层只刷新受影响的主题/字号/缓存/可见性，未变化的设置项既不重写也不触发全局刷新。

## 7. 源配置规范（v2）

### 7.1 最小骨架

```json
{
  "$schema_version": 2,
  "$id": "example-source",
  "$type": "novel",
  "$name": "示例源",
  "$enabled": true,
  "$weight": 1.0,
  "$metadata": {
    "homepage": "https://example.com",
    "description": "站点说明",
    "tags": ["小说"],
    "lang": "zh-CN",
    "region": "cn"
  },
  "transports": {
    "base_url": "https://example.com",
    "timeout": 20,
    "retries": 2,
    "interval_ms": 500,
    "headers": {}
  },
  "endpoints": {}
}
```

`$id` 只能使用小写字母、数字、下划线和连字符；它同时用于缓存键、健康记录和文件安全边界。

### 7.2 顶层字段

| 字段 | 必需 | 说明 |
|---|---|---|
| `$schema_version` | 建议 | 当前使用 2 |
| `$id` | 是 | 稳定唯一 ID，不随展示名称改变 |
| `$type` | 是 | `novel`、`comic`、`video` |
| `$name` | 是 | UI 展示名称 |
| `$enabled` | 否 | 是否参与发现/搜索/管理 |
| `$weight` | 否 | 结果排序权重 |
| `$metadata` | 否 | 首页、源管理和诊断展示信息 |
| `transports` | 是 | 基础 URL、头、Cookie、超时、重试、代理、浏览器策略 |
| `endpoints` | 按能力 | HTML 页面和正文规则 |
| `api_endpoints` | 按能力 | JSON/API 规则 |
| `media` | 视频/图片 | 播放、图片、质量和外部媒体策略 |
| `decryption` | 需要时 | 封面、章节、图片或播放地址解密 |
| `ad_block` | 需要时 | 广告选择器、URL 或媒体过滤规则 |
| `constraints` | 建议 | 页数、条数、并发和站点限制 |
| `diagnostics` | 建议 | 自检策略、提示和最后验证信息 |

### 7.3 transports

常用字段：`base_url`、`headers`、`cookie`、`timeout`、`retries`、`interval_ms`、`charset`、`follow_redirects`、`direct`、`proxy`、`proxy_pool`。

请求头合并顺序是源级配置、Cloudflare Cookie 文件、登录 Cookie 提供器等运行时信息；不要在 JSON 中提交个人会话。`{buvid3}` 这类动态占位符由 `SourceConfig.request_headers()` 生成。

### 7.4 discovery

HTML 发现通常配置：

```json
"discovery": {
  "list_url": "/category/latest",
  "render": "http",
  "list_paginator": {
    "type": "increment",
    "page_param": "page",
    "first_page_without_param": true
  },
  "works_list_item": {
    "root_selector": {"css": ".book-item"},
    "fields": {
      "title": {"css": ".title"},
      "url": {"css": "a", "attr": "href"},
      "cover": {"css": "img", "attr": "src"},
      "author": {"css": ".author"},
      "update": {"css": ".update"}
    }
  }
}
```

也可以用 `api_endpoints.discovery` 描述 JSON 列表、数据路径、字段路径和 URL 模板。发现页只取列表作品，不抓详情和正文。

### 7.5 search

常用字段：`base_url`、`method`、`keyword_param`、`headers`、`extra_params`、`paginator.param`、`paginator.start`、`paginator.url_template`、`item.root_selector`、`item.fields`、`render_config`。

`paginator.start` 是**第 1 页对应的站点页码值**，缺省 1（页码从 1 起，历史行为）。0 索引站点必须显式配 `"start": 0`：`xvideos` 搜索第 1 页是 `?k=kw&p=0`、第 2 页 `p=1`，`?page=N` 会被站点忽略（每页都回第 1 页），表现为「搜索结果只显示第一页 / 显示不完全」。0 是合法基准值，代码里不能用 `or 1` 兜底。

搜索结果字段至少需要标题和详情 URL；封面、作者、更新信息可选。动态搜索页必须先确认浏览器实际 DOM 和等待条件，再决定 `render: playwright`，不要仅因请求返回 200 就认为结果可解析。

API 搜索（`api_endpoints.search`）另有三个可选键：`exclude_providers` 按采集源 id 过滤结果、`group.providers` 提供采集源中文名与排序、`group.enabled` 开启按标题聚合线路（详见 §7.12.7）。

#### 7.5.1 JSON 数据体不做 HTML 反爬特征词扫描（2026-11 纵横搜索「出错」）

现场（2026-11-03 07:42，搜「真牛圣女」）：搜索页小说类五源并发，纵横状态条
`❌ 纵横中文网`，tooltip 只有 `反爬特征响应（命中：被封）
https://search.zongheng.com/search/book?keyword=…`；而 `logs/framework-*.jsonl`
**一条相关记录都没有**（API 搜索的异常当时只冒泡、不打日志）。

复现：同一关键词直接打接口，HTTP 200 + 完整 JSON（`code:0`，26 页 513 条），
命中词来自结果里某本书的简介——「年仅二十便被**封**为史上最年轻的圣女」。
`ANTI_SCRAPE_KEYWORDS` 的「被封」命中了小说正文内容。

根因：`_is_anti_scrape_text` 是**为 HTML 挑战页**写的（先剥注释/标签、取前
6000 字符、小写后匹配特征词）。JSON 没有标签可剥，简介、书名、章节名里的
「被封 / 封禁 / 验证码 / waf」原样留给词表 → 合法数据被判反爬。详情页没踩到，
是因为简介在 `<meta … content="…">` 的**属性**里，会随标签一起被剥掉；JSON 没有
这层「属性保护」。

修法（`framework/http.py`）：

1. 新增 `_is_json_document(text)`：首字符 `{`/`[` 且 `json.loads` 成功才算数据体
   （HTML 里夹一段 JSON 的混排页不算）。
2. GET/POST 四处特征词判定改成 `not _is_json_document(text) and
   _is_anti_scrape_text(text)`。HTTP 状态码判定（403/429/5xx）不变。
3. 拦截能力不放松：真挑战页是 HTML，`json.loads` 必失败，仍按特征词拦。
   站点改回 HTML 挑战（哪怕挂在 JSON 接口上）也照样命中。
4. `framework/search.py::search_one` 的 API 分支把异常记一条
   `[源id] API 搜索失败：<异常类型>: <原因>` 再抛——GUI 每个源一个任务，
   没有这条日志就只能看到状态条上的红叉。

限界：JSON 里若返回 `{"code":403,"msg":"访问过于频繁"}` 这类**结构化限流**，
不再触发 `AntiScrapeError`（会按空结果处理）。要区分得由源配置声明 `code`
语义，别把 HTML 词表硬套回 JSON。

验收：`tests/test_http_json_not_antiscrape.py`（9 条：JSON 带「被封」不报反爬、
GET/POST 三路径、`get_json` 拿得到 payload、HTML 封禁页仍抛 `AntiScrapeError`、
混排页不算 JSON、纵横真实配置解析字段/标题清洗/封面前缀/`keyword` 参数名、
API 失败必留日志）。

### 7.6 detail

详情配置描述标题、作者、封面、简介、标签、状态以及渲染方式。常见结构：

```json
"detail": {
  "render": "http",
  "fields": {
    "title": {"fallback": [{"css": "h1"}, {"css": ".title"}]},
    "author": {"css": ".author"},
    "cover": {"css": "img.cover", "attr": "src"},
    "summary": {"css": ".intro"},
    "tags": {"css": ".tags a"}
  }
}
```

详情页可以不包含章节；章节必须在 `endpoints.content.chapter` 或 API 章节配置中声明。

### 7.7 content.chapter

小说章节配置一般包含：

- `list.root_selector`：章节根节点。
- `list.fields.title/url`：章节标题和 URL。
- `chapters_url`：独立目录地址或 URL 模板。
- `body.selector`：正文节点，支持 `fallback`。
- `body.render` 和 `body.render_config`：需要浏览器时使用。
- `body.api`：正文 API、响应路径和解密目标。
- `pagination`：目录或正文分页上限。

正文提取应优先拿段落节点，保留 HTML 结构和图片；纯文本只作为兼容回退。BookStack 的特有要求是保留 Markdown 渲染结果，不得把 HTML 强制降为一整段纯文本。

### 7.8 comic 与 video

漫画需要声明章节列表、图片选择器、页码/下一页规则、懒加载属性和图片解密。视频需要声明选集、播放页/API、流地址字段、质量、Referer/Cookie、HLS/DASH 或外部播放器策略。对于可直接播放的 `.m3u8`、`.mpd`、`.mp4` 等 URL，不要再次当作 HTML 页面抓取。

`api_endpoints` 的请求方式跟随站点真实接口，不要假定都是 GET：

- `method`／`body`：`search`、`detail`、`episode` 均支持 `POST` 加 JSON `body`，body 里的 `{id}`、`{keyword}`、`{page}`、`{bvid}`、`{cid}`、`{quality}` 占位符由框架替换；接口只接受 POST 时（如 `69tv` 的 `/api/vod/play`，GET 返回 405），必须声明 `method: "POST"`。
- `id_regex`：详情页 URL 末段带 slug 时（`/title/ik130109-恐怖收集者`）先抽出接口认的纯 id 再填占位符；未填上的占位参数整条丢弃，不把字面量 `{source}` 发出去。
- 代理出口按主机自愈，不要用 `transports.prefer_proxy` 全局钉死出口：某个站直连 0.3~0.9s、代理探测却要 8s 超时时，偏好会把详情从 1s 拖到 13s。偏好只是「实测直连不通」的结论缓存，代理再次失败时当场撤销，不升级成「代理整体不可用」。
- `field_extractors`：值为 JSONPath 字符串；对象列表字段（标签、演员）用 `{"path": "labels", "key": "labelName"}` 取出名字列表，避免把对象序列化进标题或标签。
- 接口返回不含章节的单集视频源：声明 `content.episode.single_chapter: true`，详情接口无章节时框架会补唯一分集（详情 URL），播放页不再空白。

### 7.9 render / Playwright

只有静态请求无法得到真实 DOM、需要执行 JavaScript、WAF 挑战或动态接口时才使用 Playwright。常用 `render_config`：`wait_for`、`wait_until`、`timeout_ms`、`extra_delay_ms`、`filter_keyword`、浏览器上下文/请求头配置。

加速原则：

1. 首选 HTTP/API；Playwright 只放在必要 endpoint。
2. 使用复用的渲染线程/浏览器上下文，不为每章启动浏览器。
3. `wait_for` 应指向内容根节点，不用无意义的大延时。
4. 关闭已经不需要的 `extra_delay_ms`，但保留真实挑战完成所需等待。
5. 为慢源设置合理的超时、最大页数和并发上限。

### 7.10 decryption

解密配置描述目标、策略、输入字段和参数。可作用于章节、图片、封面或播放地址。框架负责调用和错误隔离，不能假定所有站点使用同一种算法；必须通过真实样本确认密钥、IV、编码、填充、压缩和响应路径。

解密失败必须返回可诊断错误或空结果，不能把密文展示成正文。严禁将第三方付费访问控制或 DRM 绕过逻辑写成默认功能。

### 7.10.1 取流速度的三条硬约束（2026-10 实测）

取流链上真正能省的只有「本该不做的事」，其余都是站点 TTFB，不能靠调参改善：

1. **纯 URL 解码不要走 Playwright**。decryption.targets.video_url.strategy=js_custom
   的脚本若是单次 decodeURIComponent(d) 形状，ramework/decrypter.py 直接按
   Python unquote 计算（Decrypter.unquote_only_script 只认这一种形状，其它一律
   仍走真实 JS）。实测 omofuna 单次 JS 解密往返 **1.26s**，快路径 **0.012s**。
2. **播放页 HTML 一次取流只抓一遍**。_get_play_page_html 带 120s TTL 缓存并按
   URL 合并并发请求；_fetch_play_url_http_once 与转渲染兜底都必须走它，否则
   playwright_fallback 源会为同一页抓两遍（每集多 1~3s）。
3. **渲染结果要缓存**。playwright_fallback 走到渲染时单次要 8~15s（浏览器启动 +
   iframe 解析站），同一集重播/切回/预取撞点集不该重来。_rendered_play_cache_*
   按源级 content.render_config.cache_ttl_s（缺省 600s）缓存成功结果，带时效
   签名的地址按 expire/expires/auth_key 推算剩余时长，剩余 < 30s 不缓存。

另外两条：**播放地址探测可按源关闭**（content.episode.play_url.probe.mode=skip，
用于直连必 403、只有本地代理能播的 CDN，如 omofuna 的 vip.dytt-hot.com —— 探测
纯属白等 3s 并把可用线路写进死线缓存）；**换线路候选并行取**（
_switch_candidate_episode_urls_parallel，默认线路失败时才触发，串行 3 条 = 3~6s，
正是用户最急着出画面的时刻）。

验收：	ests/test_stream_speed.py（快路径判定与等价性、播放页缓存/并发合并、
候选并行、渲染缓存与签名寿命）。

### 7.10.2 第四条硬约束：默认线路必须是直链线（2026-11 omofuna 改版后）

改版后 omofuna 只剩两条线路，而「默认线路」成了取流超时的最大来源：

| sid | 名称 | `player_aaaa.from` | 取流代价 |
| --- | --- | --- | --- |
| 1 | 精品 | `mp4`（不透明 32 位 id） | 要走 `art.v2player.top` 的 `playData(密文,key)` 二次解密，实测长期失败，一次尝试吃满预算 |
| 2 | 暴风 | `bfzym3u8`（percent 编码的 CDN m3u8） | 一次 HTTP 直取，1~2s 出画面 |

站点把「精品」放在页签第一位，早先的默认线路就是它（`default_sid` 还写着站点
根本不存在的 `3`）。于是每次开播的路径是：抓播放页 → 解析器线失败（可能持续
10~16s）→ 换线 → 再抓播放页（站点抽风时 8s 读超时 × 重试）→ 才出画面。日志
现场：`第三方播放器解析失败 … art.v2player.top:8989/player/`，紧跟着取流超时。

由此固化成配置与代码两条规则：

1. `source_switch.default_sid` 指向直链线（`"2"`），`max_switch_attempts` 等于
   **真实线路数**（`2`；留 4 次只会去打不存在的 sid，白烧两轮）；
   `stream_resolver` 预算同步收紧到 `candidate_timeout=15 / total_budget=28`。
2. 代码侧两道预算闸门，保证「慢线不会把换线机会一起烧掉」：
   - `_resolve_external_stream`：剩余预算 < `_EXTERNAL_MIN_BUDGET_S`(3s) **不启动**
     解析器，直接换线；否则按剩余预算收窄它的 `budget_s`（解析器的 `budget_s`
     是上限，不是每次都该跑满）；
   - `_fetch_play_url_auto_switch`：换线前用 `_stream_remaining()`（deadline 与
     `context.remaining()` 取小）把关，不足 `_SWITCH_MIN_BUDGET_S`(1.5s) 就收尾 ——
     换线是一次实打实的详情页往返，不该在"注定跑不完"时发起。

改版同时改了线路 tab 的结构：从 `ul.channel-tab a[href=#playlistN]` 变成
`a.play-channel-item[data-sid][data-count][href=/anime/<id>/play/<sid>/<n>.html]`，
且**详情页一个都没有、只在播放页渲染**（靠 `probe_from_chapter` 补一次首集页）。
`_parse_source_list` 的 sid 取值顺序因此是：`list_attr(data-sid)` → `sid_regex`
→ **`href` 里的 `/play/(\d+)/`**。最后这条兜底别删：改版时只要 href 还在就能认出
线路；拿不到 sid 的后果不是"少一条线路"，而是整份线路表为空 → 换源菜单消失、
失败后永远无法换线。

验收：`tests/test_omofuna_source.py`、`tests/test_omofuna_channels.py`、
`tests/test_omofuna_end_to_end_local.py`（默认线路、预算上限、解析器预算收窄、
新结构解析、只有 href 时的 sid 兜底、换线预算闸门）。

### 7.10.3 第三方解析站不可用时的三层兜底（2026-11-02 omofuna 精品线现场）

精品线（`from=mp4`）的真实地址要靠第三方解析站二次解密，而**解析站和站点主域
是同一台机器**：`art.v2player.top → 23.225.223.180`，与 `www.omofuna.com` 同 IP。
2026-11-02 早上这台机器对直连是 TCP 超时、经系统代理是 502/`RemoteDisconnected`，
两个出口同时不可用 → 精品线一整天取不到流，而暴风线（分片打散在别的 CDN 域名）
正常。旧实现因此有三个问题，现在按三层兜底修：

1. **出口不是固定的**。`prefer_direct` 原来会把主机钉死成直连，等于放弃了另一半
   成功率；而这两个出口的好坏会在一小时之内翻转。现在 `_fetch_page` 按
   「上次成功的出口」优先，另一个出口再试一次（`system_proxy_url()` 取探测到的
   系统代理，作为**显式** proxy 传入——显式 proxy 才优先于「该主机已知必须直连」
   的记忆）。成功一次后按主机记下出口偏好（`_HOST_ROUTE`）。
2. **解析结果落盘**（`LOCALAPPDATA/claw/external-player/resolved.json`）。
   `cache_ttl_s` 内直接命中（连解析站都不碰）；超出新鲜期但仍在 `stale_ttl_s`
   （omofuna 配 6h）内，且解析站失败时，用它兜底续播——**先**用 `Range: 0-2047`
   试取字节确认 CDN 直链没过期（两个出口都试，CDN 在境外的概率不低）。
3. **失败要快、要能看出原因**。传输层失败（连不上/502/证书）→ 该主机冷却
   `_HOST_DOWN_TTL`=180s，期间不再请求播放页，直接走兜底或换线；页面能取到但
   找不到 `playData` 属于结构变化，**不进冷却**（否则结构一变就再也看不到真实
   报错）。报错文本改成「页面获取失败：`<异常类型>: <原因>`」——旧版是
   `请求失败 GET <很长的 URL>：<原因>`，日志/界面按 90~120 字截断时**原因永远
   被截掉**，线上只能看到半条 URL。

另外 `_fetch_play_url_auto_switch` 抛出的顶层错误现在带最后一条真实原因
（`全部线路取流失败（omofuna）：第三方播放器页面获取失败：RequestError: …`），
界面不再只显示一句什么都不说明的"全部线路取流失败"。

验收：`tests/test_external_player_fallback.py`（落盘复用 / 兜底续播 + 直链失效
被丢弃 / 超窗口不用旧链 / 传输失败冷却但结构变化不冷却 / 直连失败换代理出口 /
报错保留原因），既有 `tests/test_external_player.py`、
`tests/test_external_player_robustness.py` 覆盖缓存与重试语义。

### 7.11 constraints 与 diagnostics

`constraints` 常用：`max_concurrency`、`search.max_pages/max_results`、`detail.max_pages`、`chapter.max_pages/max_items`。`diagnostics.selfcheck.strategy` 可为 `off`、`soft`、`strict`；慢或会触发额外请求的源可关闭自检，但应在诊断说明中记录原因。

### 7.12 源级专属功能与保护规则

本节是源维护的保护清单。专属逻辑是为真实站点结构、访问限制或媒体行为建立的最小适配，不得为了统一字段、删减配置或“优化”而删除。修改前必须确认调用链、缓存键和对应测试仍然覆盖该功能。

#### 7.12.1 广告过滤是播放与下载共用的功能

广告过滤入口统一在 `framework/adblock.py`，播放代理、下载器和投屏参数都要传递同一份源级 `ad_block` 配置。当前能力包括：

- HTML 节点过滤：内置广告 CSS、源级 `extra_css`、广告文本行和广告图片 URL。
- URL 过滤：源级 `block_url_regex`、`block_domains` 与通用路径/query 特征；只匹配明确广告边界，不能用宽泛的 `ad` 子串误删正常内容。
- HLS 清单过滤：解析 `EXTINF`、媒体 URL、`DISCONTINUITY`、`KEY`、`MAP` 和必要的分段元数据后再重写清单；必须保持 `EXTINF` 与媒体 URL 一一对应。
- 删除中插广告后保留必要的一个 `#EXT-X-DISCONTINUITY` 边界，使播放器重置时间戳；片头/片尾不能留下孤立边界。
- 过滤前后必须比较原始/重写清单字节数、分片数、总时长、广告 URL 命中数、密钥请求和首个正片分片，不能只看“返回 200”。
- 过滤器必须是保守删除：不能把“短分片”作为唯一依据，也不能删除不确定的音视频段、初始化段或后续仍需使用的密钥声明。

当前源专属规则：

| 源 | 专属规则 | 不可删除的原因 |
|---|---|---|
| `18j` | `ad_block.hls_rules.drop_leading_block`；片头仅在短时长、首个分界、媒体主机切换、未加密到 AES-128 切换等独立信号同时满足时删除，并受 `max_duration`/`max_ratio` 限制 | 仅按第一段短时长会误删正片；规则变化还可能使音视频时间戳异常 |
| `ikanpp` | `ad_block.block_url_regex` 过滤暴风源 `/video/adjump/` 中插广告；`hls_rules.drop_short_blocks` 兜底识别「被 DISCONTINUITY 夹住、时长极短、占比极小」的广告块（广告与正片**同主机同目录**、URL 无广告词，只有「短」这一个结构特征） | 暴风广告不是稳定的固定时长，且广告 CDN 会轮换域名，单靠 URL 特征会漏；反过来，要求「主机切换」也识别不到真实广告（2026-10 实测：json1080 源广告块与正片同主机，旧 `drop_inserted_blocks` 一条都没命中，广告原样播放）。短块规则必须同时保留时长上限、占比上限和「清单内至少两个正片块」三重约束，放宽任一条都可能误删正片 |
| `trtag`（提线影院，暴风线 `bfzym3u8`） | `ad_block.block_url_regex` 命中 `/video/adjump/`（与 `ikanpp` 共用同一套暴风 CDN 广告目录）；`hls_rules.drop_short_blocks` 兜底识别被 `DISCONTINUITY` 夹住的 25.76 s 短块 | 2026-11 实测《苍兰诀》第18集：687 片 / 2,729.56 s / 5 个边界，两个 9 片 25.76 s 中插广告块（片序号 75..83、609..617），分片 `/video/adjump/time/<id>.ts` 与正片**同主机同目录**。该源原先没声明 `ad_block`，默认引擎一条都命中不了：内置路径特征不含 `adjump`，短块又不满足 `<3 s`（R3）或 `<10 s`（R6）的阈值，广告原样播放——这就是用户反馈的「苍澜诀第 18 集二十来秒广告」 |
| `xuandm`、`avgood`、`bilibili`、`kanav`、`omofuna` | 继续使用各自已声明的通用 URL/HTML/HLS 规则 | 删除 `ad_block` 会让播放、下载和投屏行为不一致 |
| `xvideos` | 列表/搜索的 `root_selector.exclude` 同时剔除频道卡（`.thumb-block-profile`）与付费卡（卡内含 `.is-premium`）；标签页分页按站点 0 索引，统一用 `?page=N`；搜索分页是**另一套参数**：`paginator.param: "p"` + `paginator.start: 0`（第 1 页 `p=0`）；取流走 `play_url.http_chain`：详情页取 `html5player.setEncodedIdVideo('…')` → `GET /html5player/getvideo/{encoded}`（Referer=详情页）→ JSON 字段 `hls`，旧版内联 `html5player.setVideoHLS('…')` 只作 regex 兜底 | 频道卡点进去是用户主页、付费卡（`?premium=1` tab）打开也只有预告，用户明确要求「只保留免费」；标签页分页若用路径式 `/{N}` 会与站点 0 索引错位、第二页起全部重复第一页（实测 `/tags/xvedio/1` 与基底同页）；搜索页实测 `?page=N` 被站点忽略（page=1/2 与首页 27 条完全相同），只认 `p` 且 0 索引，配错就是「搜索结果只出第一页」；2026-11 起详情页不再内联 `setVideoHLS`/`setVideoUrlHigh`，也没有明文 m3u8（播放器 JS 改用 getvideo 接口取地址），只靠旧 regex 会「匹配不到播放地址」；相关视频的缩略图预览是 `preview.mp4`，宽匹配 `.m3u8`/`.mp4` 会把预览当正片 |

#### 7.12.2 HLS 代理、密钥和预取

- HLS 播放优先通过 `framework/media_proxy.py` 处理清单、相对 URL、分片、AES key、初始化段、Referer/Cookie 和源级请求头；不要把 `.m3u8` 当普通 HTML 再抓取。
- 对真实返回的 HLS 地址，播放器在源未声明 `media.hls.prefetch` 时使用保守默认预取（前瞻 4 片、2 路并发）；源显式 `enabled: false` 时必须尊重关闭设置，非 HLS 不开启预取。
- 代理清单缓存键必须包含目标 URL、可信源 ID、广告规则和强制代理策略；修改广告规则或源级播放策略后必须提升相关 cache version 或使旧缓存失效。
- `18j` 的特殊播放上下文只能由受信任的源 ID 传递，不能接受页面或用户输入直接伪造源身份。
- `ikanpp.media.hls.workers=4` 只用于下载器的分段下载，并不等于播放器并发数。当前播放器专属策略是 `network_caching_ms=30000`、`prefetch.enabled=false`、`depth=1`、`prefetch.workers=1`。必须保留显式关闭，避免被未声明预取时的通用默认值（4 片/2 路）重新打开。
- `ikanpp` 的播放器关闭后台 HLS 预取，是为了避免首片播放期间同时请求后续分片；实测源站 CDN 会让这些请求争抢带宽，表现为首片和后续切片抖动。播放器自身的 30 秒网络缓冲和媒体代理按需缓存负责抗短时波动。该取舍只适用于 `ikanpp`，不能推广为全局关闭预取。
- 每次换源、换集或结束播放都必须重置预取策略，避免 ikanpp 的并发和深度泄漏到其它源；预取失败应自动降级为正常按需请求。
- 预取或关闭预取都不能掩盖源站 504、TLS EOF、失效 token 或编码本身的音频偏移；验收时要分别记录网络失败、代理缓存命中和媒体同步问题。

#### 7.12.3 章节、正文与阅读排版

- 小说和漫画都必须先展开完整目录，再在共享章节排序逻辑中统一去重、识别卷号/章号、处理中文数字和保留无法识别章节的稳定顺序。源页面不能自行写一套临时排序。
- 小说正文必须按段落或语义节点渲染，不能把整章拼成一个 `QLabel`；首行缩进由阅读器统一按两个汉字宽度处理。
- 章节标题和作品标题是独立 UI 区域；顶部按钮增大后仍要保证标题、章节信息和“背景”等文字不被遮挡或裁切。
- 字号、字体样式、背景、正文颜色和按钮颜色必须经过主题联动；首页、源配置和设置页文字保持黑色，阅读正文根据背景计算对比色。
- 正文图片必须是独立内容节点并保留尺寸约束、加载失败状态和前后段落边界，不能使用绝对定位覆盖文字。
- 正文空白清理只能处理异常连续空白；不能把中文字符间距当普通空格删除，也不能把解密失败产生的空白占位当成完整正文。

#### 7.12.4 缓存、会话和源变更

- 会话 Cookie、临时请求对象和会话级页面结果只在当前进程有效；启动时不得恢复会话缓存，关闭时也不得把会话缓存写入持久化缓存。
- 搜索/发现缓存、书架缓存、正文/章节缓存、封面缓存和媒体代理缓存分开管理；清理一个缓存池不能误删用户书架、阅读进度或其它源的数据。
- 源的选择器、正文解析、解密、广告规则、HLS 处理或 Markdown 结构变化时，提升 `detail_cache_version`、`chapter_cache_version`、`page_cache_version` 或等效策略指纹。
- 封面加载必须允许占位和回填，不能先显示真实封面再被异步空值覆盖；封面失败不能让卡片或详情消失。

#### 7.12.5 当前源的专属链路

| 源或源族 | 专属链路 | 维护要求 |
|---|---|---|
| `bookstack` | 发现/搜索使用 Playwright，详情优先 HTTP；目录和正文使用 BookStack 页面结构；正文按 Markdown 富文本 HTML 读取 | 保留标题、标题层级、列表、代码、链接和图片；不能降成一整段纯文本；发现、详情、目录、正文分别验证 |
| `qk17` | 搜索/详情/正文需 Playwright；章节正文支持 `aes_cbc_base64`；免费章节与 VIP 截断要区分 | 解密配置、WAF 等待条件和 VIP 限制不能混为网络错误；验证卡片、详情、完整目录、首章和可访问正文 |
| `timotxt` | 发现、搜索、详情和正文使用 Playwright；标题、作者、章节、简介和正文统一 `zh_convert` 为简体 | 简体转换必须在字段进入统一模型前完成；不能只转换界面标题而遗漏正文 |
| `qimao` | 搜索使用 Playwright；章节目录使用 `qimaoapi/api/book/chapter-list`，详情页目录不完整时以接口为准 | 先取完整 API 目录，再访问正文页；不能只依赖详情页 SSR 的最新章节 |
| `18mh`、`18mh-novel`、`18mh-video`、`51cg1`、`91pornacomic`、`favcomic` | 图片使用源声明的 `aes_cbc` 字节解密 | 解密失败要报诊断错误，不能把密文或损坏图片当成正文；图片 URL、key、IV 和 padding 不能擅自复用到其它源 |
| `biqumo` | 正文 `base64_decode` | 解码后仍需按段落清洗和正文完整性检查 |
| `czbooks` | 标题、作者、章节、正文和简介使用 `zh_convert` | 转换只作用于声明目标字段，不能改变 URL、ID 或媒体地址 |
| `fdzys`、`kanav` | `maccms_url` 播放地址处理；`fdzys` 搜索必要时 Playwright | 先确认真实流字段，再交给媒体解析器；不要把播放 JSON 当正文 HTML |
| `omofuna` | `js_custom` 播放地址处理，并结合媒体/广告规则 | 自定义脚本只在源配置目标内执行，失败要返回可诊断错误 |
| `jptt` | 发现页支持 `playwright_fallback`；标签页是单独分类入口；详情为单视频 | HTTP 能直接得到真实 DOM 时优先使用 HTTP，浏览器只作为必要回退；标签、详情和播放地址分开验收 |
| `51cg1` | 详情页每个 `.dplayer[data-config]` 是一集；从 `data-video_title` 和 `data-config.video.url` 提取选集，直链 HLS 章节直接透传 | 单视频保持单集，多视频按页面顺序展示；必须还原 JSON 转义 URL，不能只取第一个 `m3u8` |
| `ikanpp` | 发现使用 `/api/library/browse`，搜索使用 `/api/search-parallel` SSE，详情使用 `/api/detail`；播放地址可能先返回外层 HLS 清单，再进入变体清单，分片通过媒体代理按需请求；`api_endpoints.detail.chapters.related_episodes` 在同一作品下并入其它采集源的线路，播放界面据此换线路 | 详情请求必须保留 `title` 和 `source`；播放必须保留源请求头、Referer、AES-128 key 和分片顺序；播放器使用源级低争抢策略，不能擅自恢复后台预取；换线路后 `Detail.url` 必须更新为目标线路 URL，否则下次取流会切回原线路 |
| `h-comic` | 动态图片需要 Playwright 滚动加载并提取图片节点 | 必须等待图片实际出现并去重，不能只取初始 HTML |
| `quanben` | 发现/搜索封面通过详情回填 | 先保留卡片，再异步补封面；回填失败不删除卡片 |
| `xuandm` | 选集顺序声明为 `desc`，播放地址从嵌入 JSON 提取 | 这是源的真实顺序，不得由通用默认排序覆盖；播放 URL 要做候选和有效性验证 |
| `zongheng` | 支持 `auth` 声明的登录/授权字段 | 凭据只来自运行时安全存储，不能写进源 JSON、测试夹具或日志 |
| `69tv` | 发现走 SSR HTML（原站 13 个频道 `/movies/{id}?page=N`，每页 30 条）；详情、搜索、取流走独立 API 域名 `POST` JSON：`/api/vod/content`、`/api/vod/movies`（搜索分页参数是 `pageNum`）、`/api/vod/play`（GET 405，返回带 token/expires 的 m3u8） | API 域名来自首页运行时配置 `window.__NUXT__.config.public.apiBase`，轮换后需更新；详情接口不给章节，靠 `single_chapter` 补唯一分集；搜索快速连发约 6 次以上会触发站点反爬，`max_pages` 与 `pageSize` 保持保守 |
| `lxyy` | 搜索 `GET /api/proxy.php?action=search&wd=&page=`、详情 `GET /api/proxy.php?action=detail&source=&id=`；搜索结果自带采集源坐标（`source`+`id`），`search.group` 把同一作品聚成一条采集源一条线路，`group.providers` 提供中文线与排序；`exclude_providers` 排掉官采源 `qilin`/`fy`（分集是优酷/爱奇艺/B 站官方页，要站点自己的 `svip.qlplayer.cyou/api/resolve.php?token=` 或 `/json-player.php?route=&grant=` 解析，直连实测 404）；详情 `details.0.episodes[]` 是「线路→分集」，`chapters.pick_line=direct_media` 只留分集是 m3u8 直链的那条线（jl 的 `jlplayer` 是分享页）；`transports.direct: true` 让 JSON 搜索/详情也强制直连 | 官采源不可回填进搜索结果：拿到的只是内容页 URL，播放必失败；`pick_line` 生效时**不得建线路菜单**（线路坐标在同一 URL 里切不过去，切下去只会用错 `source` 重新请求详情）；首页 `/api/proxy.php?action=home` 固定回 `home_source=qilin`，探测 `source`/`src`/`from`/`channel` 均无效，故本源只做搜索入口，不加发现页；jl 的 CDN 镜像 `jimaoys94.com` 对非浏览器出口统一 403（m3u8 直接 403，同路径 `jimaoys95/88/83` 都是 200），用 `media.url_rewrite` 改主机，不要写死进代码 |
| `lxxlxx` | 标签/榜单列表是路径式分页且第 1 页无页码（`/tags/X/`），第 N 页是 `/tags/X/N/`；分类 URL 统一写 `/tags/X//`，由 `page_placeholder: "(?<=/)(?=/$)"` 在末尾两个斜杠之间插入页码；取流用 `content.episode.play_url.http_chain` 三段：详情页 `/vjs/{vid}.js` → `m4-player-*.gotojs.xyz` 播放器脚本 → `<source src=...mp4>` | 分类 URL 不能写成 `/tags/X/{page}/`（第 1 页 `/1/` 是 404），也不能构造缺尾斜杠的 `/tags/X/N`（站点 301 到 http 后被阻断）。三段链必须逐跳按响应内容提取（`-en/-cn/-zhs` 语言域的播放器与 CDN 主机各不相同，不得写死主机名），链尾 mp4 声明 `fetch: false` 不再回源；媒体域按地域限制访问，取流成功但播放 404 属 CDN 侧限制，需换出口网络验证 |

以上表格不是新增功能清单，而是防误删清单。新增源若出现 `decryption`、`render`/`render_config`、`api_endpoints`、`media`、`ad_block`、`auth`、`cover_backfill` 或自定义章节顺序，必须在本节登记其目的、调用入口和验收方式。

#### 7.12.6 播放界面换线路的两条实现路线

播放界面的「换源/线路」菜单只认 `Detail.source_list` 这一份契约，每项为
`{source_id, sid, name, detail_url}`，换线路统一走
`Content.switch_source_url()`。产出这份列表有两条路线，二者互斥且有明确优先级：

| 路线 | 配置入口 | 适用 | 产出 |
|---|---|---|---|
| 页面换源（`source_switch`） | `endpoints.content.episode.source_switch` | MacCMS 类站：详情页有线路 tab（`?sid=`），取流需从 `player_aaaa` 解出 | 详情 HTML 选择器解析出的线路 |
| 搜索分组（`search.group`） | `api_endpoints.search.group.enabled` | 聚合站搜索把同一作品归并成多采集源 | 搜索时记住的分组结果 |
| 详情接口换线路（`related_episodes`） | `api_endpoints.detail.chapters.related_episodes` | 聚合站详情接口**只返回一条线路**，换线路要拿线路自己的 id 再请求一次（ikanpp） | 详情时并请求其它线路得到的分集 |
| 详情接口兜底（`fallback`） | `api_endpoints.detail.fallback`（`strategy=search_play_url`） | 详情接口只认自家库 id 的聚合站（ikanpp 第三方 `vod_id` 一律 400/404），详情页必「加载失败」 | 回查搜索结果，用记录里的 `vod_play_from`/`vod_play_url` 摊出线路与分集；详情请求 4xx/超时或线路为空时自动生效 |
| 详情主键提取（`id_regex`） | `api_endpoints.detail.id_regex` | 详情页 URL 末段带 slug（`/title/ik130109-恐怖收集者`），接口只认纯 id | 接口参数只带 `ik130109`；未填充的 `{source}` 之类占位参数整条丢弃，不再发出字面量 |
| 同源 http→https 升级 | `transports.allow_http`（默认 false） | 站点自己吐 `http://` 绝对地址（agedm 目录/搜索 href），80 端口在多数网络连不通 | 详情/分集请求全部落在 443：`Content._abs_url`/`Discovery._abs_url` 只对同 host 升级，跨 host（CDN）不动 |
| 线路坐标回退（`default_sid` 不存在） | `source_switch.ep_list_selector` + `default_sid` | 详情页只渲染站点真实存在的线路（omofuna 有的番只剩「精品」sid=1，配置却是 2） | 先用页面里真实出现的第一条线路重试，再退化为收全部 `/play/` 链接：分集数不为 0，`provider` 是页面上的线路 |

`related_episodes` 的要点：

1. **跨线路绝不复用当前线路的 id。** 必须先在目标线路自己的搜索接口里按标题
   查到它自己的 `vod_id`，再用该 id 请求详情。实测事故现场（2026-10）：
   json1080 的 `vod_id=31027` + `source=jisu` 会**静默返回另一部作品**
   （「窒息的阳光」，单集），且响应 `success=True`、`healed=None`——调用方无法
   从响应判断对错，用户点「极速资源」实际播的是随机一部片子。反向验证：jisu
   自己的 `vod_id=7196` 才返回正确的 35 集。
2. **二次校验标题**：详情返回的 `vod_name` 必须与当前作品标题相关（容忍
   「国语版/粤语版」这类后缀差异），否则丢弃该线路。宁可少一条线路，也不能把
   无关作品混进分集列表。
3. **线路菜单延迟加载，不许阻塞详情。** 换线路要按标题检索其它采集源、再各请求
   一次详情，实测 ikanpp 约 3.5s；放进详情主链路会把打开视频从 ~1.5s 拖到
   ~4.5s，而多数用户根本不换线路。因此：
   - `fetch_detail` 只置 `Detail.line_menu_available`（**零网络请求**）；
   - 播放界面加载完立即渲染选集，再由 `_FetchLineMenuTask` 在后台调
     `Content.fetch_line_menu(source, url, title)` 补菜单，回到主线程
     `VideoView._on_line_menu_ready` 应用；
   - 迟到结果按 `_line_menu_generation` 丢弃；期间若已由搜索分组给出线路列表，
     不覆盖。
4. 只在**当前详情没有分组线路**（`_has_group_menu` 为假）时才值得拉；已有分组
   时直接跳过。搜索分组优先级高于详情接口线路。
5. 线路 id 解析结果按 `(源, 线路, 标题)` 缓存（上限 256 条），反复打开详情/换线路
   不重复搜索。
6. 并入的分集带 `provider` + `line_name` + `line_switch_url` +
   `line_switch_source`；`Content.line_menu_from_chapters()` 把它整理成
   `source_list`（含该线路的集，供播放界面按标题/位置定位当前集）。
7. 任一线路请求失败只跳过该线路；整块异常回退空列表，不能拖垮详情加载或播放。

换线路后 `switch_source_url()` 必须把 `Detail.url` 更新成目标 URL：线路坐标
（ikanpp 是 query 的 `id`/`source`）就是从详情 URL 推导的，沿用旧 URL 会让下一次
取流又切回原线路。

#### 7.12.7 GET 型聚合接口的四个补充键（2026-11 洛雪TV）

ikanpp 的 `/api/search-parallel` 是 POST，采集源名单躺在 body 里，框架从
`body.sources` 就能拿到线路 id、中文名和优先级。换成 GET 型聚合站（洛雪TV
`/api/proxy.php?action=search`）后这三样都没有了，于是补了下面几个键——**都不
改变老源行为，缺省即旧行为**：

| 键 | 位置 | 作用 |
|---|---|---|
| `exclude_providers` | `api_endpoints.search` | 按采集源 id 过滤搜索结果。用于「该采集源的分集不是媒体地址」的情况：官采源给的是优酷/爱奇艺/B 站内容页，播放端必然取不到流；留在结果里只会让默认线路点到必败的一条 |
| `group.providers` | `api_endpoints.search.group` | `[{id, name, priority}]`：GET 体没有 `body.sources` 时提供线路展示名与排序。搜索卡片的 `provider_name`、变体标签、线路菜单名字都读它；缺省线路名会退化成品 id（`mj`/`jl`） |
| `pick_line` | `api_endpoints.detail.chapters` | `"direct_media"`：嵌套线路接口里有「直链媒体线」和「内容页线」（洛雪TV 的 `jlm3u8` / `jlplayer`）时，取第一条**首集就是媒体直链**的线路；生效时不建线路菜单（`line_switch_url` 靠改同一个 URL 的线路坐标，这类站切不过去），分集上的 `line_switch_*` 字段也不会残留 |
| `url_rewrite` | `media` | `[{pattern, replacement}]` 正则改写播放地址，在直链 passthrough 返回前按序生效。用于同一库的 CDN 镜像主机里有的主机对非浏览器出口一律 403（`jimaoys94.com` → 同路径 `jimaoys95.com` 就 200）。映射写在源 JSON 里，主机轮换只改配置；坏正则只跳过该条并打 warning |

验收口径：`tests/test_lxyy_source.py`（离线假 HttpClient）覆盖请求参数、官采源
排除、同名作品聚线路、中文线路名、`pick_line` 选中直链线且 `source_list` 为空、
官采分集原样保留、m3u8 直链 passthrough 不发请求、`url_rewrite` 只改被墙主机。

`transports.direct` 现在也作用于 `api_endpoints` 的 JSON 请求（`_search_api` 与
`_fetch_detail_api` 的 `get_json` 都带 `direct=`，`HttpClient.get_json` 随之接受
该参数）。此前只有 HTML GET 走直连，国内直连站在加速器/系统代理下每条搜索都要
先吃一次代理探测超时；洛雪TV 配了 `transports.direct: true` 后搜索从 6~14s
降到 1~3s。

同分组内同一采集源回多条时（洛雪TV 的 jl 常见 4 条同名「自营」）线路名会追加
更新信息与序号（`自营·HD中字`、`ikun·正片 2`），否则换线路菜单里是一串同名条目。

#### 7.12.8 检测播放源（线路可用性）2026-11

播放界面的「检测播放源」按钮有两条分支，判断口径不同——别用一条的结论去解释另一条：

| 线路形态 | 检测方式 | 结论来源 |
|---|---|---|
| 无 `detail_url`（`related_episodes` 并入的其它源线路、跨源候选） | `fetch_video_streams` 真解一次流 | 拿到视频/音频地址即「可播放」 |
| 有 `detail_url`（同源采集源线路，`search.group` 产出） | `framework.probe_line_playable()`：取该线路详情 → 取**同序号那一集** → `_probe_playback_chain`（媒体代理 → 清单 → 变体 → 首个分片） | 整条链路都通才「可播放」；`reason` 记录 `detail:…`/`no_episodes`/`http403`/`html`/`no_variant`/`no_segment`/`segment_http403`/`segment_empty`/`timeout` |

要点：

1. **只看详情接口有没有数据不算可用。** 洛雪TV 的 jl 详情有分集，但分集指向的
   CDN 镜像 `jimaoys94.com` 对非浏览器出口统一 403：详情正常、播放必失败。
2. **清单 HTTP 200 也不算可用**（这是「显示可播放却黑屏」的主因）：播放器要的是
   清单里的分片。分片 403/返回 HTML 错误页/清单为空/变体清单 403 时，清单本身
   依然是 200。所以必须走完整条链：`#EXTM3U` → 有 `#EXTINF` → 首个分片
   HTTP<400 且非空非 HTML（`segment_http403`/`segment_empty`/`segment_html`）。
3. **探测与播放同路**：`_probe_playback_chain` 用 `proxy_url_for` 把地址包成本地
   媒体代理 URL（带源 `ad_block` 配置），清单经过 m3u8 重写 + 广告段过滤再验分片
   —— 代理这一层把清单改坏同样是黑屏，直连探测看不到。代理起不来时退回直连
   （`via=direct`），探测不因此失败。`media.url_rewrite` 也在探测前执行。
4. **探测的分集必须与播放口径一致**：换线路后 App 按**序号**映射同一集
   （`current_episode_no()`），所以探 `episode_index`；只测首集时「第 3 集是死的」
   线路会被标成可播放。
5. **同 sid 的多条线路要分开记账**：`_probe_key()` 有 `detail_url` 就用它当主键，
   否则用 sid。旧实现按 sid 记账 + 按 sid 去重，导致洛雪TV 的 4 条 jl 只剩 1 条
   被测、结果互相覆盖。
6. **`detail:` 类失败重试一次**再判死：聚合站连续请求会偶发限流，一次抖动把好
   线路标成「不可用」比不检测更误导（实测 6 条里有 2 条被瞬时抖动误判过）。
7. 检测顺序「当前正在看的线路优先」，`media.source_probe.max_sources` 上限从 5
   放宽到 12（一个作品聚出 10+ 条线路是常态），并发仍为 1，逐条测。
8. 结果标注进下拉框与播放器内菜单：`（可播放）` / `（不可用）` / `（超时）`，
   失败原因进 tooltip（`Qt.ToolTipRole` / `QAction.setToolTip`），按钮文本给出
   `可播放 N/M`。检测不改变当前线路，是否切换由用户决定。
9. **真实播放失败要能改判**：探测只能证明「这一刻清单和分片取得到」，播不出还有
   播放器/解码/代理重写等层。`_on_player_error`（播放器报错）与
   `_on_stream_loaded` 的取流失败分支都调 `_mark_current_line_unplayable()`，把该
   线路从「可播放」翻成「不可用」并把原因写进 tooltip——界面不能继续骗人。
10. **严格模式（可选，默认关）**：`sources_runtime.verify_player_probe=true` 时，
    链路通过后再用 App 的播放器（mpv，`--vo=null --ao=null --frames=8`）拿**同一个
    代理地址**真解 8 帧，rc=0 才标「可播放」（失败记 `player_failed` /
    `player_timeout`）。本机没装播放器返回 `no_player`，不判死。代价每条线路
    5~25s，所以默认关；需要「播放器证明过才算可播放」的用户自己打开。
11. **清单要分块读**：`raw.read(262144)` 会卡在等满 256KB，聚合站 2500+ 段的大
    清单经代理转发时经常读满超时（实测把 2 条好线路判成 `error:ReadTimeout`）。
    `_probe_read_manifest` 按 8KB 分块读、够分类且拿到首个 URI 就停：全 12 条
    线路的检测从 63.7s（含 2 条误判）降到 37.5s（12/12 通过）。
12. 同 sid 但 `detail_url` 不同的线路必须能切换（`_on_source_switch` 只比 sid 时
    「点了没反应」）；下拉框选中态、菜单勾选态都按 `detail_url` 优先匹配。

验收：`tests/test_playback_probe_chain.py`（分片 403/HTML/空清单/变体 403/直链
mp4/代理不可用回退/超时/分块早停/严格模式播放器判定，全离线）+
`tests/test_video_probe_sources.py`（按钮可见性、候选顺序与去重、`episode_index`
口径、按 `detail_url` 标注 + tooltip、播放失败改判、同 sid 换线路）+
`tests/test_lxyy_source.py::test_probe_line_playable_*`（ok/死 CDN/同序号/no_episodes）。

## 8. 选择器、解析和统一化规则

### 8.1 selector

解析器支持 CSS、XPath、`attr`、`regex`、`b64decode` 和 `fallback`。`fallback` 按顺序尝试，返回第一个非空结果。选择器必须尽量指向语义根节点，而不是依赖脆弱的层级序号。

根节点自身属性使用：

```json
{"xpath": ".", "attr": "href"}
```

不要写成会在根节点上下文丢失的绝对 XPath。

### 8.2 URL、封面和懒加载

- 相对 URL 使用源 `base_url` 归一化。
- 图片的 `src` 为空或 `data:` 占位时，依次尝试 `data-src`、`data-original` 等懒加载属性。
- 封面解密、详情回填和网络图片下载应分离；卡片先显示占位，不要先显示真封面再被异步空值覆盖。
- 记录 Referer、Cookie 和 CDN 域名要求；封面加载失败不应让详情卡片消失。

### 8.3 标题、正文和空白

- 标题要去除站点装饰前后缀，但不能删掉作品本身的卷名、章节号或必要括号。
- 小说正文按段落节点提取；只对异常连续空白做归一化，不把中文正文的单字间隔误删成空格或反过来把加密占位当普通空格。
- 首行缩进统一由阅读器渲染策略处理，源配置只提供内容，不提供 Qt 排版代码。
- Markdown/HTML 内容优先保留语义标签、图片、链接、列表和代码块。

### 8.4 章节排序

章节排序必须在详情和阅读器共享的通用逻辑中完成，不能在某个源页面临时排序。基本原则：

1. 先保留源的稳定顺序和卷/分组信息。
2. 从标题或 URL 提取卷号、章号、数字序号。
3. 中文数字、阿拉伯数字和“第 X 章”归一为可比较键。
4. 无法识别编号时保持原顺序，不把未知章节强行排到正文之前。
5. 去重使用规范化 URL/章节 ID。
6. 目录展开后再排序，保证小说和漫画都得到完整目录。

### 8.5 书架排序

书架有三种排序：「最近阅读」「最近添加」「书名」。**只有「最近阅读」允许随阅读
行为改变顺序**，另外两种在阅读后必须保持不动——否则用户点开一本书回来，卡片
就跳走了；同一时刻批量加入的一批卡片尤其明显。

实现约束（`ShelfService._sort_items`）：

- 时间字段分两个，不能混用：`added_at`（加入书架：收藏时间 `favorited_at` /
  本地文件 `mtime`）与 `updated_at`（最近阅读时间）。
- 「最近添加」**只**按 `added_at` 倒序；同一时刻批量加入的条目加入时间相同，
  次级键用规范化标题（+ kind）保证稳定，绝不能拿 `updated_at` 或 `mtime`
  兜底——收藏条目的 `mtime` 恒为 0，一旦用它当主键，「最近添加」会退化成
  「最近阅读」（2026-10 实测：修复前两者排序结果完全相同）。
- 「书名」按规范化标题排序，与阅读无关。
- 「最近阅读」按 `updated_at`（缺失回退 `mtime`）倒序，这是它应有的语义。
- `favorites()` 必须把 `favorited_at` 同时写入 `added_at` 与初始 `updated_at`，
  之后只允许阅读进度覆盖 `updated_at`；`added_at` 一经写入不得再变。

回归测试：`tests/test_shelf_sort_stability.py`。

## 9. 缓存、历史和持久化

### 9.1 缓存层次

| 层 | 内容 | 生命周期 |
|---|---|---|
| 会话缓存 | 本次进程的请求/页面临时对象 | 进程结束即消失，不应在启动时恢复会话 |
| 搜索/发现缓存 | 搜索结果、发现列表和分页快照 | 持久化缓存池，可单独清理 |
| 书架缓存 | 封面、详情、目录、章节正文、漫画页和位置 | 持久化缓存池，服务重启后可恢复 |
| 页面内存缓存 | 当前页面、详情 HTML、封面 Pixmap、预加载章节 | 页面/进程生命周期 |

### 9.2 缓存键与版本

详情、目录、正文和页面缓存键必须包含 `source_id`、规范化 URL 和必要的 cache version。站点改版、选择器调整或解密逻辑变化时，提升源中的 `detail_cache_version`、`chapter_cache_version`、`page_cache_version`，避免旧数据污染新解析。

### 9.3 清理规则

- 设置页的普通“清除缓存”清理封面内存缓存和临时合成图，不误删持久化缓存。
- “清除书架缓存”和“清除搜索&发现缓存”分别操作独立缓存池，并立即落盘。
- 会话 Cookie 和会话缓存不得因为关闭应用而被意外持久化。
- 阅读进度对非书架作品有生命周期清理；加入书架的作品保留续读位置。
- 删除源不应自动删除用户书架记录，除非用户明确清理关联缓存。

### 9.4 专属缓存不变量

- 媒体代理的 HLS 清单缓存必须区分源、目标流、请求头、代理开关和 `ad_block` 规则；不能复用未过滤清单替代已过滤清单。
- 源级预取缓存只服务当前播放会话；换源、换集、重试或关闭阅读器时必须取消/失效旧任务，迟到结果不能写入新会话。
- 章节正文缓存必须区分原始响应、解密后正文和阅读器排版结果；解密或简体转换逻辑变化时不能继续命中旧的错误正文。
- 缓存命中仍要经过内容完整性检查；空正文、验证码页、VIP 截断和广告清单不能被标记为成功缓存。

## 10. 错误处理与健康诊断

### 10.1 错误分类

| 类型 | 处理 |
|---|---|
| 配置错误 | 跳过坏源，源管理显示原因 |
| 网络/超时 | 按源重试和间隔处理，页面显示可重试状态 |
| 反爬/挑战 | 记录 challenge 状态，不把验证码 HTML 当结果 |
| 结构变更 | `StructureChanged`/结构失败，提示更新源 |
| 内容缺失 | `ContentMissingError`，说明选择器/API 未得到内容 |
| 解密失败 | 记录目标和阶段，不展示密文 |
| VIP/登录限制 | 显示已取得的预览范围和访问限制 |
| 播放器失败 | 尝试候选源/外部播放器，保留错误摘要 |

### 10.1.1 阅读/播放进度的记录时机

三类内容**记录时机不同**，不要统一化：

| 类型 | 何时写入进度 | 原因 |
|---|---|---|
| 小说 / 漫画 / epub | **点开即记**（首个章节/首页渲染出来就写） | 打开本身就是有效阅读行为，用户期望书架立刻反映「最近读过」 |
| 视频 | **只有真正拿到播放流并成功启动播放器之后**才记 | 取流失败/播放器启动失败时什么都没播，记进去会让书架把没看过的片子排到「最近阅读」最前，续读还会定位到那一集 |

视频侧的落点是 `VideoView._play()`：`_has_played`（唯一的进度回写闸门）必须在
`open_with_player()` 返回**之后**按 `framework.external_player.player_launch_ok(msg)`
置位，不能在发起播放前就置位。`player_launch_ok` 把返回串的语义固定下来：

- `""`（无可播地址）/ `"浏览器打开失败"` → 失败，不记录进度；
- `"已用外部播放器打开…"` / `"已在浏览器中打开"` → 成功，允许记录。

不要在 `_on_stream_loaded` 里只凭「video 非空」就记进度：拿到地址不等于播出来了。

### 10.1.2 视频续播的「同一集」判定

续播位置按**集身份**对位，不按 URL 全等：多线路源（omofuna 的 `/play/1/11.html?sid=1`
→ 换线路后 `/play/3/11.html?sid=3`）、CDN 换域名、m3u8 每次新签 `auth_key`，都会让
同一集的 URL 变样。只比字符串的后果是「换条线路看过的集被当成没看过，从头重播」——
这正是用户侧最容易被报成「记不住播放位置」的现象。

判定收敛在 `gui/pages/reader/video_view.py` 的 `_same_episode()`，两条归一化：

- **集号签名** `(第几集, 集名后缀)`：`第01集` / `11集` → `(11, "")`；`第1集 上` /
  `第1集 下` → `(1, "上")` / `(1, "下")`，不会互相串。集号冲突时**直接判否**，
  这条先于「单集作品」的放宽生效（连载型单集换集不能继承上一集秒数）。
- **URL 身份**：取不到集号时，比去掉易变查询参数（`auth_key`/`sign`/`expire`/
  `token`/`sid` 等）后的路径。单集视频的签名 m3u8 靠这一级命中。

`load()` 定位续读集位同样走这套判定：URL 全等没命中只是「换过线路」，不是「不是
这一集」，否则续读会静默回落到第 1 集。`_progress_options()` 只在同一集成立时
回填 `seconds/position`，不同集必须返回空 location——宁可从头播，也不要把上一集
的秒数套到错集上。

### 10.2 自检

自检是健康监控，不应在每次正文请求前同步额外请求造成双倍延迟。`off` 表示关闭，`soft` 记录警告但不标红，`strict` 在结构失败时标记源异常。诊断页应展示最近状态、错误摘要和建议动作。

## 11. 性能与稳定性规范

当前项目的性能策略是“先修明显卡顿，再保持简单”：

- 首页和主窗口先显示。
- 非当前标签惰性构建。
- 网络、解析、解密、封面下载、Playwright 和下载任务不在 UI 线程执行。
- 卡片批量提交，固定占位尺寸和网格布局，避免窗口抖动。
- 详情、章节、封面和列表优先复用正确版本的缓存。
- 章节预加载默认受设置和源并发约束，不能无限预取。
- Playwright 浏览器/渲染线程复用，减少启动成本。
- Playwright 发现源首屏只请求第 1 页，后续页在滚动时按需加载；静态分类直接在 UI 线程构建，不额外等待线程池往返。
- 只有出现可复现的卡顿、请求放大或内存增长时才增加更复杂优化。

媒体性能的专属约束：

- HLS 预取必须受 `depth`、`workers`、超时和取消机制限制；播放器对未声明策略的真实 HLS 地址使用保守默认（4 片/2 路），源显式 `enabled: false` 时关闭，非 HLS 不预取。切源时必须取消旧预取窗口并按新源并发配置重建线程池。
- `ikanpp` 的 30 秒播放器缓存与“关闭后台预取”是针对其暴风 CDN 争抢特征的调优，不得改成所有源的全局值；如果出现卡顿，先检查 CDN、首片耗时、分片失败率、代理缓存命中和音视频时间戳，再考虑改变预取策略。
- Playwright 只在 HTTP/API 不能得到真实内容时使用；复用浏览器上下文，缩短 `extra_delay_ms` 前必须确认不会跳过 WAF、图片懒加载或正文渲染。
- “变快”不能通过提前加载所有源、所有详情和所有正文实现；启动优先级仍是首页、BookStack 发现、源管理、书架/缓存、设置，详情和正文按需加载。

#### 取流预算与「首次打开失败」

`StreamResolver` 的单项候选超时（默认 10s）本意是「快速失败换下一条线路」。但
冷启动的**第一个请求**要付 DNS + TLS 握手 + 系统代理探测的一次性成本（实测
沙箱内首次 1.2~1.5s，真实环境叠加代理/加速器后更长），一旦超过单项超时就会被
整条丢弃，表现为**第一次打开播放失败、重试立刻就好**——重试时连接已热身。

约束：**最后一个候选**（已经没下一条线路可换）在单项超时之外再给一段宽限，
`min(单项超时 × LAST_CANDIDATE_GRACE_FACTOR, 剩余总预算)`（默认 10s → 20s）。
非最后候选仍严格按单项超时快速失败；需要跑满总预算的慢站必须显式声明
`stream_resolver` 的 `candidate_timeout`/`total_budget` 或 `stream_budget: pipeline`，
不能靠放宽全局默认值让它变快。改动 `LAST_CANDIDATE_GRACE_FACTOR` 必须同步
`tests/test_stream_resolver.py`（普通候选静默超时的契约）与
`tests/test_stream_first_open.py`。

性能验收应关注首屏可交互时间、首次分类时间、详情时间、首章时间、翻章时间和重复打开缓存命中时间；不能只用 HTTP 200 作为速度或可用性的判断。

## 12. 测试与验收

### 12.1 源开发最小闭环

1. 阅读目标站点真实首页、分类、搜索、详情、目录、首章和末章。
2. 记录真实请求头、Cookie 要求、重定向、动态渲染和 API 响应。
3. 确认内容类型和访问限制。
4. 编写最小源 JSON，先通过配置加载。
5. 分别验证发现、搜索、详情、完整目录、第一章、随机中间章、最后可访问章。
6. 验证标题、作者、封面、简介、章节顺序、正文段落、图片和视频流。
7. 加入离线 HTML/API 夹具和源回归测试。
8. 配置必要的缓存版本、自检策略和并发上限。
9. 运行目标测试，再运行相关框架测试和 `git diff --check`。

### 12.2 不同内容类型的验收标准

小说：目录完整、顺序正确、正文段落不堆叠、中文空白正常、标题显示完整、图片不扰乱文字、缓存重开可读。
漫画：章节顺序正确、图片顺序正确、懒加载属性生效、封面不会被异步空值覆盖、图片失败有提示。
视频：选集完整、流地址可验证、播放源切换可用、播放位置能恢复、无法播放时给出实际原因。
广告过滤：播放和下载都要验证过滤前后清单统计、分片连续性、密钥/初始化段、首个正片分片和总时长；音视频时间戳不能出现由过滤器引入的异常跳变。
BookStack：发现和详情能加载，目录能展开，文章正文按 Markdown 富文本显示，标题/列表/代码/图片不丢失。
17K：卡片、详情、目录、可访问正文和 AES 解密链路分别验证；VIP 截断必须标为站点限制。

### 12.3 推荐命令

```powershell
pytest -q
pytest -q tests/test_bookstack_reader.py tests/test_qk17.py
pytest -q tests/test_public_novel_sources.py tests/test_chapter_sort.py
git diff --check
```

若环境缺少 Qt、Playwright 浏览器或目标站点不可达，应记录环境限制；不能把未执行的真实站点验收写成已通过。

### 12.4 广告过滤实测记录规范

每次修改广告规则都要保存一份不含 Cookie/token 的统计记录，至少包括：源、清单 URL 的脱敏标识、原始/过滤后字节数、分片数、总时长、`DISCONTINUITY` 数、删除的分片索引或 URL 特征、首个正片分片 HTTP 状态、key 状态和 ffprobe 音视频流信息。原始媒体文件不是必需的，但统计必须可复现。

当前已验证基线（站点清单会变化，不能当成永久常量）：

| 源 | 过滤前 | 过滤后 | 结论 |
|---|---|---|---|
| `18j` | 45,350 B、439 片、1,748.492 s | 44,824 B、434 片、1,734.867 s | 删除 5 个片头广告片段；首个正片分片 HTTP 206，AES key HTTP 200 |
| `ikanpp` 暴风 | 51,933 B、2,060 片、6,188.32 s、4 个边界 | 50,913 B、2,042 片、6,136.24 s、2 个边界 | 删除 18 个广告片段、两组约 26.04 s；`EXTINF` 与媒体 URL 数量一致，广告路径清除 |
| `ikanpp` json1080（「澳门新葡京」中插广告真实样本） | 38,363 B、706 片、2,820.44 s、6 个边界 | 37,632 B、691 片、2,761.92 s、3 个边界 | 删除 3 个广告块（16.47 s / 17.67 s / 16.47 s，共 50.52 s、15 片）；`EXTINF` 691 = 媒体 URL 691，片尾无孤立边界，剩余 3 块均为正片（497.72 / 1,296.04 / 968.16 s） |
| `trtag` 暴风（《苍兰诀》第18集，`play-0-17`） | 17,012 B、687 片、2,729.56 s、5 个边界 | 15,992 B、669 片、2,678.04 s、3 个边界 | 删除 18 个广告片段（两组 25.76 s，共 51.52 s）；`EXTINF` 669 = 媒体 URL 669、`adjump` 归零；首片 `0000000.ts` 经代理取回 HTTP 200 / 77,644 B（`video/mp2t`）；结尾无孤立边界 |
| `ikanpp` 光速（回归对照） | 47,318 B、590 片、2,494.38 s、0 个边界 | 不变 | 单一正片块，`drop_short_blocks` 因 `len(blocks)<3` 直接跳过 → 规则不会在无边界清单上误删 |

如果前后字节数、分片数或时长没有变化，不能直接宣称“广告已去除”；如果变化大于规则预期，先停止发布并检查是否误删正片、key、map 或音视频段。真实 CDN 出现 SSL EOF/504 属于源站可用性问题，不能归因于过滤成功或失败。

#### 短块广告规则（`hls_rules.drop_short_blocks`）

**2026-10 实测结论**：`ikanpp` 的聚合源（json1080）把广告当**独立短块**拼进清单，
广告块与正片**同主机、同目录**，分片 URL 不含任何广告词，唯一的差异就是「短」：

```
block0 124片 497.72s 正片 | block1  4片 16.47s 广告
block2 325片 1298.96s 正片 | block3 5片 17.67s 广告
block4 244片 973.16s 正片 | block5  4片 16.47s 广告（片尾）
```

因此「要求主机切换」的旧规则（`drop_inserted_blocks`）一条都命中不了，广告原样
播放——这是「澳门新葡京广告没去掉」的直接原因。新规则改按「短」判：

1. 清单内必须有 **≥2 个时长 ≥ `max_duration` 的正片块**（纯短视频/预告清单不动）；
2. 块的时长 ≤ `max_duration`（默认 30s，实测广告 16.5~17.7s，留 ~70% 余量）；
3. 块的时长 ≤ `max_ratio` × 清单总时长（默认 0.05，实测广告占 0.6%）；
4. 块与相邻正片之间有 `#EXT-X-DISCONTINUITY`（`require_boundary`）——片头块
   要求**后**边界、片尾块要求**前**边界、中间块要求两侧都有；
5. **内部块直接命中**；首/尾块额外要求「分片时长序列签名」与已命中的内部广告块
   一致（实测 block1 与 block5 签名均为 `[6.63,3.33,4.8,1.7]`，同一素材重复插入），
   这样才不会把正常片尾内容当广告删掉。

删块沿用中插广告的既有语义：合并两侧边界、保留 `#EXT-X-KEY`/`#EXT-X-MAP`、
`EXTINF` 与媒体 URL 数量保持一致；**片尾块被删时其前置边界也一并回收**，
否则结尾会留下 `#EXT-X-DISCONTINUITY` 紧跟 `#EXT-X-ENDLIST` 的孤立边界。

该规则只在源显式声明时生效（未声明 `ad_block` 的源走默认引擎，不会触发），
配置入口是源 JSON 的 `ad_block.hls_rules`。若日后出现 >30s 的广告块，先取真实
清单确认其占比与块结构，再单独上调 `max_duration`，不要用「块短于中位数」这类
相对条件替代——相对条件会把正常短场景误判成广告。

#### 已确认「清单层不可拦截」的形态（勿重复尝试）

以下结论来自 2026-10 对 `ikanpp` 聚合源的实测，记录在此以避免重复走弯路：

| 源 | 清单形态 | 实测结论 |
|---|---|---|
| `json1080`（`yzzy.play-cdn11.com`） | 6 块、6 个 `DISCONTINUITY`、广告块 16.5/17.7/16.5s | **可拦截**，已由 `drop_short_blocks` 处理（删 15 片/50.5s） |
| `jisu`（`vv.jisuzyv.com` → `p.jisuts.com:999`） | **单块、0 个 `DISCONTINUITY`**、AES-128 单密钥、分片名连续 `plistN.ts` | **清单层无任何标记**：换 Referer/UA/重复请求清单字节完全一致（非服务端动态插入）；分片体积与时长成正比（无低码率广告簇）；跨集同时间点分片密文无一相同（非固定时点的同一素材）；跨集时长子序列也无稳定签名 |

对 `jisu` 这类形态，广告与正片由同一路编码产出，`EXTINF`/`DISCONTINUITY`/URL/
主机/体积/跨集比对都不构成可判定信号，因此**不能靠改写清单剔除**。可行方向只有
「换到可拦截的线路」或「按用户可见的时间点做区间跳过」，不要试图用更宽的启发式
（例如「连续多片固定时长即广告」）去猜——那会直接删掉正片。

### 12.5 ikanpp HLS 卡顿调优记录

本节记录 `ikanpp` 当前唯一的播放专属优化，防止后续维护时把它误改成通用 HLS 策略。

#### 现象与根因判断

- `ikanpp` 的详情接口能够返回真实 HLS 地址，但播放器访问的地址可能经历“外层清单 -> 变体清单 -> AES-128 key/TS 分片”三段链路。
- 旧策略在播放首片时同时前瞻 8 片、开启 4 路预取。源站 CDN 的后续分片请求与播放器当前分片请求争抢连接和带宽，卡顿集中出现在首片起播和连续切片阶段。
- 这不是音频轨合并或广告删除逻辑的问题；不能通过删除帧、强制音频偏移或修改全局播放器参数来掩盖。

#### 当前策略

| 配置 | 当前值 | 设计目的 |
|---|---:|---|
| `media.hls.workers` | `4` | 仅控制下载器的分段下载，不控制实时播放器 |
| `media.hls.network_caching_ms` | `30000` | 用播放器自身缓冲吸收短时 CDN 抖动 |
| `media.hls.prefetch.enabled` | `false` | 禁止首片播放期间的后台分片争抢 |
| `media.hls.prefetch.depth` | `1` | 保留合法的最小配置边界，关闭时不应产生预取 |
| `media.hls.prefetch.workers` | `1` | 防止配置被误读为高并发播放策略 |

运行链路为：`VideoView._configure_playback_prefetch()` 读取源级配置 -> `MediaProxy.configure_prefetch()` 重置当前预取会话 -> `enabled=false` 时播放器只按需请求 `/c/` 分片 -> 媒体代理继续负责请求头、Referer、清单重写、AES key 和磁盘缓存。源配置显式关闭后，不会落入未声明 HLS 的通用默认预取。

#### 验证与后续验收

- 已对真实 `ikanpp` HLS 地址验证外层清单、内层清单和 4 个实际 TS 分片均返回 `200`，代理链路可以取得完整分片内容。
- 已通过 JSON 配置回归检查和 Python 语法编译检查；当前环境未安装 `pytest`，没有把未执行的全量测试写成通过。
- 源站清单、CDN 节点和网络状态会变化；后续若仍报告卡顿，必须分别记录首片首字节、分片吞吐、`403/429/5xx`、代理缓存命中和 VLC 缓冲事件，并进行至少 3--5 分钟实际播放与音画同步对比。
- 只有确认按需请求本身不足以维持播放，才重新评估低并发预取；调整时先从 `workers=1`、小 `depth` 开始，不能直接恢复旧的 8 片/4 路。

### 12.6 慢速 CDN 的分片交付（agedm / 18av 实测，通用规则）

#### 实测证据（来自真实播放留下的分片落盘时间戳，与清单 `EXTINF` 逐片对齐）

| 样本 | 结构 | 交付速率 | 慢于该片时长的次数 |
|---|---|---|---|
| `agedm`（AGE动漫，`3000k/hls/mixed.m3u8`） | 30+ 片、无边界、未加密 | 起播 7/8/8.2s 一片，中途 20.5s 空档 | 多数 |
| `18av` 27 分钟集（`cdn-*.streamfastpro.com`） | 236 片 / 1636s / 0 边界 | 内容 898s ÷ 墙钟 855s = **1.05×** | **61/128** |
| 同 CDN 另一集 | 256 片 / 1613s | 内容 54s ÷ 墙钟 12s = **4.66×** | 0/8 |
| `18av` 9.6 分钟集 | 100 片 / 577s | 1.14× | 33/88 |

结论：**同一个 CDN 既有 4.66× 的时段也有 0.43×（23.9s 下 10.4s 内容）的时段**，最差分片约 `100KB/s` 量级。抖动来自节点/单连接限速，不是总量不足；`agedm` 的 `3000k` 变体需 ~375KB/s，单连接必然不够。

#### 预取预算必须按体积换算（原固定 10s 会把预取打死）

- 旧实现给每个预取分片**固定 10s 总预算**：2.5MB 的分片在 100KB/s 下要 25s，必然超时 → 中止已下载字节 → 播放器重新下一次（白白多等一个 10s）→ 连续 3 次触发 `_PREFETCH_FAILURE_LIMIT` 熔断 → 之后整场只剩单连接顺序拉片，表现为持续卡顿与掉帧。
- 现规则（`_prefetch_budget`）：已知 `Content-Length` 后按 `32KB/s` 视为可接受下限换算总预算，下限 `_PREFETCH_TIMEOUT`(10s)、封顶 `_PREFETCH_MAX_TIMEOUT`(180s)；建连/抢锁阶段仍用 10s。
- 单次读超时取 `min(剩余预算, _PREFETCH_TIMEOUT)`：**慢但在动**的连接不会被总预算一刀切，真的 10s 一个字节都没到才放弃。
- **已写入字节的超时不算失败**（不累加 `_pf_failures`）：否则慢速 CDN 上 3 次就把整场预取熔断。零字节的失败仍照常计数并熔断，403/429 仍立即熔断。

#### 源级参数的三条约束

- `media.hls.network_caching_ms` **只对分片流生效**（`hls/dash/live` 分类）。键名是 `media.hls.*`，而 mp4 直链靠 Range 渐进读取，实测 15000ms 缓冲要等 15.2s 才开播；`format` 写 `mp4` 但真实地址是 m3u8 的源（`agedm`）按真实分类判定，仍然生效。
- 源**显式声明** `media.hls.prefetch` 即视为「按 HLS 调优」，不再要求 URL 里出现 `m3u8`——否则写错 `format` 的源会让声明好的参数静默失效。
- 并发预取能绕开单连接限速（实测 4 并发 ≈ 单连接的 2.1~3.3 倍），所以慢速 CDN 该开预取；`ikanpp` 的关闭决定（§12.5）只属于 `ikanpp`，不能推广成全局关闭。

#### 三个慢速源的当前定标（agedm / 18av / omofuna）

| 源 | `network_caching_ms` | `prefetch` | 依据 |
|---|---:|---|---|
| `agedm` | 15000 | `true, depth 10, workers 4` | 3000k 变体需要 ~375KB/s，实测只有 40~250KB/s |
| `18av` | 15000 | `true, depth 10, workers 4` | 实测 1.05× 实时、**61/128** 次交付慢于该片自身时长，最差 23.9s 下 10.4s 内容 |
| `omofuna` | 12000 | `true, depth 8, workers 3` | 分片 4~8s、平均 2.18s 一片（≈1.8×），但峰值间隙 12.8s；另受 §12.7 的分片整理影响 |

`depth` 取「约 8~20s 内容窗口」覆盖观测到的 20s 级空档；`workers` 按 `4×100KB/s ≈ 400KB/s` 定，恰好压过 3000k 需要的 375KB/s。三者都只作用于本源，不影响其它源。

验收口径：同一集连续播放 ≥3 分钟，`data/cache/video/<key>/` 里分片落盘间隔应稳定小于该片 `EXTINF`；若仍出现 20s 级空档，先看 `_pf_disabled` 是否被置位（403/429 或无字节超时），再考虑抬 `workers`。熔断会打一行 `[hls-prefetch] 熔断：…` 到 `logs/framework-*.jsonl`，这是判断"预取到底有没有在跑"的第一现场。

#### 已知限制（尚未修，别误以为已解决）

- ~~播放器请求某片时，若该片正在被预取，`_serve_cache` 会取消预取再自己下一遍，已下载的 `.part` 字节被丢弃。~~ 已修（见 §12.8 根因三）：`_segment_guard` 先等预取收尾，等不到才打断并保留 `.part` 续传。仍存在的只是"取消瞬间在途但未落盘的字节"。
- `_PREFETCH_TIMEOUT` 仍是**建连/抢锁**的预算；若 CDN 在建连阶段就慢（TLS 握手 20s），预取依然会被判超时，这类样本要单独看 `logs/framework-*.jsonl` 的失败原因。
- 本节全部结论来自 `data/cache/video` 的真实落盘时间戳与 `EXTINF` 对齐，没有做VLC内部缓冲事件采集；要更硬的结论需接 VLC 的 `--stats` 或 HTTP 控制接口取 `demux_lost_frames`。

### 12.7 分片时间轴整理：不得在分片路径起子进程（omofuna 实测）

`omofuna` 的 HLS 是把 mp4 硬切成 TS（每片边界不一定是 I 帧），因此声明了 `ad_block.hls_rules.remux_timestamps`，由媒体代理把第 2 片起的时间轴整体平移到前一片结束点之后。

#### 旧实现的三个后果（一集 46 片）

| 观测 | 证据 |
|---|---|
| 每片一次 `ffmpeg -c copy` 重封装 | `data/cache/video/<key>/` 里有 **46 个 `<分片>.remuxed` 标记**，等于 46 次进程创建 |
| Windows 下每个进程弹一个控制台窗口 | `subprocess.run` 没带 `CREATE_NO_WINDOW`，而 `framework/subprocess_no_window.py` 早就存在，其它调用点（downloader / external_player / ffmpeg_merger / ytdlp）都用了 |
| 分片交付被拖慢 + 抢焦点 | 进程冷启动 ~0.13s 起，且整条清单共用 `_remux_serial` 串行锁；窗口创建还会抢前台焦点 → 播放器缓冲见底 → **跳帧 + 音频断断续续** |

对照事实：同一集在源站网页上播放正常（用户实测），说明片源本身没问题，问题出在本地这一层加工。

#### 现规则

- `_ts_shift_bytes(data, first_dts)`：**纯字节**改 PES PTS/DTS 与 PCR，返回 `(新字节, 本片时长)`；`_ts_shift(path, …)` 只是就地写文件的薄封装（测试/调试用）。
- **已连续就不动**：本片最早时间戳 `min_ts >= first_dts`（±0.5ms）时返回 `None` 直通。实测 omofuna 真实分片本就带连续时间戳（如 19.440 / 31.305），因此常见路径等于"原样交给播放器"，与源站行为一致。
- **只允许整体平移**：同一次调用的位移对**所有 PID** 完全相同。真实分片校验（`python tools/_probe_playback.py remux`）：

  | 样本 | 视频 pid0100 | 音频 pid0101 | 位移 |
  |---|---|---|---|
  | f2286663/841c53f7… | 1.400 | 1.457 | 两流均为 98.6 |
  | ecd3d114/7ea04a55… | 31.305 | 31.307 | 两流均为 68.695122 |

  两流位移必须恒等，否则就是"声音断断续续"的来源。因此**整片只算一个 `bump`**：
  先扫全片得到「最小时戳 → 目标起点」的位移，再对所有 PES 与 PCR 套同一个值；
  任何"逐包/逐流微调"（旧实现的 `resid`、`delta_by_off`）都可能让音频与视频在
  同一片内各自走表，一片差 20ms、几百片之后就是肉眼可辨的音画不同步。
  单流内部连续性真的坏掉时**不修**：直接返回 `None` 让该片直通（`0 < prev-dts
  < 1h` 视为倒退，33 位回绕不算），交给播放器自己处理 —— 一个正确的错误比一个
  悄悄改坏的时间轴更容易诊断。
- 播放分片路径**不得**起 ffmpeg/ffprobe 等子进程；确需子进程时必须走 `framework/subprocess_no_window.py` 的 `run()`/`popen()`（或显式 `**no_window_kwargs()`），否则 Windows 上就是一个个黑窗口。

#### 验收

1. 播一集 omofuna，`Get-Process` 里不应出现 ffmpeg，任务栏不应再闪控制台窗口；
2. `data/cache/video/<key>/` 的 `.remuxed` 数量只应随**真正需要平移**的分片增长（本来连续的片不写标记也不改写）；
3. 与源站同集对比音画同步；`python tools/_probe_playback.py remux` 输出的各流位移必须一致。
4. `tests/test_hls_remux_sync.py`：单 delta 不变式（音视频偏移 tick 级保持不变、
   PCR 与 PES 同 delta、跨片前向 splicing 保持偏移、单流倒退返回 `None`、33 位
   回绕不被误判）；`tests/test_media_proxy_remux_timeline.py` 守住既有时间轴规则。

#### 适用范围（别扩散结论）

- 分片整理/时间轴平移**只在声明了 `ad_block.hls_rules.remux_timestamps` 的源生效**，当前只有 `omofuna`。`agedm`、`18av` 从不走这条路径，它们没有 ffmpeg 参与，因此它们的"卡顿/掉帧/音频断续"属于 §12.6 的交付问题（实测 1.05~1.14× 实时、大量分片慢于自身时长），用预取与缓冲治理；两个机制的账要分开算。
- 静态闸门 `tests/test_no_console_windows.py`：扫 `framework/` 与 `gui/` 里所有 `subprocess.run/Popen/call/check_output`，没带 `**no_window_kwargs()`（或显式 `creationflags=`）就失败；posix-only 的 `xdg-open` 在白名单里。以后新增调用点不会再悄悄弹窗。
- 判断"窗口是谁弹的"：先 `Get-Process ffmpeg,ffprobe,yt-dlp,node` 看有没有进程，再看 `data/cache/video/<key>/*.remuxed` 计数。子进程都不存在时还闪窗口，来源就不在应用进程里（例如开发者终端里自己跑的诊断命令），不要继续改播放代码。

### 12.8 起播延迟：变体扇出 + 同一片重复下载（2026-11-01 实测）

用户报的是"**全部**视频载入都慢"。注意区分两件事：**交付速率**（能不能跟上播放）与**起播延迟**（点开一集到出画）。先量再改：

```powershell
python tools\_probe_playback.py delivery      # 交付速率：2.72× ~ 9.12× 实时 → 不是带宽问题
python tools\_probe_startup.py proxy          # 代理侧：清单→首片、分片回源次数
python tools\_probe_startup.py all            # 真 mpv 起播：spawn → state3
```

`_probe_startup.py` 自带一个本地合成上游（主清单 + 3 变体 + 每片 300KB，支持 Range，`PROBE_SEG_RATE_KBPS` 可限速），所以结论可复现、不依赖当天网络。

#### 根因一：主清单的三路变体各被打开一遍

真实缓存目录给出的证据（用户机 `data/cache/video/`）：同一集在 **1 秒内**建了 3 个 key 目录（`hls-1080p` / `hls-360p` / `hls-250p`），清单落盘时间 23:35:37–38，首片分别晚 10s / 36s / 43s 才出现——三路在抢同一条出口带宽。合成上游复现：修前 `清单请求数=4 分片请求数=18`，`/v720.m3u8`、`/v360.m3u8` 的前三片都在抓。

`max_videos` 的 LRU 下，一部片子吃 3 个槽，等于顺手把"重看第二遍能秒开"的缓存挤掉。折叠后一部片子只占 1 个槽，所以 `app_config.json` 的 `video_cache.max_videos` 从 5 调到 12（字节上限仍是 8GB，按实际看过的分片占位）：重看/连播时命中的是一整集，而不是被自己的另外两个画质挤出去。

修法：`collapse_master_variants()`（`framework/media_proxy.py`）在主清单下发前只留**最高码率**一路（与播放器本来就会选的那路一致），`#EXT-X-MEDIA` 独立音轨组原样保留；看不懂的清单形状（`STREAM-INF` 后不是 URI 行）**原样返回**，宁可多请求不许改坏。旋钮 `media.hls.single_variant`（默认 `true`）。修后：`清单请求数=2 分片请求数=12`，`v720`/`v360` 一次都没被请求。

#### 根因二：mpv 对**每一片**都发 `Range: bytes=0-`，旧代码当"拖动"处理

实测（`_probe_startup.py all` 的 DEBUG 日志）：`Range='bytes=0-'` 出现在每个分片请求上。旧代码 `shared_fetch = not handler.headers.get("Range")`、`_full_zero_offset_response(resp, handler.headers.get("Range"))` 都把它当拖动，于是同时丢了四件事：分片路径锁 / 预取握手、`.part` 续传、以及**播放路径的落盘**（只有预取在写缓存 → 关掉预取就永远没有第二次命中）。现在只有"从中间取"才算 Range：`from_head = range_start is None or (range_start == 0 and known_segment)`，`bytes=0-` 在**清单里出现过的分片**上按整片处理（真拖动仍是 Range，语义不变）。

`known_segment`（`target in self._seg_pos[cache_key]`）这个限定不能省：`/c/<key>/<整片 mp4>` 是"单文件走缓存路由"，它的 `Range` 必须保持"不进单飞、各自独立回源"的老语义，`tests/test_media_cache.py::test_cached_route_range_misses_bypass_single_flight` 锁的就是这条；否则两个并发 `bytes=0-` 会被合并成一次上游请求，下载器/拖动行为随之改变。

#### 根因三：撞车就是"取消 + 重下"

旧行为：播放器要第 N 片时无条件 `_cancel_prefetch_url()`，预取 `abort()` 掉已下载字节，播放器从 0 重下。合成上游（300KB/片、150KB/s 限速）实测：同一片被完整下载两遍。

现行为：

1. `_segment_guard()` 先给在途预取 `_PREFETCH_HANDOVER_WAIT = 0.15s` 收尾，写完了直接命中本地。**窗口必须短**：起播时撞车几乎必然发生（预取是清单刚下发时排的队，播放器下一个请求就到），在慢 CDN 上赌"它快写完了"等于白拿起播时间换一次大概率失败的等待；150ms 只用来覆盖"已到尾巴、只差 rename"这一种。
2. 等不到才打断，并把这片标记进 `_pf_takeover`：只有"播放器接管"才走 `_FileTee.pause()` **保留 `.part`**；`stop()`／换会话／清单结束这类取消仍然 `abort()` 整片丢掉（半截字节没有续传价值）。
3. 本请求带 `Range: bytes=<已下载>-` 续取，并且对客户端仍是完整 200（头部字节从磁盘回放 + 续流剩余部分）。
4. 拿锁之后再查一次命中（等锁期间预取可能刚好写完）。

限速实测（300KB/片、150KB/s、12 片）：

| | 分片回源次数 | 上游分片字节 |
|---|---|---|
| 修前（取消 + abort） | 每撞一次多一整片 | **2.00×**（同一片完整下两遍） |
| 修后（0.15s 握手 + 续传） | 18 次 / 11 个不同分片 | **1.19×** |

不限速（本地合成上游）修后是 11 次请求 / 11 个不同分片 = **1.00×**，一片不多下。多出来的那 19% 是取消瞬间已进 socket 但还没落盘的字节，回收不了（要回收得在分片内做多连接 Range，未验证）。

#### 验收

1. `python tools\_probe_startup.py all`（可加 `PROBE_SEG_RATE_KBPS=150` 模拟慢 CDN）：同一 `segNNN.ts` 在上游表里只应出现一次；末行 `分片上游总字节` 应≈`单份 的 1.00×`；`state3` 在本地应在 1s 内；
2. `tests/test_hls_single_variant.py`、`tests/test_hls_segment_handover.py` 全绿（分别锁折叠行为、`bytes=0-` 命中/续传/有界握手/接管标记/`pause` 保留字节）；
3. 真机复看：`data/cache/video/` 一集只应有一个 key；`logs/framework-*.jsonl` 出现 `[起播] 清单→首片 x.xxs` 与 `[起播] 首帧 x.xxs`（`video_view._on_player_time` 打点），拿这两个数说话，别再凭感觉。

#### 已知遗留（与本节改动无关，别记错账）

`tests/test_hls_throughput_tuning.py` 里 `test_prefetch_stops_at_playlist_end` 与 `test_slow_progressing_prefetch_does_not_open_circuit` 在**本次改动前的基线**上同样失败（用 `git stash` 掉 `framework/media_proxy.py` 复跑确认过），属于预取状态机自身的旧账；要修另开一轮，别把它们当成起播回归。

#### 尚未覆盖

- `_PREFETCH_TIMEOUT` 仍是建连/抢锁预算，TLS 握手就慢 20s 的 CDN 依旧判超时；
- 撞车时的"取消瞬间在途字节"无法回收（要回收得在分片内做多连接 Range，未验证）；
- 当晚真实网络本身也有问题（`系统代理不通…改直连重试`、`Read timed out (read timeout=8.0)`、`命中限流页，11s 后重试`、WAF `AntiScrapeError`），这类日志说明还有**环境/源站**因素，不能全记在本地代理账上。

## 13. 已知限制与边界
- 目标网站可以随时改版、关闭、启用 WAF、修改 Cookie 或返回不同地区内容。
- 需要登录、会员权限、验证码、DRM 或受限 API 的内容不能保证匿名完整读取。
- Playwright 依赖浏览器运行环境；浏览器不可用时只能走 HTTP/API 或显示明确失败。
- 远程 CDN 图片和视频可能要求 Referer、Cookie、跨域策略或外部播放器。
- `ikanpp` 暴风样本曾观察到音频相对视频约提前 60–75 ms，且多个片段都存在；这更像源站编码偏移，不得未经对比就通过广告过滤器或全局播放器补偿。
- 第三方源的解密配置必须以合法可访问样本验证；项目不保证破解受保护的付费内容。
- WebDAV、部分投屏/转码和某些高级媒体能力属于预留或依赖本机环境。

## 14. 文件与改动检查表

### 14.1 修改界面功能

- [ ] 是否找到正确的 `gui/pages/` 或 `gui/pages/reader/` 页面？
- [ ] 网络/磁盘工作是否离开 UI 线程？
- [ ] 是否处理 loading、empty、error、stale result 和关闭窗口？
- [ ] 是否保持主题文字对比度、按钮尺寸和布局稳定？
- [ ] 是否补充恢复进度、缓存命中和重复打开行为？

### 14.2 制作或修复源

- [ ] `$id`、`$type`、`$name`、`transports.base_url` 正确。
- [ ] 发现、搜索、详情、目录、正文/图片/视频链路分别验证。
- [ ] 真实请求头、Cookie、Referer、渲染等待和重定向已记录。
- [ ] 选择器有 fallback，根节点自身属性使用 `xpath: .`。
- [ ] 相对 URL、懒加载图片、标题清洗、正文段落和媒体候选已验证。
- [ ] 目录完整展开后排序，不在页面层写源特判。
- [ ] API/Playwright/解密/广告过滤配置与实际响应一致。
- [ ] 若存在专属功能，已对照 §7.12 登记其目的、入口、缓存影响和回归验证；没有把源特例移入页面层。
- [ ] `constraints`、cache version、diagnostics 已设置。
- [ ] 有离线夹具或最少一个针对源的回归测试。
- [ ] 未把 Cookie、密码或个人数据写入提交文件。

### 14.3 修改缓存或公共解析逻辑

- [ ] 已搜索所有调用方和同类实现。
- [ ] 已判断是否会污染旧缓存。
- [ ] 已检查 HLS 广告规则、清单缓存策略和源级预取是否需要新的策略指纹或版本。
- [ ] 已验证会话缓存不会持久化。
- [ ] 已覆盖空值、异常 HTML、重复请求、关闭窗口和迟到结果。
- [ ] 已运行直接测试及相关回归测试。

## 15. 维护约定

本文件只记录当前实现、稳定设计原则和未来修改所需的操作规范。临时调研、一次性网站响应、旧方案、失败尝试和历史会话不再单独维护为项目说明文档；若某个决定成为长期约束，应在这里补充“设计原则、代码落点、验收标准和限制”，并同步当前代码和测试。
