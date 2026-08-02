# 多源爬虫框架 — 架构设计文档

> 版本：v1.0　·　日期：2026-08-01　·　状态：设计定稿

## 目录

1. [设计目标与原则](#1-设计目标与原则)
2. [总体架构](#2-总体架构)
3. [目录结构](#3-目录结构)
4. [核心模块职责说明](#4-核心模块职责说明)
5. [JSON 源配置规范（字段标准）](#5-json-源配置规范字段标准)
   - 5.9 [应用级设置（全局配置）](#59-应用级设置全局配置)
6. [源配置制作指南（手把手）](#6-源配置制作指南手把手)
7. [数据流与关键时序](#7-数据流与关键时序)
8. [异常处理与失效源隔离](#8-异常处理与失效源隔离)
9. [约束与边界（框架绝不做什么）](#9-约束与边界框架绝不做什么)
10. [测试方案](#10-测试方案)
11. [验收标准](#11-验收标准)
12. [主题与画风规范](#12-主题与画风规范)
13. [UI 与 Framework 解耦模式](#13-ui-与-framework-解耦模式)
14. [源编辑器交互协议](#14-源编辑器交互协议)
15. [验收标准（GUI 维度补充）](#15-验收标准gui-维度补充)

---

## 1. 设计目标与原则

| 原则 | 含义 | 落点 |
|---|---|---|
| **配置驱动** | 站点规则全部外置到 JSON，框架不写死任何站点的选择器、接口、解密逻辑 | `sources/*.json` 与 `framework/` 完全解耦 |
| **单一职责** | 每个模块只干一件事，杜绝"全家桶"函数 | 模块 = 类，类 = 单文件 |
| **高内聚低耦合** | 调度只调度、解析只解析、展示只展示，模块间通过明确的调用关系连接 | 依赖方向严格单向（见 §7 图） |
| **可移植** | 框架本体不依赖任何第三方库，仅用标准库 + 框架自带的统一配置校验 | 运行时零强制依赖 |
| **鲁棒** | 单源失败不影响其他源；页面改版不崩溃只告警；资源始终被回收 | 异常统一包装为 `SourceError` |

> 一句话：**"改站点 = 改 JSON，改框架 = 改 Python。"** 新增站点永远不需要碰框架代码。

---

## 2. 总体架构

```
┌───────────────────────────────────────────────────────────────┐
│                         CLI 入口 (__main__)                   │
└──────────────────────────────┬────────────────────────────────┘
                               │
┌──────────────────────────────▼────────────────────────────────┐
│                     Scheduler 调度中枢                        │
│  流程编排 · 限制翻页/条数 · 生命周期 · 日志 · 错误隔离         │
└──┬──────────┬──────────┬──────────┬──────────┬───────────────┘
   │          │          │          │          │
   ▼          ▼          ▼          ▼          ▼
[SourceManager] [Discovery]  [Search]   [Content]  [Render/Download]
   │          │          │          │          │
   ▼          ▼          ▼          ▼          ▼
[Config校验] [HttpClient] [Parser] [SelfCheck] [Decrypter]  ── 基础设施层
   │            │
   ▼            ▼
sources/*.json  requests(或外部)
```

- **驱动层（上）**：调度、编排、限制、展示。
- **基础设施层（下）**：HTTP 会话、页面解析（只提供能力，规则由配置给出）、自检、解密调用。
- **配置层（外）**：`sources/*.json` 是唯一的"站点知识"来源。

### 模块依赖方向（只许向下一层依赖，禁止反向）

```
scheduler
  ├── source_manager ──→ config
  ├── discovery       ──→ http · parser · selfcheck · config
  ├── search          ──→ http · parser · selfcheck · config
  ├── content         ──→ http · parser · selfcheck · decrypter · config
  ├── render          ──→（无）
  └── downloader      ──→ http · config
```

---

## 3. 目录结构

```
d:\code\
├── README.md                      # 项目说明 + 快速开始
├── requirements.txt               # 运行时零依赖；测试/示例需要 requests/lxml/pytest
app_config.example.json         # 应用级设置示例（首次运行自动生成 app_config.json）
├── docs\
│   └── architecture.md            # 本文档
├── framework\
│   ├── __init__.py                # 版本号
│   ├── __main__.py                # CLI 入口（python -m framework ...）
│   ├── errors.py                  # 异常体系（SourceError / ConfigError / StructureChanged / ContentMissing ...）
│   ├── config.py                  # 源配置加载 + 结构化 + 校验（Pydantic）
│   ├── source_manager.py          # 源加载模块（发现/分类/搜索/爬取共用的配置仓库）
│   ├── http.py                    # HttpClient：会话、UA、请求间隔、代理、超时、重试
│   ├── parser.py                  # Parser：CSS/XPath 选择器求值、文本提取、属性提取、URL 拼接
│   ├── selfcheck.py               # 结构自检模块：爬前 check_selector 校验
│   ├── decrypter.py               # 解密接口客户端：按配置调用外置 API
│   ├── discovery.py               # 发现&分类模块
│   ├── search.py                  # 多源搜索模块（单源 / 全域同类型）
│   ├── content.py                 # 内容爬取模块（小说章节/漫画图集/视频分集 + 翻页）
│   ├── render.py                  # 结果渲染展示模块
│   ├── downloader.py              # 下载调度模块（按内容类型分发）
│   └── scheduler.py               # 调度中枢（流程编排 + 限制 + 日志 + 错误隔离）
├── sources\
│   ├── template.example.json      # 完整字段模板（复制改名即可新建源）
│   ├── demo_novel.json            # 小说示例源
│   ├── demo_comic.json            # 漫画示例源
│   └── demo_video.json            # 视频示例源
└── tests\
    ├── __init__.py
    ├── conftest.py                # 公共 fixture：本地回环 HTTP 站点 + 临时源目录
    ├── fixtures\
    │   ├── fixture_server.py      # 用标准库构造的测试站点（首页/搜索/详情/正文/图片/分集）
    │   ├── site_novel.py          # 小说站页面生成器
    │   ├── site_comic.py          # 漫画站页面生成器
    │   └── site_video.py          # 视频站页面生成器
    ├── test_config.py             # 配置校验测试
    ├── test_search.py             # 多源搜索测试（边界/异常/合并）
    ├── test_content.py            # 内容爬取 + 翻页测试（边界/异常）
    ├── test_exceptions.py         # 失效源隔离 / 自检告警 / 解密失败测试
    └── test_closed_loop.py        # 功能闭环测试（加载→分类→搜索→详情→翻页→下载）
```

---

## 4. 核心模块职责说明

### 4.1 源加载模块 `source_manager.py` — `SourceManager`

- 扫描指定目录下所有 `*.json`，逐个加载校验，**一个坏文件只跳过该文件并告警，不影响其他源**。
- 按 `content_type`（novel / comic / video）分组管理，提供索引：
  - `get(source_id)` — 按 ID 取单个源
  - `by_type(content_type)` — 取某类型全部源
  - `all()` / `types()` — 枚举
- 内置去重：同一 `source_id` 重复加载时后者覆盖并告警。

### 4.2 发现&分类模块 `discovery.py` — `Discovery`

- 读取源内 `category_config`，请求分类入口，按配置的分类名/地址选择器提取全站分类列表。
- 输出 `[Category(title, url)]`，title 展示、url 是列表页地址。
- 入口解析失败（选择器失效/结构变更）抛 `StructureChanged`，由调度层转为用户可读提示。

### 4.3 搜索模块 `search.py` — `Search`

- **单源搜索** `search_one(source_id, keyword)`：按 `search_config` 构造请求 → 解析结果列表 → 统一 `SearchResult` 对象。
- **全域同类型搜索** `search_type(content_type, keyword)`：把该类型下全部源并行/串行跑一遍，结果合并。
- **搜索结果统一格式化**：每条结果固定字段 `{title, cover, source_id, source_name, url, update_info}`。
- **限制**：`max_pages` / `max_results` 双上限，一旦达到立即停止分页，杜绝无限抓取。
- **隔离**：单源搜索抛错时把该源结果标记为失败条目，其余源正常返回。

### 4.4 内容爬取+翻页模块 `content.py` — `ContentFetcher`

按内容类型分派，全部由 `detail_rule` 驱动：

| 类型 | 提取 | 翻页终止条件（默认） |
|---|---|---|
| 小说 | `novel.chapter_list_selector` 提取章节；`novel.content_selector` 提取正文 | 下一页链接不存在 / 已抓满 / 去重命中 |
| 漫画 | `comic.image_selector` 提取分页图片；图集 URL 规则由 `comic.page_selector` 生成 | 无更多页 / 达到 max_pages / 去重 |
| 视频 | `video.play_selector` 提取播放地址；分集由 `video.episode_selector` 遍历 | 无更多分集 / 达到 max_pages |

- 章节/分集级**去重**（按 URL），避免循环翻页。
- 解密逻辑一律走 `decrypter.decrypt()`，**框架绝不内置解密算法**。
- 返回统一的 `ContentResult`，供渲染与下载共用。

### 4.5 展示&下载模块 `render.py` + `downloader.py`

- `render.render_search_results()` — 列表展示：标题、封面、来源、更新信息、详情地址。
- `render.render_content()` — 内容展示：目录 / 当前页正文 / 图片地址列表 / 播放地址。
- `downloader.download()` — 按 `content_type` 分发：
  - novel → 文本保存（UTF-8）
  - comic → 图片保存（按 URL 批量拉取）
  - video → 仅导出播放地址清单（实际下载交给外部下载器，框架不做流式抓流）
  - 支持 `max_items` 限制，防止批量下载失控。

### 4.6 结构自检模块 `selfcheck.py` — `StructureChecker`

- 每次**请求页面之前**用 `check_selector` 做冒烟校验：请求 → 求值 → 存在即通过。
- 校验失败抛 `StructureChanged`，携带站点名与提示「站点结构已变更，请更新源配置」。
- 可在 `Scheduler` 配置 `strict=True/False` 关闭（供调试或测试）。

### 4.7 调度中枢 `scheduler.py` — `Scheduler`

- 串联所有模块；统一日志；统一异常出口（`SourceError` → 用户可读错误，绝不裸崩溃）。
- 持有并发开关（`parallel` 布尔），决定全域搜索串行/并行。
- 是 CLI 唯一直接调用的门面对象。

---

## 5. JSON 源配置规范（字段标准）

> 一级 key 与字段名**固定**。框架按此标准解析；不存在的字段走默认值。
> `selector` 取值可以是 CSS（`"css"`）或 XPath（`"xpath"`），由每个 selector 对象自带的 `type` 字段声明。

### 5.1 顶层字段

```jsonc
{
  // ---- 必填 ----
  "source_id": "demo_novel",        // 唯一标识，全局不得重复
  "source_name": "演示小说站",       // 结果展示用站点名
  "content_type": "novel",          // novel | comic | video
  "base_url": "https://example.com/novel",

  // ---- 必填 ----
  "category_config": { ... },
  "search_config":    { ... },
  "item_rule":        { ... },
  "detail_rule":      { ... },

  // ---- 可选（框架提供默认）----
  "decrypt_api":      null,         // 外置解密接口；null 表示无解密
  "anti_crawl_config": { ... },     // UA/间隔/代理/重试
  "check_selector":   null,         // 结构校验标签；null 表示跳过自检
  "fetch":            { "max_pages": 3, "max_results": 20 }   // 爬取上限
}
```

### 5.2 `category_config` — 发现页/分类规则

```jsonc
"category_config": {
  "category_page_url": "https://example.com/novel/list",  // 分类入口链接
  "category_name_selector": {
    "type": "css",
    "value": ".category-list a"
  },
  "category_url_selector": {
    "type": "css",
    "value": ".category-list a",
    "attr": "href"          // 取链接用属性；缺省则取文本
  }
}
```

### 5.3 `search_config` — 搜索规则

```jsonc
"search_config": {
  "search_base_url": "https://example.com/novel/search",   // 拼接模板
  "search_keyword_param": "keyword",                       // 关键词参数名（GET 时有效）
  "search_method": "GET",                                  // GET 或 POST
  "search_headers": { "X-Requested-With": "XMLHttpRequest" },  // 可选附加头
  "search_item_selector": { "type": "css", "value": ".book-item" },   // 列表项根选择器
  "result_cover_selector": { "type": "css", "value": "img", "attr": "src" },
  "result_url_selector":   { "type": "css", "value": "a.title", "attr": "href" },
  "result_title_selector": { "type": "css", "value": "a.title" },
  "result_update_selector":{ "type": "css", "value": ".update" }
}
```

- **POST 情形**：`search_method=POST` 时，用 `search_headers` 里声明的 `Content-Type`（如 `application/x-www-form-urlencoded`）把 `{keyword_param: keyword}` 作为表单体发送。
- **翻页**（可选）：`search_page_param`（如 `"page"`）+ `search_max_pages`，框架从 1 开始递增拼接。

### 5.4 `item_rule` — 列表页提取规则（搜索结果/列表通用）

```jsonc
"item_rule": {
  "title_selector":     { "type": "css", "value": "a.title" },
  "cover_selector":     { "type": "css", "value": "img.cover", "attr": "src" },
  "detail_url_selector":{ "type": "css", "value": "a.title", "attr": "href" },
  "author_selector":    { "type": "css", "value": ".author" },
  "update_selector":    { "type": "css", "value": ".update" },
  "item_root_selector": null   // 列表页复用搜索选择器时置 null
}
```

### 5.5 `detail_rule` — 详情页 & 内容规则（按 content_type 分型）

**小说 novel**
```jsonc
"detail_rule": {
  "novel": {
    "chapter_list_selector": { "type": "css", "value": ".chapter-list a" },
    "chapter_url_selector":  { "type": "css", "value": ".chapter-list a", "attr": "href" },
    "content_selector":      { "type": "css", "value": "#content" },
    "prev_page_selector":    null,      // 单页无分页时置 null
    "next_page_selector":    { "type": "css", "value": "a.next" },
    "page_url_selector":     { "type": "css", "value": "a.next", "attr": "href" }
  }
}
```

**漫画 comic**
```jsonc
"detail_rule": {
  "comic": {
    "chapter_list_selector": { "type": "css", "value": ".episode-list a" },
    "chapter_url_selector":  { "type": "css", "value": ".episode-list a", "attr": "href" },
    "image_selector":        { "type": "css", "value": "img.comic-page", "attr": "src" },
    "page_selector":         { "type": "css", "value": "a.next-page", "attr": "href" }
  }
}
```

**视频 video**
```jsonc
"detail_rule": {
  "video": {
    "play_url_selector":   { "type": "css", "value": "video source", "attr": "src" },
    "play_url_attr":       null,          // 覆盖时按属性取
    "episode_list_selector": { "type": "css", "value": ".episode-list a" },
    "episode_url_selector":  { "type": "css", "value": ".episode-list a", "attr": "href" }
  }
}
```

### 5.6 `decrypt_api` — 外置解密接口

```jsonc
"decrypt_api": {
  "url": "https://api.example.com/decrypt",   // 必填
  "method": "POST",                            // 可选，默认 POST
  "payload_key": "data",                       // 可选，请求体字段名，默认 data
  "result_key": "result",                      // 可选，响应体字段名，默认 result
  "timeout": 10                                // 可选，秒
}
```

调用契约：框架把**待解密内容（如加密封包 / 混淆后的播放地址）**放在 `{payload_key: <content>}` 里发出，响应体取 `result_key` 字段作为解密结果。解密算法、密钥、逻辑完全在接口侧，**框架只传内容、收结果**。

### 5.7 `anti_crawl_config` — 反爬基础配置

```jsonc
"anti_crawl_config": {
  "headers": { "User-Agent": "Mozilla/5.0 (...)" },   // 可选；未配则用默认 UA
  "request_interval": 0.5,        // 秒；两次请求最小间隔（配合 sleep）
  "proxy": null,                  // 代理标记；字符串 URL 或 null
  "retry_times": 3,               // 请求重试次数
  "timeout": 10                   // 请求超时，秒
}
```

> 尊重站点：请勿调小间隔对目标站点构成压力；配置仅供合法爬取使用。

### 5.8 `check_selector` — 结构校验标签

```jsonc
"check_selector": { "type": "css", "value": ".site-header" }
```
每次爬取前的冒烟校验都用它。站点改版导致该元素消失 → 抛「站点结构已变更」提示，不崩溃。

---


### 5.9 应用级设置（全局配置）

> **站点规则**放在 sources/*.json，是"每个站点一份"；
> **应用级设置**放在 `app_config.json`（项目根目录），是"这台客户端的配置"——所有站点共享的默认值、UI 偏好、下载路径、日志行为都在这里。

两者的优先级：

`
sources/*.json (单站配置)  >  app_config.json (全局默认)  >  内置默认
`

反过来说：每源 `anti_crawl_config` 字段如果显式配置了，会覆盖全局；空字段才继承全局。

#### 字段一览

`json
{
  "schema_version": 1,
  "network": {
    "default_user_agent":       "Mozilla/5.0 ...",
    "default_timeout":          10,
    "default_retries":          3,
    "default_request_interval": 0.0,
    "proxy":                    null,
    "concurrent_search_sources": 4,
    "max_bytes_per_image":      5242880
  },
  "ui": {
    "theme":                 "sakura",   // sakura | mint | midnight
    "font_scale":            1.0,
    "cover_cache_size_mb":   256,
    "default_page_size":     20
  },
  "download": {
    "output_dir":                       "downloads",
    "naming_template":                  "{title}_{chapter_no}_{chapter_title}",
    "merge_chapters_into_one_file":     false,
    "skip_existing":                    true,
    "max_concurrent_downloads":         4
  },
  "library": {
    "shelf_export_dir": "library",
    "webdav_url":       null
  },
  "diagnostics": {
    "verbose_logging":   false,
    "log_dir":           "logs",
    "log_file_pattern":  "framework-{date}.jsonl"
  },
  "sources_runtime": {
    "broken_source_warn_interval_hours": 24,
    "auto_disable_after_failures":       3,
    "selfcheck_strategy":                "soft"   // soft | strict | off
  }
}
`

#### 加载与回退

- 启动时 ramework/settings_manager.py 读取 `app_config.json`；不存在则用内置默认并写一份出来。
- 坏 JSON / 字段类型错误 → ConfigError，但**单字段错误不会让整个启动失败**——坏字段直接回退到默认，并加 WARNING。
- schema_version 与当前 SCHEMA_VERSION 不一致时记录在 warnings 列表，不阻塞。

#### 字段约束（设计意图）

| 字段组 | 约束 |
|---|---|
| 
etwork.* | 数值字段接受 0 / 正数；proxy 接受 http://user:pass@host:port 形式 |
| ui.theme | 枚举 sakura / mint / midnight，未知值回退 sakura |
| ui.font_scale | 0.75 ~ 1.5，超出范围截断 |
| download.naming_template | 模板变量：{title} {chapter_no} {chapter_title} {source_id}；非法字符自动替换为 _ |
| diagnostics.log_file_pattern | 支持 {date} 占位符（YYYY-MM-DD） |
| sources_runtime.selfcheck_strategy | strict 失败抛 StructureChangedError；soft 仅 WARN；off 跳过 |

#### 与源配置的关系

`
# sources/foo.json
{
  "anti_crawl_config": {
    "headers":           { "User-Agent": "Foo/1.0" },   // 显式 → 优先
    "timeout":           5,                              // 显式 → 优先
    "request_interval":  null                            // 空 → 走 app_config.json
  }
}
`

实现策略：HttpClient 取值时（计划中）改为 source.anti_crawl_config.field or settings.get('network.default_xxx')。当前版本先完成骨架与配置加载，**取值合并**在 Phase 2 实现。

## 6. 源配置制作指南（手把手）

### 6.1 新增一个站点的步骤（5 步，全程不改框架代码）

1. **打开模板**：复制 `sources/template.example.json` 为 `sources/我的站点.json`（文件名随意，`source_id` 才是唯一标识）。
2. **填身份信息**：`source_id`（唯一）、`source_name`、`content_type`、`base_url`。
3. **写发现规则**：在浏览器按 F12，用元素选择器定位分类入口的容器，回填 `category_config`。
4. **写搜索/列表规则**：打开站内搜索页搜一个关键词，定位结果列表项，回填 `search_config` 与 `item_rule`；注意 `<img>` 用 `attr: "src"` 取封面、`<a>` 用 `attr: "href"` 取链接。
5. **写详情规则**：打开一个详情页，按内容类型回填 `detail_rule`；有加密/混淆时配 `decrypt_api`。

> 自检清单：改完 JSON 后运行 `python -m framework check --source <id>`（或直接跑 `--selftest`），框架会做配置校验 + 结构自检冒烟，一次性暴露选择器写错或结构已变。

### 6.2 选择器写法规则

- 每个选择器是 `{"type": "css"|"xpath", "value": "..."}`，文本提取时用 `value`，取链接/图片时加 `"attr": "src"|"href"`。
- CSS 示例：`"value": ".chapter-list a"`；XPath 示例：`"type": "xpath", "value": "//div[@id='content']/text()"`。
- 相对链接自动拼接：取到 `href="/book/1"` 且 `base_url="https://a.com"` → 自动转为 `https://a.com/book/1`；已含 `http` 原样保留。

### 6.3 常见坑与排查

| 现象 | 原因 | 排查 |
|---|---|---|
| 搜索无结果 | 搜索接口不是 GET、或结果项选择器不准 | F12 看实际返回，先手动 `curl` 拼一次 URL |
| 封面空 | `<img>` 用了文本选择器没加 `attr` | 给封面选择器补 `"attr": "src"` |
| 翻页停不下来/提前停 | 下一页选择器匹配到自身（循环） | 用去重日志看 URL 是否重复，收紧 `next_page_selector` |
| 报「站点结构已变更」 | `check_selector` 指向的元素消失 | 打开页面确认后更新 `check_selector` |
| 正文是乱码/加密 | 站点有解密逻辑 | 单独做解密 API，`decrypt_api` 指向它，不写进框架 |

### 6.4 解密 API 的制作建议（外置的理由）

- 把「解密某个站点内容的函数」做成一个独立 HTTP 服务（任意语言），框架只发 `POST {data: "密文"}`，收 `{result: "明文"}`。
- 好处：新站点只要在 JSON 里改 `decrypt_api.url`；解密升级不触碰框架；密钥不进爬虫代码。
- 测试期可直接用 `sources/demo_*.json` 中 `decrypt_api` 指向的本地回环接口验证全链路。

---

## 7. 数据流与关键时序

### 7.1 搜索闭环时序

```
Scheduler.search_type(content_type, keyword)
  └─ for each source in manager.by_type(content_type)
       ├─ SelfCheck.check(source, url)          # 结构校验（失败 → 隔离+告警）
       ├─ Search.search_one(source, keyword)
       │    ├─ HttpClient.get(拼接 search_base_url + keyword_param)
       │    ├─ Parser.parse_items(html, search_config, item_rule)
       │    └─ 翻页直到 max_pages / max_results
       └─ 合并本源结果（失败源标 failed）
  └─ 全部结果统一 SearchResultList → render 展示
```

### 7.2 内容闭环时序

```
Scheduler.fetch_content(source, url, mode)
  ├─ SelfCheck.check(source, url)
  ├─ ContentFetcher 按 content_type 分派
  │    ├─ novel  : 章节列表 → 逐个正文（next 翻页）→ 去重 → ContentResult
  │    ├─ comic  : 章节 → 每页 image_selector 抓图 → page_selector 翻页 → 去重
  │    └─ video  : 分集 → play_url_selector 取播放地址（解密走 decrypter）→ 遍历
  └─ render.render_content()  /  downloader.download()
```

### 7.3 依赖关系（单向，禁止反向引用）

```
scheduler → source_manager → config
         → discovery → http · parser · selfcheck
         → search    → http · parser · selfcheck
         → content   → http · parser · selfcheck · decrypter
         → render    →（无）
         → downloader→ http
```

---

## 8. 异常处理与失效源隔离

### 8.1 异常体系（`framework/errors.py`）

| 异常 | 触发场景 | 对用户提示 |
|---|---|---|
| `SourceError` | 所有爬虫异常基类 | — |
| `ConfigError` | JSON 缺字段 / 类型错 / source_id 冲突 | 「源配置错误：<细节>」 |
| `SourceNotFoundError` | 查不存在的 source_id / content_type 无源 | 「未找到数据源：<id>」 |
| `StructureChangedError` | `check_selector` 校验失败 | 「站点结构已变更，请更新源配置」 |
| `ContentMissingError` | 详情页提取不到内容（章节/图片/播放地址） | 「未解析到内容，请检查源配置或站点是否改版」 |
| `RequestError` | 网络/超时/重试耗尽 | 「请求失败：<url>」 |
| `DecryptError` | 解密接口不可达 / 返回异常 | 「解密接口调用失败」 |

### 8.2 失效源隔离策略

- 调度层对每个源 `try/except SourceError`，**失败源的错误记入日志与结果集的 failed 条目，其余源继续**。
- `search_type` 返回 `SearchReport`：`{results, errors, total, sources}`，前端/CLI 可单独展示失败源。
- `config` 加载坏 JSON 时**跳过该文件**并告警，不中断整个 SourceManager 初始化。

### 8.3 资源与礼貌性爬取

- 所有 `HttpClient` 请求共享会话，`requests.Session` 自动复用连接。
- 请求间隔由 `anti_crawl_config.request_interval` 控制，两个请求之间 `time.sleep()`（测试模式下用可注入的 `sleeper` 替换，见 §10）。
- 超时 + 有限重试，避免卡死；页面条数与翻页数均有硬上限。

---

## 9. 约束与边界（框架绝不做什么）

| 约束 | 说明 |
|---|---|
| 不写死站点规则 | 选择器、接口、翻页规则只存在于 JSON；框架代码里 `search("...")` 只出现字段名常量 |
| 不内置解密 | 解密一律外置 `decrypt_api`；框架只有 `Decrypter.decrypt()` 调用壳 |
| 不做视频流下载 | 视频只提取播放地址导出清单，流媒体下载交给外部工具 |
| 不存数据 | 无数据库；结果仅在内存 + 下载到本地文件 |
| 不用第三方爬虫库 | 框架本体标准库自足（CSS/XPath 由配置的可选依赖 `lxml` 支持，未装时自动退回基础文本匹配并告警） |
| 不隐藏失败 | 结构变更、解析失败都显式抛出提示，绝不静默吞错 |

> 合规提醒：本框架仅用于**合法授权**的公开数据爬取。请遵守目标站点 robots 与服务条款、尊重版权，控制请求频率。

---

## 10. 测试方案

> 测试策略：**本地回环站点**（`tests/fixtures/`，标准库 `http.server` 构造的假站点）+ **临时源目录**（运行期生成 JSON 指向回环站点），做到「离线、可重复、不依赖外网」。全部测试走真实 HTTP 往返，验证的是真实调用链，不是 mock。

### 10.1 边界值测试

| 用例 | 输入 | 期望 |
|---|---|---|
| 单章节小说翻页终止 | 详情页只有 1 章，无 next | 正文抓取后即停，`pages=1`，不报错 |
| 单话漫画翻页终止 | 图集只有 1 页，无 next | 图片 1 张，`pages=1`，终止 |
| 单集视频分集遍历终止 | 分集列表只有 1 集 | 播放地址 1 条，终止 |
| 限制搜索页数 | `max_pages=2`，站点有 5 页 | 只抓 2 页就停 |
| 限制结果条数 | `max_results=3`，站点更多 | 只返回 3 条 |
| 去重防循环 | 站点下一页指向自身 | 命中去重即停，不无限循环 |

### 10.2 异常测试

| 用例 | 输入 | 期望 |
|---|---|---|
| 篡改校验选择器 | `check_selector` 指向不存在的标签 | 抛 `StructureChangedError`，提示「站点结构已变更，请更新源配置」 |
| 失效源隔离 | 源 A 正常 + 源 B `check_selector` 失效 | `search_type` 仍返回 A 的结果，B 进入 `errors` |
| 坏 JSON 跳过 | 源目录里放一个语法错误 JSON | 加载跳过该文件，不中断其余源 |
| source_id 冲突 | 两个源同 ID | 后者覆盖 + 告警 |
| 缺失字段 | JSON 缺 `detail_rule` | `ConfigError`，给出字段名 |
| 解密接口失败 | `decrypt_api` 指向不可达端口 | `DecryptError`，不影响其他源 |
| 正文为空 | 详情页无内容节点 | `ContentMissingError` |

### 10.3 功能闭环测试

链路：**加载源 → 查看分类 → 关键词搜索 → 进入详情 → 翻页阅读 → 批量下载**。
由 `tests/test_closed_loop.py` 端到端执行：用回环站点 + 临时源 JSON，走 `Scheduler` 完整跑一遍，断言每步产出且结果一致。

### 10.4 多源联合搜索测试

- 三个源（novel×2 + video×1）同类型检索：`search_type("novel", kw)` 合并两个小说源结果，标注 `source_name` 各归其位；video 源不参与。
- 断言合并结果条数 = 各源条数和，且每条含来源字段。

### 10.5 测试执行与覆盖口径

```bash
pip install -r requirements.txt
python -m pytest tests/ -v
```

- 期望全绿；覆盖率口径为「上述用例 100% 断言通过」。
- 框架运行时对 `lxml` 不可用的降级路径、以及 `sleep` 的真实等待，在 CI 中用短间隔配置覆盖。

---

## 11. 验收标准

对照本架构实现的框架必须满足：

1. **新增站点零改码**：把一份合法 JSON 放进 `sources/` 即可被识别并可爬；框架代码不变。
2. **解密外置**：框架源码中不出现任何解密算法实现，只有 `Decrypter` 调用壳。
3. **分层单向依赖**：§7.3 依赖图成立，模块间不互相 import 环。
4. **鲁棒**：单源失效不影响其他源；结构变更只告警不崩溃；资源上限生效。
5. **统一输出**：搜索、内容、下载均为统一数据对象，渲染层只管展示。
6. **测试全绿**：§10 全部用例通过 `pytest`。
7. **文档同步**：架构文档、示例源、README 与实际代码一致。
---

## 12. 主题与画风规范

> 主题是 GUI 层的契约，框架本体不感知；为统一多端（桌面 / 后续 Web / 移动），集中放在 docs/theme.md。
> 本节只约定它存在、它的目标、它与代码的绑定方式。

### 12.1 设计目标

- **二次元、轻量、温暖**：浅色（樱花粉 / 薄荷绿）和深色（夜樱紫）双主题。
- **可工程化**：所有颜色 / 圆角 / 阴影 / 字号都用 **token** 表达，**不写死**。
- **可访问**：所有正文对背景对比度 ≥ 4.5:1（浅色）/ 7:1（深色）。
- **可切换**：主题切换实时生效，不刷新、不重启。

### 12.2 主题清单

| Key | 名称 | 风格 |
|---|---|---|
| sakura   | 樱花粉（默认浅色） | 桃雪背景 + 樱粉强调 |
| mint     | 薄荷绿（备选浅色） | 奶油白背景 + 薄荷绿强调 |
| midnight | 夜樱紫（深色）     | 紫夜背景 + 霓虹粉强调 |

未来可扩展：ocean（海蓝）、matcha（抹茶）。所有主题的字段集必须一致。

### 12.3 关键 Token（详细值见 docs/theme.md）

- 调色板：--accent / --accent-2 / --accent-3 / --bg / --bg-2 / --text / --text-2 / --danger / --warning / --success / --stroke / --glow
- 圆角：--radius-sm: 6px / --radius-md: 12px / --radius-lg: 20px / --radius-pill: 999px
- 阴影：--shadow-1 / --shadow-2 / --shadow-glow
- 字体：中文 汉仪乐喵体 / 思源黑体 Rounded；英文 Quicksand / Nunito Rounded；代码 JetBrains Mono Rounded

### 12.4 视觉规则

- 圆角统一 12px；按钮 hover 时 scale(1.03) + 阴影到 --shadow-glow。
- 空状态必须有 Q 版插画 + 一句二次元台词（"还没有源哦，要不要找一只？"）。
- Loading 用花瓣 / 蝴蝶结进度环，不用默认 spinner。
- 吉祥物 logo（可选）：运行中 / 报错 / 待机 三种表情。

### 12.5 插画与字体来源

- 占位插画：CC0 / 自制 / 委托（推荐）
- 字体：思源黑体 / 汉仪乐喵体（务必保留版权声明）
- 严禁使用未授权角色 / 商业素材

### 12.6 落地路径

| 技术栈 | 实现方式 |
|---|---|
| PySide6 / PyQt6 | QSS 模板按 token 生成；运行时切 qApp.setStyleSheet(...) 即可。 |
| Tauri (Web 桌面) | 自定义 Theme for shadcn / DaisyUI；CSS Variables 集中维护。 |
| Electron | Tailwind CSS 	heme.extend + CSS Variables。 |
| 浏览器样式   | 直接读 docs/theme.md 复制 token 进样式表。 |

---

## 13. UI 与 Framework 解耦模式

> 框架本体保持 CLI-only + 库形态；GUI 是上层独立组件。
> 三种集成模式，按复杂度自选。

### 13.1 模式 A：进程内直接绑定（同线程）

- GUI 进程直接 import framework.scheduler.Scheduler。
- 优点：调试简单、零 IPC 成本。
- 缺点：UI 卡顿会阻塞爬取。
- 适用：调试 / 极小工作量。

### 13.2 模式 B：同进程多线程 + 事件总线（推荐）

- Scheduler 在后台 ThreadPoolExecutor 跑，UI 线程不阻塞。
- 全部爬取事件通过 ramework.events.EventBus 广播。
- 事件类型：SearchStarted / SearchProgress / SearchCompleted / SourceError / DownloadStarted / DownloadProgress / DownloadCompleted / Log / StructureChanged。
- 适用：个人 GUI 项目。**默认推荐**。

### 13.3 模式 C：独立进程 IPC

- framework 独立 python -m framework daemon 启动，GUI 通过 ZeroMQ / Unix Socket / stdin-out 通信。
- 优点：framework 独立升级、跨语言。
- 缺点：复杂度高、状态序列化难。
- 适用：未来要做"远程爬虫服务"时。

### 13.4 选型建议

- **个人用 B**。EventBus 实现最小化：线程安全的订阅者列表 + 事件快照。
- **不在 Scheduler 里写 UI 逻辑**；所有 UI 行为通过 EventBus 触发。
- **Scheduler 的 CLI 路径不依赖 EventBus**（CLI 仍是同步调用）。
- 事件常量定义在 ramework/events.py：EVENT_SEARCH_STARTED 等。

### 13.5 与框架现有模块的接入点

| 现有模块 | 接入方式 |
|---|---|
| Scheduler.search_type | 入口 emit SEARCH_STARTED，每源 emit SEARCH_PROGRESS，结束 emit SEARCH_COMPLETED 或 SOURCE_ERROR |
| Scheduler.fetch_* | 同上模式：emit DOWNLOAD_STARTED / DOWNLOAD_PROGRESS / DOWNLOAD_COMPLETED |
| StructureChecker | 失败时 emit STRUCTURE_CHANGED，payload 含 source_id / url / selector |
| Downloader | 每张图片 / 每段正文 emit DOWNLOAD_PROGRESS（带 idx / total / path） |

EventBus 默认为 None（兼容旧 CLI 调用）；当外部传入时启用，不传时整个事件链不工作，运行效率不受影响。

---

## 14. 源编辑器交互协议

源编辑器是 GUI 的核心组件：用户可视化编辑 sources/*.json，系统实时校验。
所遵循的契约：

### 14.1 数据契约

- **读**：SourceConfig.from_dict(parsed_json) —— 复用配置层校验。
- **写**：编辑器只产生临时 diff，最后应用时一次性回写。
- **不变量**：保存后的 JSON 必须能通过 SourceConfig.from_dict 严格校验，否则禁止落盘。

### 14.2 实时预览 API（在 ramework/preview.py 中）

`python
class SourcePreview:
    def validate_selector(
        self, source: SourceConfig, url: str, field_name: str, selector: dict
    ) -> PreviewResult: ...

    def preview_search(
        self, source: SourceConfig, keyword: str, page: int = 1
    ) -> dict: ...

    def preview_detail(
        self, source: SourceConfig, url: str
    ) -> dict: ...
`

- PreviewResult 包含：matches: List[str]、suggested_selector: Optional[dict]（基于命中元素反向推导，未来可有）、error: Optional[str]。
- 调用期间**不修改磁盘源 JSON**，副作用只读。
- 预览失败（如站点改版）时 error 字段非空，前端高亮 + 给出修复建议。

### 14.3 编辑器交互流程

`
加载源文件
    ↓
解析为 SourceConfig
    ↓
UI 渲染字段表（按字段分组：分类 / 搜索 / 详情 / 解密 / 反爬）
    ↓
用户修改某一字段
    ↓
触发 validate_selector 预览（实时）
    ↓
校验不通过 → 字段红边 + 详细提示
    ↓
用户点"保存"
    ↓
写回 JSON + 重新加载 SourceManager
`

### 14.4 错误诊断面板

- 失败的源在面板里展示最近的 StructureChangedError 取样 + 建议的修复方向。
- 字段命名风格：简洁、英文（与代码常量一致）。
- "复制可工作的源模板"按钮：一键从 sources/template.example.json 复制一份到手选目录。

### 14.5 跨平台行为

- 预览请求走 HttpClient，**强制走 settings 的代理 / 限速**，不绕过全局设置。
- 预览超时与重试由 HttpClient 统一处理，编辑器不需要重复实现。

---

## 15. 验收标准（GUI 维度补充）

除 §11 原有 7 条外，GUI 版本还必须满足：

1. **可双主题切换**：浅色 / 深色切换不影响布局、不刷新整页，仅刷新 token。
2. **所有爬取事件进 EventBus**：UI 可订阅进度、错误、日志；CLI 路径不依赖 EventBus，自动降级。
3. **应用级设置可持久化且优先级正确**：源配置优先于全局默认；坏字段不阻塞启动。
4. **源编辑器可视化**：所见即所得编辑 selector，校验不通过时字段高亮 + 错误提示。
5. **设置面板完整覆盖 `app_config.json` 全字段**：每个字段都有对应 UI，不能"写在 JSON 里但 GUI 不暴露"。
6. **空状态有插画 + 台词**：禁止光秃秃占位符；loading 不使用默认 spinner。
7. **主题切换 / 字体缩放实时生效**：不需重启，不刷新整窗。
8. **下载进度可订阅**：UI 能渲染"5 / 120 章节"的进度条与"已用 X 秒 / 剩余 Y 秒"估算。
9. **失败源有诊断面板**：被 StructureChangedError 命中的源一键跳到源编辑器，自动填充失败的 selector。
10. **图标体系统一**：所有图标走同一套线性图标库（lucide / phosphor），不同 tab 不混搭风格。
