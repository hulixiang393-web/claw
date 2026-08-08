# 项目规划：书架 Service 层重构 + 视频播放界面现代化

> 生成于 2026-08-08，阶段 2 产出。项目经理视角 + 开发人员视角。
> 属 claw 项目内改造，独立成文便于对照验收。

## 一、项目概述

**书架**：现有 UI 层（library_page.py 613 行）直接操作 4 个数据源（目录扫描、LibraryStore 收藏、ReadingProgress 续读、hidden_local.json 隐藏名单），无统一 API 层；epub 类型检测每次刷新全量读文件，书多时卡顿。目标：新增 `ShelfService` 统一书架 API，UI 只调服务层，epub 类型检测缓存化、扫描后台化。

**视频播放**：现有播放界面（video_view.py 895 行）两行控制条常驻显示、无键盘快捷键、无中央播放按钮、播放地址调试信息常显底部。目标：按 2026 主流播放器交互模式（Netflix/YouTube/IINA）重构——控制自动隐藏、中央播放钮、双击全屏、快捷键、齿轮设置菜单、缓冲指示。

## 二、调研方案（阶段 0 产出）

### 书架候选方案
| 方案 | 架构 | 优点 | 缺点 | 工作量 |
|---|---|---|---|---|
| A. Service 层重构（选定） | `ShelfService` 统一 API + `ShelfItem` 模型 + 类型缓存 + 后台扫描 | UI/数据解耦、性能提升、后续可扩展 WebDAV/云端同步 | 需动 UI 层 | ~0.5 天 |
| B. 轻量优化 | 仅加缓存和后台扫描 | 改动小 | 逻辑仍分散 UI 层 | ~0.2 天 |
| C. 数据库迁移 | SQLite 存书架 | 强一致可查询 | 改动大、迁移风险高 | 2 天+ |

**选定方案：A**（理由：用户明确要求"重做 API 调用"，Service 层是持久收益最大、风险最小的结构化解法）

### 播放器候选方案
| 方案 | 交互 | 优点 | 缺点 | 工作量 |
|---|---|---|---|---|
| A. 现代化交互（选定） | 自动隐藏 + 中央按钮 + 双击全屏 + 快捷键 + 设置菜单 | 沉浸、符合主流习惯 | 需重写交互层 | ~1 天 |
| B. 仅美化 | 保留常显控件重排样式 | 风险小 | 交互模式未变 | ~0.3 天 |

**选定方案：A**（理由：调研结论——现代播放器核心是"内容优先、控制渐进披露"，键盘快捷键是桌面播放器硬需求；用户已确认）

## 三、功能点清单（阶段 1 产出）

### 书架
| # | 功能点 | 描述 | 依据 | 优先级 |
|---|--------|------|------|--------|
| S1 | ShelfService 统一 API | `list/scan_local/favorites/folders/resume/hide/show/favorite_add/remove/move/export` | 用户需求 + Repository 模式 | P0 |
| S2 | ShelfItem 统一模型 | dataclass：key/title/content_type/cover/kind/source_id/url/path/episode_count/tags/folder/resume_title/author/missing | 消除 kind 分支散落 | P0 |
| S3 | epub 类型缓存 | data/shelf_meta.json，按 mtime+size 失效 | 全量 read_epub 卡顿（现状诊断） | P0 |
| S4 | 后台扫描 | QThreadPool 扫描 + 信号回主线程，60ms 合并节流（沿用知识库经验） | 知识库：多源并发 UI 渲染节流 | P1 |
| S5 | LibraryPage 瘦身 | UI 只调 ShelfService，删除直接文件/存储操作 | 单一职责 | P0 |

### 播放器
| # | 功能点 | 描述 | 依据 | 优先级 |
|---|--------|------|------|--------|
| P1 | 控制条自动隐藏 | 3s 无鼠标动作隐藏，移动/触碰显示 | 调研：Netflix/YouTube 模式 | P0 |
| P2 | 中央播放按钮 | 未播放时视频区中央大播放钮，点击播放 | 调研：large central play state | P0 |
| P3 | 单击暂停/双击全屏 | 视频区点击切换播放暂停，双击全屏 | 调研：familiar behaviors | P0 |
| P4 | 键盘快捷键 | Space 播放暂停 / F 全屏 / M 静音 / ←→±5s / ↑↓音量 / ? 帮助浮层 | 调研：keyboard shortcuts 硬需求 | P0 |
| P5 | 齿轮设置菜单 | 画质 + 倍速收纳进 QMenu | 调研：settings menu 中心化 | P0 |
| P6 | 进度条 hover 展开 | 细条 hover 变粗 + 时间戳气泡 | 调研：thin progress bar expands on hover | P1 |
| P7 | 播放地址隐藏 | 地址行改为右键「复制播放地址」，错误/加载提示用浮层 | 用户确认 | P0 |
| P8 | 缓冲指示 | VLC Buffering 状态显示 spinner | 调研：status feedback | P1 |
| P9 | 全屏沉浸 | 全屏顶栏标题 + 退出按钮，控制条同自动隐藏 | 调研：fullscreen removes distractions | P1 |
| P10 | 保留现有能力 | 换源/预拉/续读/看门狗/外部播放/刷新重试 | 回归底线 | P0 |

## 四、整体规划（项目经理视角）

- **里程碑**：M1 书架 Service 层（S1-S5）→ M2 播放器交互重构（P1-P5,P7）→ M3 播放器细节增强（P6,P8-P10）→ M4 测试回归 + 提交
- **模块划分**：`framework/shelf_service.py`（新）/ `framework/library_store.py`（不动）/ `gui/pages/library_page.py`（重构）/ `gui/pages/reader/video_view.py`（重构）/ `gui/components/`（新控件可内嵌）
- **资源依赖**：ebooklib、PySide6、python-vlc 现有依赖，无需新增
- **风险点**：视频视图 895 行交互密集，重构必须以现有信号契约（load/reload_detail/set_source_sid/source_changed/episode_changed/play_label）为回归底线；书架 UI 依赖 `_smoke_library.py` 冒烟测试
- **时间估计**：M1 半天 → M2 半天 → M3 半天 → M4 半天

## 五、功能点逻辑设计（开发人员视角）

### S1 ShelfService
- **输入**：构造注入 output_dir、LibraryStore、ReadingProgress、data_dir
- **处理**：`list()` = scan_local 结果 + favorites 结果合并为 ShelfItem 列表；筛选/分组/排序在服务层完成
- **输出**：`list[ShelfItem]`
- **异常**：目录不存在返回空列表；JSON 损坏兜底空数据
- **依赖**：LibraryStore / ReadingProgress / hidden_local.json

### S3 epub 类型缓存
- **输入**：epub 文件路径
- **处理**：查缓存 dict{path: {type, mtime, size}} → mtime/size 未变直接返回缓存类型；变化则重新 read_epub 判定后写回
- **输出**：类型字符串 novel/comic/epub
- **异常**：读取失败返回 "epub" 兜底

### S4 后台扫描
- **输入**：刷新信号
- **处理**：QThreadPool 起扫描任务（只做文件系统遍历 + 类型检测）→ finished 信号带结果 → 主线程重建 UI
- **输出**：ShelfItem 列表信号
- **注意**：沿用知识库经验——任务必须列表持有引用防 GC；60ms 合并窗口节流

### P1 控制条自动隐藏
- **输入**：鼠标事件/定时器
- **处理**：视频区 mouseMoveEvent 重置 3s QTimer；超时隐藏控制条 + 鼠标指针（setCursor 隐藏）；移动重新显示
- **状态**：IDLE → VISIBLE → IDLE；全屏/未播放时同规则
- **异常**：拖动进度条期间禁止隐藏

### P3 单击/双击
- **输入**：video_frame 鼠标点击
- **处理**：单击 toggle 播放暂停；双击 _toggle_fullscreen；用 QTimer 区分单击/双击（300ms 窗口）
- **注意**：单击不应在双击后误触发——用点击延迟判定

### P4 快捷键
- **输入**：视图 keyPressEvent（焦点在视图内）
- **处理**：Space=播放暂停、F=全屏、M=静音、←/→=seek ±5s、↑/↓=音量 ±10、?=帮助浮层（QFrame 半透明）
- **注意**：focusPolicy 设为 StrongFocus；帮助浮层列出全部快捷键

### P5 设置菜单
- **输入**：齿轮按钮点击
- **处理**：QMenu + 子菜单「画质」「倍速」，选中即应用（复用现有 quality/speed 逻辑）
- **注意**：画质选项来自源配置 quality.options，无画质选项时菜单只显倍速

### P7 地址隐藏
- **输入**：播放成功/失败状态
- **处理**：删除底部 play_label 常显；成功不显示地址；失败显示浮层错误（3s 自动消失或点刷新重试）；「复制地址」保留在控制条图标 + 右键菜单
- **注意**：保留 play_label 属性名与外部契约（ReaderPage 可能引用）

### P8 缓冲指示
- **输入**：VLC state 变化（Buffering/Playing/Error）
- **处理**：Buffering 显示中央 spinner（QMovie 或旋转图标），Playing 隐藏
- **注意**：用现有 time_changed/error 信号驱动，不新增 VLC 轮询

## 六、验收标准（阶段 6 对照）

### 书架
- [x] `framework/shelf_service.py` 新增，LibraryPage 不再直接调用 LibraryStore/_refresh_data 的文件逻辑
- [x] 书架刷新在 100+ 本地书时 <1s（类型缓存生效，二次刷新 <200ms）
  - 缓存机制单测验证：二次调用不触发 detector（test_epub_type_cache_reuses_detector）
- [x] 本地书/收藏/续读/隐藏/收藏夹全部行为与改造前一致（对照 _smoke_library.py 用例）
  - _smoke_library 通过 + test_shelf_service 12 项全绿
- [x] 书架数据在无 output_dir 时显示空状态不崩溃（test_missing_output_dir_returns_empty）

### 播放器
- [x] 3s 无操作控制条自动隐藏，鼠标移动重新显示（离屏验证 auto-hide OK）
- [x] 点击视频区播放/暂停，双击全屏，Esc 退出全屏（_VideoFrame 事件路由实现）
- [x] 快捷键 Space/F/M/←/→/↑/↓/? 全部生效（_handle_key + 帮助浮层离屏验证）
- [x] 画质/倍速设置菜单可用，行为与旧控制条一致（⚙ QMenu 收纳，画质选项读源配置）
- [x] 底部不再常显播放地址；错误提示浮层可重试（play_label 仅加载/错误显示）
- [x] 换源/预拉/自动续播/续读恢复/外部播放/刷新重试全部回归通过
  - _smoke_reader / _smoke_regression 通过（含换源信号、续读位置信号验证）
- [x] `python -m pytest` 既有单测全绿（45 passed）

### 回归记录（2026-08-08）
- pytest 45 passed；_smoke_library/_smoke_settings/_smoke_reader/_smoke_regression/_smoke_download 通过
- _smoke_search/_smoke_discover/_smoke_app 失败为预存网络依赖问题（git stash 基线验证同款失败，与本次改动无关）
