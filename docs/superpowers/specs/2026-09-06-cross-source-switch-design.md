# 跨源换源 + LLM Agent 自动制源 设计文档

日期：2026-09-06
状态：待实施计划
说明：本文包含两个相互独立的功能模块——「部分 I：跨源换源（全类型）」与「部分 II：LLM Agent 自动制源」。两部分共享源 schema / 阅读界面 / 源编辑器基础设施，可分别实施与验收。

---

# 部分 I：跨源换源（全类型）

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

---

# 部分 II：LLM Agent 自动制源

## 11. 背景与目标

现有源制作依赖人工：在源编辑器手填各字段，再逐一用「测试搜索 / 测试详情 / 选择器验证」面板试错。目标：**用大模型驱动 Agent 自动完成整个制源闭环**——用户给出站点 URL，Agent 抓取页面、分析结构、生成源配置 JSON、复用现有验证引擎自动验证、失败自动优化重试，成功则添加为可用源。

覆盖全部内容类型（novel / comic / video）。

## 12. 需求决策（已确认）

| 决策点 | 结论 |
|---|---|
| 自动化程度 | 抓取→分析→生成→验证闭环（复用现有引擎） |
| 目标站点 | 用户输入站点 URL + 类型 + 可选类别 |
| 云端接入 | OpenAI 兼容接口（Key + base_url + 模型名） |
| 本地接入 | Ollama；**用户填写模型服务地址** |
| 本地一键启动 | 「启动 Ollama」按钮拉起本地服务 |
| 失败策略 | 自动重试 N=5 轮保底，仍失败转人工并给建议 |
| 绕过能力 | 智能识别（JS渲染/登录/反爬）+ 建议绕过，不自动部署代理池/破解验证码 |
| 密钥安全 | API Key 存 `data/llm_keys.json`（data/ 已 gitignore，不进 git） |
| 提示词 | 内置 `prompts/source_builder.txt` 模板（全类型要点 + schema + 注意事项） |
| 验证能力 | 复用 source_editor 预览内核 / search_one / fetch_detail / 取流 |

## 13. 架构

```
framework/llm.py                    # LLM 客户端适配（本地+云端统一）
  LlmClient(base_url, api_key, model)   # OpenAI 兼容 chat
  OllamaManager()                       # 一键启动/健康探测/模型列表
  LlmKeyStore()                         # data/llm_keys.json 读写（密钥隔离）

framework/source_agent.py           # Agent 制源闭环
  SourceAgent(llm, http, preview, ...)
  make_source(site_url, content_type) -> AgentResult
  # Phase1 探测 → Phase2 生成 → Phase3 验证闭环(N轮) → Phase4 结果

prompts/source_builder.txt          # 内置提示词模板（全类型）
  - novel / comic / video 字段矩阵速查
  - 选择器写作规范 + 反爬注意事项 + URL 占位符用法
  - JSON 输出严格格式约束

gui/pages/settings_page.py          # LLM 设置区块（改动）
  - 云端 API Key / base_url / 模型名
  - 本地 Ollama 地址 / 选模型 / 「启动 Ollama」按钮

gui/components/source_editor.py     # 「🤖 AI 制源」入口 + Agent 对话框（改动/新增）
  AgentDialog                           # 站点URL/类型/类别/模型 + 实时日志
```

## 14. LLM 客户端层（framework/llm.py）

### 14.1 `LlmClient`
- 构造：`LlmClient(base_url, api_key, model)`，兼容 OpenAI Chat Completions 格式
- `chat(system, user, json_mode=False) -> str`：POST `{base_url}/chat/completions`
  - 用项目现有 `HttpClient`（`framework/http.py`）发起，不新增第三方依赖
  - `json_mode=True` 时带 `response_format: {"type": "json_object"}`（部分兼容端点支持）
  - 超时取模型相关设置，失败抛 `LlmError`
- 本地/云端统一走本类：本地 base_url=用户填的 Ollama 地址（如 `http://127.0.0.1:11434/v1`）
- 地址归一：`base_url` 不以 `/v1` 结尾时追加 `/v1`（兼容用户只填端口）；管理接口（OllamaManager 的 `/api/*`）用**未追加**的原始地址

### 14.2 `OllamaManager`
- `start() -> bool`：探测地址端口 → 未响应则启动 `ollama serve`（找到 ollama.exe，找不到提示安装）
- `models() -> list[str]`：GET `{base}/api/tags` 返回已安装模型名列表
- `running() -> bool`：健康探测
- 地址来源：用户填写（`data/llm_keys.json` 的 `local.base_url`），保留默认 `http://127.0.0.1:11434`

### 14.3 `LlmKeyStore`（密钥安全）
- 文件：`data/llm_keys.json`（`data/` 已被 `.gitignore` 排除，**绝不入 git**）
- 结构：
  ```json
  {
    "cloud": { "api_key": "sk-...", "base_url": "...", "model": "..." },
    "local": { "base_url": "http://127.0.0.1:11434", "model": "" }
  }
  ```
- `app_config.json` 里**不存** API Key（该文件被 git 追踪），只存模型名/base_url 等非敏感项
- 读取失败/文件不存在 → 返回空，不阻塞程序
- `data/` 目录不存在时自动创建

## 15. Agent 制源流程（framework/source_agent.py）

```python
@dataclass
class AgentResult:
    ok: bool
    source_id: str = ""          # 成功时已生成的源 id
    draft: dict | None = None    # 失败时保存的草稿 JSON
    attempts: int = 0            # 实际迭代轮数
    logs: list = field(default_factory=list)   # 各阶段日志（抓取/生成/验证）
    suggestions: list = field(default_factory=list)  # 人工介入建议
```

`make_source(site_url, content_type)` 步骤：

- **Phase 1 探测**
  - 抓取列表页/首页，判定页面技术：SSR（HTML 含内容）/ SPA（JS 渲染，body 空壳）/ API（XHR JSON 特征）
  - 空壳/反爬特征 → 标记「可能需要 render / 参考同类站」，转 Phase 2 附建议；后续验证阶段可尝试 playwright 渲染再抓
- **Phase 2 生成**
  - 把真实 HTML 片段（长度截断 + 关键节点结构化）与 schema 摘要 + 类型要点 + 阶段 1 判断喂给 LLM
  - 要求输出**单个 JSON 代码块**（严格字段、占位符 `{page}/{cat}/{keyword}/{id}` 正确使用）
  - LLM 输出 → 解析 JSON（容错代码块标签剥除）→ `SourceConfig.from_dict` 结构校验
- **Phase 3 验证闭环（N=5 轮）**
  - 每轮按类型跑验证链（见 §16），失败信息 + 实际 HTML 片段回喂 LLM 要求修正
  - 轮间给 LLM 提供「上次验证错误 + 建议方向」，修正后重新生成/局部修正
  - 达到 N 轮仍失败 → 保存草稿，输出建议
- **Phase 4 结果**
  - 成功 → 写入 `sources/{id}.json` → 通知 `SourceManager.add()` 刷新 → UI 提示已在源编辑器加载
  - 失败 → 草稿保存到 `data/agent_drafts/{id}.json` + 完整日志 + 建议清单

## 16. 验证链（每类型）

| 类型 | 验证步骤（全部通过才算成功） |
|---|---|
| 共有 | `SourceConfig.from_dict` 结构校验 → 发现列表（若配置了 discovery）提取 ≥1 条 → 搜索 `search_one(keyword)` ≥1 条 → 详情 `fetch_detail(url)` 元数据非空 |
| novel | 章节目录 `fetch_chapters` ≥1 章 → 正文 `fetch_body` 非空 |
| comic | 分页/图片列表 ≥1 图 → 图 URL 可请求（HEAD/小范围 GET） |
| video | 分集列表 ≥1 集 → 通过 `Content` 统一入口取流（视频播放地址提取，内部按源配置走 HTML 直链 / 接口 / yt-dlp）→ 拿到 ≥1 条可播放 URL |
| 特殊 | 站点需渲染 → 复用 `playwright_helper` 渲染后抓取再验证 |

每步失败 → 记入 `logs` → 进入下一轮优化。验证使用**有限数量**（搜索 1 页、详情 1 个、图片 1 张、流 1 条），避免打满站点配额。

## 17. 源制作界面（GUI）

### 17.1 设置页 LLM 区块（gui/pages/settings_page.py）
- 「云端模型」组：API Key（QLineEdit，`EchoMode.Password`）、base_url、模型名
- 「本地模型」组：Ollama 地址、「获取模型列表」下拉、「🤖 启动 Ollama」按钮 + 状态灯
- 保存 → `LlmKeyStore.save()` 写 `data/llm_keys.json`（不碰 app_config.json 的 Key）

### 17.2 源编辑器 AI 入口（gui/components/source_editor.py）
- 顶部按钮区新增「🤖 AI 制源」→ 弹 `AgentDialog`
- `AgentDialog`：
  - 输入：站点 URL、内容类型（下拉 novel/comic/video）、可选类别、模型选择（云端/本地）
  - 实时日志面板（追加式 QPlainTextEdit）：每个 Phase 输出（抓取 URL、LLM 生成摘要、每轮验证结果/错误）
  - 按钮：开始 / 停止
  - 成功 → 关闭并**自动加载**进源编辑器表单（复用现有 `_load_config` 路径）+ 提示可预览验证
  - 失败 → 显示建议清单 + 「保存草稿」，用户可继续在表单手调

## 18. 提示词模板（prompts/source_builder.txt）

内置（全类型都要）内容结构：
1. **角色设定**：你是专业爬虫配置工程师，产出严格符合项目 schema 的源配置
2. **类型字段矩阵**：novel→`content.chapter`、comic→`content.page`、video→`content.episode`+`media`；附各类型必填/选填速查
3. **选择器写作规范**：
   - 优先 CSS；取属性用 `attr`；多候选结构写 `fallback` 数组（按命中率排序）
   - 避免选择器过宽（命中无关元素）或过窄（依赖单一 class，站点小改即碎）
   - 长文本选 `text`，链接取 `href`，图片取 `src`/`data-original`
4. **URL / 占位符用法**：分页 `{page}`、分类 `{cat}`、搜索 `{keyword}`、详情/分集 `{id}`
5. **反爬规避注意事项**：
   - transports 配 UA / Referer / Accept-Language / timeout / retries / interval_ms / charset
   - 站点有 JS 渲染 → `render` 配置；接口加密 → `decryption`；广告 → `ad_block`
   - 识别需登录 → `auth.login_required`，不强行绕登录
   - 识别网站是 MacCMS / Meipui / 其他已知 CMS → 提示可参考同类源配置模式（source_switch 多线路）
6. **输出格式强约束**：只输出单个 ```json 代码块；`$id` 小写蛇形唯一；必填字段齐备；禁止虚构不存在的字段
7. **验证失败回喂格式**：每轮附带「错误信息 + 相关 HTML 片段」，要求给出修正后的完整 JSON

## 19. 错误处理

| 场景 | 处理 |
|---|---|
| LLM 无配置（Key/地址缺失） | 弹设置引导，跳转设置页 LLM 区块 |
| Ollama 未运行 | 提示并尝试启动；启动失败引导安装 |
| LLM 返回非 JSON / 字段缺失 | 重试解析，连续失败计入迭代 |
| 站点反爬/CF | 记录识别特征 → 建议（可用 playwright 渲染选项） |
| 某验证步骤失败 | 回喂 LLM 优化重试（最多 N 轮） |
| 全类型验证通过但想要的部分缺失 | 源仍保存（可用），建议补充 |

## 20. 测试计划

- `tests/test_llm.py`：
  - `LlmClient`：mock HttpClient 的请求/响应解析、错误、json_mode、超时
  - `OllamaManager`：端口探测、models 解析、启动调用（mock）
  - `LlmKeyStore`：读写、文件缺失、**确认写 data/ 且不碰 app_config.json**
- `tests/test_source_agent.py`：
  - 全流程 mock LLM + fixture 站点：Phase 生成、验证链调用、成功路径
  - N 轮重试：连续失败→草稿+建议
  - 各类型验证链分支（novel/comic/video）
- GUI 冒烟：
  - 设置页 LLM 区块显示/保存
  - AgentDialog 打开/日志追加
- 回归：`pytest tests`（106 基线）

## 21. 范围裁剪（YAGNI）

- 不自动部署代理池 / 验证码破解（仅识别 + 建议）
- 不做 Agent 自主找站
- 不做云端厂商原生 SDK（统一 OpenAI 兼容）
- 不无限重试（N=5 保底转人工）
- 播放流验证仅到「可取流 URL」，不真播完整视频
- API Key 不写入源 JSON / app_config.json / 任何 git 跟踪文件

## 22. 验收标准

1. 设置页可配置云端（Key/base_url/model）与本地（地址/模型），Key 存 `data/llm_keys.json` 不回显明文且不入 git
2. 「启动 Ollama」按钮可拉起本地模型服务，列表可读模型
3. 源编辑器「🤖 AI 制源」输入 URL+类型，Agent 自动抓取→生成→验证，成功后源出现在源列表且可在源编辑器预览面板搜索/详情验证
4. 验证失败自动重试最多 5 轮；仍失败保存草稿 + 日志 + 建议
5. 提示词模板覆盖三种类型要点与制源注意事项
6. 现有 106 测试全绿，新增 llm / source_agent 测试通过