# 内核设计文档（整体设计逻辑）

> 版本：v1.0 · 日期：2026-08-02 · 状态：定稿
>
> 本文档描述**内核如何消费源配置键值对、爬取逻辑如何设计**的整体思路。
> 是理解 `core.md`（模块清单）与 `source-schema-v2.md`（键值对库）之间**如何联动**的桥梁。
> 阅读顺序建议：architecture.md → source-schema-v2.md → **本文档** → core.md → ui-*.md。

---

## 一、核心原则：配置驱动

**源 JSON 里的键值对 = 一份可执行的爬取计划书。**
内核不是"为某个站写死逻辑"，而是**"读懂计划书并执行"**。

```
改站点 = 加/改 JSON，框架永不改
改框架 = 改 Python（通用能力，不针对任何站点）
```

---

## 二、三层架构：配置 → 执行引擎 → 业务编排

```
┌─ 第一层：配置加载（把 JSON 变成可信赖的对象）─────────────┐
│   config.py 读取 sources/*.json                            │
│   → SourceConfig 对象（带校验、默认值、按类型路由）        │
└────────────────────────────────────────────────────────────┘
┌─ 第二层：通用执行引擎（读配置、按计划爬）─────────────────┐
│   http / parser / selfcheck / decrypter / adblock          │
│   这些是"怎么发请求、怎么解析、怎么解密、怎么过滤"的能力   │
└────────────────────────────────────────────────────────────┘
┌─ 第三层：业务编排（把能力组织成操作）─────────────────────┐
│   discovery / search / content / bulk_fetch / downloader    │
│   每个业务模块 = 一个"读取配置 → 调能力 → 产出结果"流程    │
└────────────────────────────────────────────────────────────┘
```

| 层 | 模块 | 职责 | 是否含站点逻辑 |
|---|---|---|---|
| 配置加载 | config.py / source_manager.py / settings_manager.py | JSON→对象、校验、默认值、按类型路由 | 否 |
| 执行引擎 | http.py / parser.py / selfcheck.py / decrypter.py / adblock.py / events.py | 通用能力：请求/解析/自检/解密/过滤/事件 | 否 |
| 业务编排 | discovery.py / search.py / content.py / bulk_fetch.py / downloader.py / download_queue.py / library_manager.py / preview.py / scheduler.py / render.py | "读配置→调能力→产出" | 否（读配置决定行为） |

---

## 三、爬取调用链设计：以「搜索」为例

```
用户输入关键词 → Scheduler.search_type("novel", "凡人")
  │
  ├─ ① SourceManager 选目标源
  │    → 遍历 SourceManager.by_type("novel")
  │    → 每个源都是 SourceConfig 对象（config.py 已解析）
  │
  ├─ ② 读取源配置 endpoints.search
  │    → source.search_config（config.py 解析好的对象）
  │    → 拿到 base_url / method / query_template / paginator / item
  │
  ├─ ③ 构造请求（http.py 消费 transports）
  │    → base_url + query_template 填入 {keyword} → URL
  │    → method=GET → get / method=POST → post_form
  │    → 带上 transports.headers / proxy / timeout / interval_ms
  │
  ├─ ④ 结构自检（selfcheck.py）
  │    → 读 diagnostics.selfcheck.strategy
  │    → soft: 失败仅告警 / strict: 失败中止 / off: 跳过
  │
  ├─ ⑤ 解析结果（parser.py 消费 item 的 selector）
  │    → item.root_selector 定位每个结果项
  │    → fields.title / url / cover / author 逐个提取
  │    → 相对 URL 拼成绝对（transports.base_url）
  │
  ├─ ⑥ 翻页（paginator）
  │    → increment: page+1 直到 constraints.search.max_pages
  │    → next_link: 抓下一页链接
  │    → cursor: offset+step
  │
  └─ ⑦ 返回统一 SearchResult 对象列表
       → 每个结果含 title/cover/source_id/source_name/url/update_info
```

**关键点**：内核里的 `search.py` **不写任何站点的选择器**。它只写"怎么用 `endpoints.search.item.fields` 去提取"。选择器是什么、URL 长什么样，全由源 JSON 决定。

---

## 四、三类站型的差异化处理

源配置支持三套接口描述，解决不同站的差异：

| 站型 | 用哪个键 | 例子 |
|---|---|---|
| 普通 HTML 站 | `endpoints`（selector 解析） | 笔趣阁（SSR） |
| API 站 | `api_endpoints`（JSONPath 解析） | 拷贝漫画 v3 API、B站 |
| SPA 站 | `render`（playwright 渲染） | 包子漫画（Vue） |

同一个业务模块（如 search）内部：

```
search(source, keyword):
    if source.api_endpoints.search 存在 → 走 API 路径（JSONPath）
    elif source.endpoints.search 存在   → 走 HTML 路径（selector）
    else → 报配置错误
```

**这就是"配置驱动"的精髓**：同一个 `search.py`，通过读不同的键值对，能爬 HTML 站、API 站、SPA 站，而不用改内核。

---

## 五、各业务模块的调用链

### 5.1 发现（discovery）

```
Scheduler.list_categories(source_id)
  → 读 endpoints.discovery / api_endpoints.discovery
  → 若没配置 → 该源不出现在发现界面
  → 配置了 → 请求分类页/API → 解析 list_item.fields
  → 返回 [Category(title, url)]
```

### 5.2 内容（content）—— 按 $type 分派

```
Scheduler.fetch_detail(source_id, url)
  → 读 endpoints.content.{chapter|page|episode}
  → novel:  章节列表 → 逐章正文（body.selector）→ 长章节翻页（body.paginator）
  → comic:  分页图片（list.fields.url）→ 图集翻页（list.paginator）
  → video:  分集列表 → 播放地址（play_url）→ 解密（decryption）
  → 每步间过 adblock 过滤
  → 返回 ContentResult（统一结构）
```

### 5.3 解密（decryption）

```
content.py 解析出密文
  → 读 decryption.targets.{content|image|video_url}
  → 交给 decrypter.py
  → 按 strategy 分派：
       xor / aes_cbc / aes_ecb / rsa → 本地通用算法（密钥/iv 来自配置）
       custom_endpoint → 调自建解密服务（外置 API）
  → 返回明文
```

> 解密也不写死站点密钥/算法——密钥、iv、endpoint 全在源 JSON 的 `decryption` 里。

### 5.4 广告过滤（adblock）

```
content.py 拿到正文 html / 图片列表 / 播放列表
  → 交给 adblock.filter_content(content_type, data, source)
  → 读全局 rules_{novel,comic,video}.yaml
  → 合并源级 ad_block（extra_css / block_domains / block_url_regex）
  → 剔除广告节点/图片/播放段
  → 返回干净内容
```

> 规则优先级：源级 ad_block > 全局该类型规则库 > 全局通用规则。

### 5.5 全量抓取（bulk_fetch，方案B 元数据索引）

```
Scheduler.bulk_fetch(source_id)
  → 遍历全部分类（若有）→ 每分类翻页到底
  → 每作品：拉详情元数据（title/cover/url/update）
  → 只存元数据到 SQLite 索引，不拉正文/图片
  → 进度经 EventBus 广播
  → 完成 → 书架可浏览/搜索全站索引
```

### 5.6 下载（downloader / download_queue）

```
下载队列加入任务
  → 按类型分派：
       novel: 逐章拉正文 → 落盘 txt（adblock 后）
       comic: 逐图拉取 → 落盘图片
       video: 拉播放地址 → 导出 m3u8 清单
  → skip_existing 续传（跳过已下载）
  → 失败自动重试 3 次
  → 进度经 EventBus 广播 → 完成通知
```

---

## 六、统一数据对象（GUI 与内核的契约）

内核所有业务模块返回**统一数据对象**，GUI 只管渲染，不关心数据从哪个站来。

| 对象 | 字段 | 由谁产生 |
|---|---|---|
| `SourceConfig` | source_id/$type/$name/transports/endpoints/... | config.py |
| `Category` | title / url | discovery.py |
| `SearchResult` | title / cover / source_id / source_name / url / update_info | search.py |
| `ContentResult` | source_id / content_type / url / title / chapters / pages / texts / videos | content.py |
| `SearchReport` | keyword / results / errors / total | search.py |

---

## 七、事件驱动（GUI 异步）

GUI 通过 EventBus 订阅进度/错误，不阻塞 UI 线程。

```
业务模块执行中
  → emit 事件（SEARCH_PROGRESS / DOWNLOAD_PROGRESS / STRUCTURE_CHANGED ...）
  → EventBus.broadcast
  → GUI 各页订阅 → 刷新（进度条/吉祥物/状态灯）
```

> 事件常量见 core.md §4.4 / PROJECT.md §四.4.4。

---

## 八、约束与边界（与 core.md 一致）

| 约束 | 说明 |
|---|---|
| 不写死站点规则 | 选择器/接口/翻页/广告规则全外置（源 JSON + 规则库 yaml） |
| 不内置解密算法密钥 | 密钥/iv/endpoint 全在配置；算法为通用库 |
| 广告规则可配置 | 内置规则库 + 源级补充 + 额外规则目录 |
| 不做视频流播放 | 只解析播放地址 |
| 数据本地化 | 收藏/历史/续读 JSON；书架索引 SQLite |
| 鲁棒 | 单源失败不影响其他源；结构变更只告警；并发/页数硬上限 |

---

## 九、设计要点回顾

1. **配置是数据，内核是通用执行器**。
2. **业务模块 = 读配置 → 调能力 → 产出**，不写站点逻辑。
3. **三类站型（HTML/API/SPA）**由 `endpoints` / `api_endpoints` / `render` 键区分，内核统一。
4. **解密、广告过滤**都是"能力 + 配置"，密钥/规则全外置。
5. **统一数据对象**是 GUI 与内核的稳定契约。
6. **事件驱动**让 GUI 不卡线程。
