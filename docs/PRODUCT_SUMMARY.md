# Claw 产品总结说明文档

> 版本：v1.1 · 日期：2026-10-11 · 状态：现状定稿
>
> 本文档是**实现后**的产品总结：说明产品是什么、功能面、架构分层，以及最重要的「设计理念 / 设计思路」——是后续任何改动的第一参考。
> 规划期总描述见 `docs/PROJECT.md`；架构 / 源配置 / 主题 / 内核分别见 `docs/architecture.md`、`docs/source-schema-v2.md`、`docs/theme.md`、`docs/core.md`。

---

## 一、产品一句话

配置驱动的**多源爬虫桌面应用**（PySide6 GUI + framework 内核）：一个桌面程序同时挂多个小说 / 漫画 / 视频源，规则全部外置（源 JSON + 广告规则库），支持浏览、搜索、阅读、下载、书架、源管理、投屏播放。

技术栈：Python（标准库优先，可选用 requests / lxml / playwright / VLC）+ PySide6/PyQt6 + JSON/SQLite + YAML 广告规则。可选依赖缺失时自动降级，不阻断核心功能。

## 二、功能总览

| 界面 | 形态 | 职责 |
|---|---|---|
| 首页 | 窗口落地页 | 统计、最近搜索、后台任务、批量更新检测、三态吉祥物 |
| 发现 | 选项卡 | 选源 → 分类 / 全站 → 懒加载列表 → 详情 |
| 搜索 | 选项卡 | 跨源 / 单源关键词搜索，来源角标筛选 |
| 阅读器 | 选项卡 | 小说正文 / 漫画翻页 / 视频分集 / EPUB；续读、预取、自动滚动 |
| 下载 | 选项卡 | 队列、进度、并发、续传、重试、HLS 并行 |
| 书架 | 选项卡 | 本地已下载 + 收藏 + 续读记忆，CSS 懒加载 |
| 源管理 | 选项卡 | 源启停、健康灯、诊断、权重、自动禁用 |
| 源编辑器 | 对话框 / 全屏 | 可视化编辑源配置 + 实时预览 |
| 设置 | 对话框 | app_config 分块 + 广告规则目录 |

全局快捷键 `Ctrl+1..8` 切换 Tab、`Ctrl+F` 聚焦搜索、`Ctrl+Enter` 搜索、`Ctrl+D` 加入下载、`F5` 刷新，见 `docs/PROJECT.md`。

## 三、架构分层与模块导览

```
claw/
├── gui/                  # 表现层（PySide6），app.py 统一编排
│   ├── app.py            # 主窗口 + 分阶段启动 + 服务发布 + 退出编排（唯一进程入口）
│   ├── base_page.py      # 页面基类（占位、可重试加载态、懒 Tab）
│   └── pages/            # 八大 Tab + 阅读器子包
│       └── reader/       # novel / comic / video / epub 视图（已按职责拆包）
├── framework/            # 内核（纯 Python，不依赖 GUI）
│   ├── atomic_io.py        # 原子写盘统一（新）
│   ├── cache_service.py    # 会话/搜索/书架缓存单例（受限反序列化）
│   ├── content/            # 抓取解析主逻辑（拆分后的包：shared/base/detail/chapter/comic/video）
│   ├── source_manager.py   # 源配置/健康/自动禁用/缓存失效
│   ├── shelf_service.py / shelf_cache_repository.py / shelf_precache.py
│   ├── download_queue.py / downloader.py / download_history.py
│   ├── reading_progress.py # 进度/续读 + 位置快照 + GenerationGuard 公共工具
│   ├── media_proxy.py / media_cache.py / media_tuner.py / adblock.py
│   ├── external_player.py / vlc_player.py / cast_session.py / dlna.py / cast_history.py
│   └── ...（http/proxy/parser/search/discovery/events/settings ...）
├── sources/*.json       # 全部站源声明（schema v2，规则外置）
├── docs/                # 规划文档 + 本总结
├── tests/               # pytest 套件（约 1600+ 用例）
```

按职责归类：配置与设置（`config`/`settings_manager`/`theme_manager`）、源管理（`source_manager`+`sources/`）、抓取（`http`/`proxy_pool`/`cookie_manager`/`signers`/`playwright_helper`/`bulk_fetch`）、解析（`parser`/`search`/`discovery`/`content/`）、自检健康（`selfcheck`/`health`）、缓存（`cache_service`/`library_store`/`media_cache`/`search_history`）、书架（`shelf_*`/`series_group`/`folder_lock`/`comic_cover_recovery`）、下载（`download_*`/`epub_*`/`ffmpeg_merger`/`transcode_session`/`ytdlp`）、播放投屏（`vlc_player`/`external_player`/`media_proxy`/`stream_resolver`/`cast_*`/`dlna`）、基础设施（`atomic_io`/`events`/`logging_setup`/`errors`/`utils`）。

## 四、设计理念与设计思路（改动的第一参考）

以下原则是历次实现沉淀的核心，任何新改动都应在这些约束内进行；破坏它们必须视为重大变更并提前确认。

### 4.1 配置驱动、规则外置
- 站点差异全部外置到 `sources/*.json`（声明式 schema v2：endpoints / parser / list / detail / anti-scrape），广告规则外置到广告规则库，内核零硬编码站点细节。
- 新源 = 新增一份 JSON，通常无需改内核代码（复杂源可加 provider 模块，如 `ikanpp_provider_memory`）。

### 4.2 分阶段启动与服务原子发布
- **首帧同步只读**：启动第一帧只用「同步 + 只读」的能力（白名单数据），交互重服务全部经占位 → `LOADING` → `READY/FAILED` 状态机渐进就绪，可重试。
- 服务发布采用「先备齐全部服务对象 → 单一原子提交点 → 任一步失败整体回滚」`_prepare_runtime_services`，避免残留半套服务。
- **代际（generation）与取消**：所有可能过期的异步结果（扫描、预取、换源、详情）带回世代号，新任务使旧代失效、旧代回调被拒绝，防止迟到结果覆盖新状态。

### 4.3 线程边界与并发模型
- 纯 GUI 线程凭据：**worker 不碰 QWidget**，跨线程统一走 `EventBus`（Qt 桥 QueuedConnection）/ QRunnable+Signal。
- 后台并发全部有界且有明确上限（线程池、playwright worker 1..8、下载并发、渲染队列）；共享结构用锁（如 `search_history` RLock），**锁内不做长 IO（gzip/网络）**——gzip 等重操作移到锁外。
- 退出编排按序执行：停任务 → 失效回调 → 关闭事件总线 → 有界等待线程池/预热线程（≤2s）→ flush 进度 → 关连接；**不允许退出被无限阻塞**。

### 4.4 数据与存储模型
- **JSON 快存 + SQLite 镜像**：收藏/历史/续读用 JSON（生命周期短、易迁移），书架索引用 SQLite；两者语义明确分工。
- **原子写盘统一**（`framework/atomic_io.py`）：所有关键 JSON 落盘走 `atomic_write_json/text/bytes/stream`（临时文件 + `os.replace`），失败路径必须是可观察的失败（`log.warning` + 原因），禁止静默吞异常；读取侧带长度容错（`read_text_lenient`）。
- **失败关闭（fail-closed）**：持久化加载失败时进入「只读保护态」（`load_failed` 禁写，避免把损坏覆盖成空库）；隐私数据（进度/历史）读写均走同一策略，绝不因解析失败暴露或清空。
- **缓存归属**：缓存带前缀/归属校验（`_is_owned_payload` 强白名单），修复/清理只操作自有临时载荷，绝不误删他人数据；TTL 明确的缓存才可清理，pinned（置顶收藏）条目永不清理。

### 4.5 源管理、自检与安全
- 源健康：`selfcheck` 定时自检，broken 状态**同步失效该源缓存**（`source_manager._invalidate_source_cache`），避免带毒缓存反复失败。
- 自动禁用策略：连续失败自动禁用，状态**持久化**（`sources_runtime.json`），重启恢复但不覆盖用户的显式启停。
- 渲染预算：需要 JS 的源经 `playwright_helper` 渲染，worker 数可配且有界；等待/解密/签名（`signers`/`acw_solver`）集中处理。
- **输入安全**：用户输入只在服务端可信边界校验；搜索关键词归一化后查询；解析失败**可见**（`[]` + ERROR）绝不静默把整页当条目；网络调用统一超时/重试/退避且有上限。

### 4.6 书架懒加载与增量
- 书架卡片只展示元数据索引（轻量），封面/详情**懒加载**（miss 时占位，异步回填并去重）。
- 书架扫描支持**增量**（目录 mtime 签名缓存，未变更目录复用）+ 全量回退路径，输出一致；扫描带世代号，新扫描立刻使旧扫描失效（`_ScanTask` + `cancel_precache_jobs`）。

### 4.7 下载模型
- **幂等**：任务入队即置 `DOWNLOADING`，重复入队/重试语义明确（retry 只勾选失败项）；终态判定单点 `_finalize_status`（暂停优先保持、取消优先于失败）。
- **内存有界**：EPUB 材料累积超过阈值（默认 64MB）自动 spill 落盘，流式续写；成功/取消清理中间产物，失败保留供重试续用。
- 历史 `downloads.json` 内容未变不重复写盘（指纹去重 + 原子写）；视频清理可按 `book_key` 精准删除而非全表扫描。

### 4.8 阅读器
- 位置快照/恢复由公共工具统一（`reading_progress.PositionSnapshot`），四类视图（novel/comic/video/epub）行为一致；进度 flush 统一覆盖视频与 EPUB。
- 代际守卫统一为 `GenerationGuard`，换源/翻章时旧请求作废；预取按量钳制（`ahead<=1`、`behind==0`）且**按资源键去重**，杜绝同一请求并发多份。
- 巨型文件按职责拆包（video→common/tasks/widgets、comic/novel→tasks、content→`framework/content/` 包），**对外 import 门面保持兼容**；epub 拆分缓存按书指纹（path/size/mtime）区分。

### 4.9 播放 / 投屏
- 本地 VLC 与外部播放器（浏览器/VLC 外链）双路径；外链回退路径进度不回写时给出**可观察日志**。
- DLNA 发现多轮 M-SEARCH 聚合去重；音量动作路由到 `RenderingControl`（严格 0-100 校验），无支持能力时**受控失败**而非静默。
- MediaProxy：m3u8 按上游最终 `resp.url` 重写、签名清单不落盘、`/c/` 分片**断点续传**（`.part` TTL 内跨重启保留、416 时丢弃重取、回放磁盘头部再续流）、`/e/` 惰性系列、看门狗与租约。
- adblock：通用预滚回退（R5）为唯一路径，已移除退网源的专属分支；播放超时口径单点收敛（常量 + 文案派生）。

### 4.10 安全与可靠性基线
- 缓存反序列化走**受限 unpickler**（白名单：builtins 基础容器/标量 + 允许的项目类型），任何越界类型 → 空缓存，**不执行未知代码**。
- 关键写路径全部原子化（见 4.4）；异常处理统一「可观察日志 + 明确失败」，禁止无差别空 `except`/`pass`。
- 敏感信息（cookie/密钥/路径）不入普通日志、错误响应不泄露堆栈。
- 对第 三/四方并发写入者保持警惕：`gui/app.py` 曾被外部进程覆盖损坏，修改后须做完整性自检（`ast.parse`/行数/关键符号）。

## 五、本轮 44 项优化记录（已全部实施）

> 本轮（2026-10）按「通读 → 汇报可优化点（含影响程度）→ 用户确认全部实施 → 密集波次执行 → QA 全量回归 + 独立复审通过」完成。影响程度：高/中/低。

| 组 | 编号 | 内容 | 影响 | 落地位置 |
|---|---|---|---|---|
| 基础 | F1 | 原子写盘统一 | 中 | `framework/atomic_io.py`（新）；5 处改写 |
| 基础 | A2 | 设置封装（get 深拷贝、save 捕 JSON 异常） | 中 | `settings_manager.py:160-184` |
| 基础 | A7 | 缓存单例解耦（三个 getter 独立懒加载） | 中 | `cache_service.py` `_configure_shelf/search/session` |
| 基础 | A8 | 搜索历史线程锁 + 同步装饰器 | 中 | `search_history.py` RLock + `_synchronized` |
| 基础 | D2 | 缓存受限反序列化白名单 | 高 | `cache_service.py:33-78,250-310` |
| 源 | D1 | 源 broken → 缓存失效 | 高 | `source_manager.py:352`、`content/video.py:76-140` |
| 源 | D3 | 搜索关键词归一化 | 中 | `framework/search.py` |
| 源 | D4 | 流解析签名名单 / TTL 配置化 | 中 | `framework/stream_resolver.py` |
| 源 | D5 | playwright 渲染 worker 有界池 | 中 | `framework/playwright_helper.py`（1..8 / env） |
| 源 | D6 | `_settings_opt` 不永久缓存空配置 | 中低 | `framework/content/base.py` |
| 源 | D7 | 无 lxml 解析失败可见 | 低 | `framework/parser.py`（`[]`+ERROR 告警） |
| 源 | D8 | check_links 复用 thread-local client | 中低 | `framework/discovery.py:808-930` |
| 源 | D9 | 自动禁用状态持久化 | 中低 | `source_manager.py:461-514` |
| 源 | D10 | 详情 HTML 二级缓存加 TTL | 中 | `content/base.py:231`（`_PAGE_HTML_TTL`=7 天） |
| 书架 | C1 | 书架增量扫描 + 代际丢弃迟到 | 高 | `shelf_service.py`（mtime 签名 + generation） |
| 书架 | C2 | 预取取消真实接线 | 中高 | `shelf_precache.py:86-95`、`library_page.py` |
| 书架 | C3 | 扫描代际（新扫描失效旧扫描） | 中 | `library_page.py` `_ScanTask`/`_scan_generation` |
| 书架 | C4 | gzip 移出 RLock（锁内仅内存结构） | 中高 | `shelf_cache_repository.py:228-290` |
| 书架 | C9 | pinned 条目绝不清理 | 高 | `shelf_cache_repository.py:391` |
| 书架 | C8 | prune 视频历史按 book_key | 中 | `shelf_cache_repository.py:162-240` |
| 书架 | C10 | epub 类型缓存仅变更写 + `_last_img_emit` 清理 | 低中 | `shelf_service.py`、`download_queue.py` |
| 下载 | C5 | 下载终态判定单点 `_finalize_status` | 中 | `download_queue.py`（三处共用） |
| 下载 | C6 | 材料溢出落盘（内存有界） | 中 | `downloader.py`（默认 64MB，spill） |
| 下载 | C7 | downloads.json 指纹去重 + 原子写 | 中 | `download_history.py:104-154` |
| 阅读 | B1 | 位置快照公共工具 + 四视图接入 | 中 | `reading_progress.PositionSnapshot` |
| 阅读 | B2 | 代际守卫统一 GenerationGuard | 高 | `reading_progress` + `reader_page._switch_guard` |
| 阅读 | B3 | 预取参数钳制（ahead<=1/behind=0） | 低 | `settings_manager.reader_prefetch_settings` |
| 阅读 | B4 | 巨型文件拆分（novel/comic/video/content） | 高 | `reader/*.py`、`framework/content/` 包 |
| 阅读 | B5 | 自动滚动单一语义 | 低中 | `reader/auto_scroll.py`（Mixin） |
| 阅读 | B6 | 进度载荷健壮解包 | 中 | `reader_page._on_progress_signal` |
| 阅读 | B7 | 消除跨对象私有属性访问 | 低 | reader_page ↔ video_view 公共接口 |
| 阅读 | B8 | 预取按资源键去重 | 中 | `content` `_inflight_call` |
| 阅读 | B9 | flush 统一覆盖 epub/video | 中 | `reader_page.flush_progress` |
| 阅读 | B10 | epub 拆分缓存按书指纹 | 低 | `epub_view._cache_key`（path/size/mtime） |
| 阅读 | B11 | ebooklib→FastEpub 遗留清理 | 低 | 阅读/EPUB 相关遗留分支 |
| 应用 | A1 | 死代码删除 | 低 | `app.py`（两个 `_schedule_*`） |
| 应用 | A3 | 服务发布原子提交 + 整体回滚 | 中 | `app.py` `_prepare_runtime_services` |
| 应用 | A4 | 封面任务引用注销（防泄漏） | 中 | `app.py` backfill finished 清理 |
| 应用 | A5 | 退出有界等待线程池/VLC 预热 | 低 | `app.py` closeEvent（2s/2s） |
| 应用 | A6 | 异常吞没收口为可观察日志 | 低 | `app.py` 各 `except: pass` |
| 应用 | F2 | QRunnable 样板统一 `_AppRunnable` | 低中 | `app.py` |
| 播放 | E1 | 续播按已保存进度寻位（确认已实现，补测试） | 中 | `external_player.py:643-653` + 回归测试 |
| 播放 | E2 | DLNA 音量（RenderingControl 路由 + 严格校验） | 低 | `dlna.py`、`cast_session.py`、`video_view.py` |
| 播放 | E3 | DLNA 多轮 M-SEARCH 聚合 | 中 | `dlna.py`（3 轮/0.25s/≤2s） |
| 播放 | E4 | HLS 半缓存跨重启续传 | 低 | `media_cache.py`（`_PART_TTL`）、`media_proxy.py`（Range 续流） |
| 播放 | E5 | 投屏超时口径统一单点 | 低 | `video_view.py`（常量 8000 + 文案派生） |
| 播放 | E6 | MediaCache touch 淘汰节流 | 低 | `media_cache.py`（prune_interval=60s） |
| 播放 | E7 | 外置播放器进度回写可观察日志 | 低 | `external_player.py` |
| 播放 | E8 | 移除 18j 专属预滚分支 | 低 | `adblock.py`（保留通用 R5） |

## 六、测试与质量基线

- 运行：`C:\Users\alonely\AppData\Local\Programs\Python\Python310\python.exe -m pytest tests/ -q -p no:cacheprovider`（约 49 分钟，1600+ passed）。
- **已知基线失败（接受范围，勿误报）**：
  - 缓存重构 WIP：`test_cache_service.py` 2 条。
  - 缺 fixtures：`test_dm84_source.py`（缺 `dm84.json`）、`test_baozimh_cover_rewrite.py`（缺 `baozimh.json`）。
  - 直接投屏（IMP-2）未实现：`test_video_cast.py` 6 条。
  - HLS 参数默认值/漫画滚动取整差异：`test_hls_throughput_tuning.py` 5 条、`test_acgxmh_perf.py` 1 条、`test_comic_scroll_anchor.py` 1 条。
  - Qt offscreen 环境崩溃：`test_card_layout_qss.py` 与 `test_discover_column_stretch.py` 不可在同一进程连跑（本机环境现象）。
- 提交前自检：`python -m compileall`（改动文件）+ `git diff --check` + `ast.parse`（大文件完整性）。

## 七、给未来修改者的约束

1. **源改动优先改 JSON**，不要硬编码站点细节；确需 provider 逻辑时放 `framework` 对应模块并保持规则外置。
2. **线程边界**：worker 永不触碰 QWidget；跨线程一律事件/信号；并发必须有界。
3. **写盘/缓存**：新持久化落盘必须走 `atomic_io`；缓存键带归属与 TTL；pinned 数据不可清理；反序列化沿用受限白名单。
4. **异步结果**：任何「可能迟到」的结果都带代际或等于可取消，禁止覆盖新状态。
5. **异常处理**：可观察失败 + 明确原因；禁止空 `except`；敏感信息不入日志。
6. **范围与确认**：新功能、数据模型变更、依赖变更、破坏性修改须先确认再做；不擅自扩大需求。
7. **验证**：任何改动不得新增测试失败；影响面不小于 44 项本轮改动时请并行执行回归 + 独立复审。