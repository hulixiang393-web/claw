# 框架内核（framework/）

> 无界面，纯 Python 库。
> GUI 通过 EventBus（模式 B）或直接调用（模式 A）接入。
> 状态：**模块清单已定稿**

## 定位

配置驱动的多源爬虫内核。规则全部外置（源 JSON + 规则库），框架只做通用能力：调度、分发、展示、翻页、多源搜索、解密外置、广告过滤、下载调度。

## 模块清单

### 基础层（无业务，通用能力）

| 模块 | 职责 | 状态 |
|---|---|---|
| config.py | 源配置 v2 schema 解析 + 校验（`SourceConfig.from_dict`） | ✅ 已定 |
| errors.py | 异常体系（SourceError / ConfigError / StructureChangedError / ContentMissingError / RequestError / DecryptError） | ✅ 已定 |
| http.py | 会话 / 重试 / 代理 / 限速 / TLS / 编码（transports 驱动） | ✅ 已定 |
| parser.py | CSS/XPath 选择器 + fallback 链 + attr 提取 + URL 拼接 | ✅ 已定 |
| events.py | 事件总线（线程安全订阅者列表 + 事件常量） | ✅ 已定 |

### 配置与数据源层

| 模块 | 职责 | 状态 |
|---|---|---|
| source_manager.py | 源加载 / 分组 / 隔离坏文件 / `$enabled` 启停 / `$weight` 权重 / 健康状态历史 / 只列有 discovery 的源 | ✅ 已定 |
| settings_manager.py | app_config.json 全局设置读写 + 默认值合并 | ✅ 已定 |

### 业务爬取层

| 模块 | 职责 | 状态 |
|---|---|---|
| selfcheck.py | 结构自检（off/soft/strict）+ 自动禁用 + interval 缓存 | ✅ 已定 |
| decrypter.py | 外置解密接口调用（POST/GET，payload_key/result_key） | ✅ 已定 |
| discovery.py | 分类 / 发现列表 + 全量抓取入口 | ✅ 已定 |
| search.py | 多源搜索（单源/跨源/合并/权重）+ **合并相似结果开关** | ✅ 已定 |
| content.py | 详情 + 正文/图片/分集 + 翻页 + **章内分页拼接**（长章节跨 `_N.html`）+ **目录缓存** + **广告过滤接入** | ✅ 已定 |
| adblock.py | **广告过滤引擎**：加载规则库（novel/comic/video）+ 合并源级规则 → 过滤正文/图片/播放页 | ✅ 已定 |

### 调度与存储层

| 模块 | 职责 | 状态 |
|---|---|---|
| scheduler.py | 调度中枢（流程编排 + 生命周期 + 日志 + 错误隔离） | ✅ 已定 |
| bulk_fetch.py | **全量抓取**：遍历源全站列表/分类 → 聚合入库（方案B 元数据索引） | ✅ 已定 |
| downloader.py | 按类型下载 / 并发 / 命名 / 续传（skip_existing）/ 失败重试 3 次 / 完成通知 | ✅ 已定 |
| download_queue.py | **下载队列**：并发调度 / 暂停 / 继续 / 取消 / 进度广播 | ✅ 已定 |
| library_manager.py | **书架**：扫描本地目录 + 收藏库 + 续读记录 | ✅ 已定 |
| search_history.py | **最近搜索历史**（20条）持久化 | ✅ 已定 |
| preview.py | 源编辑器预览 API（validate_selector / preview_search / preview_detail） | ✅ 已定 |
| render.py | 文本渲染（CLI 展示） | ✅ 已定 |

### 规则库资源（非模块，随框架打包）

```
framework/adblock/
  ├── rules_novel.yaml    # 小说广告规则
  ├── rules_comic.yaml    # 漫画广告规则
  ├── rules_video.yaml    # 视频广告规则
  └── adblock.py          # 引擎
```

> 规则库路径可配置：settings 里可指定额外规则目录，用户可放自定义规则（覆盖/追加内置）。

## 广告过滤规则体系（adblock）

### 结构：两层规则
```
全局规则库（内置，按类型 novel/comic/video 分）
        +  （优先级：源级 > 全局该类型 > 全局通用）
源级补充规则（源 JSON 可选配 adblock.rules）
```

### 各类型规则内容

| 类型 | 规则类型 | 规则内容 | 示例 |
|---|---|---|---|
| **小说** | CSS/XPath | 正文内 ad/banner/promo 节点剔除 | `.ads`, `.ad`, `#adtext`, `.promo`, `.gg` |
| 小说 | 文本正则 | 广告文案整段删除 | `请记住本站`, `最新章节请记住`, `手机阅读请访问`, `广告/推广` |
| 小说 | 位置 | 正文首/尾固定广告位 | 首段前/末段后的 `.read-ad` |
| **漫画** | URL 特征 | 图片 URL 含广告标记剔除 | 域名含 `ad.`/`banner`/`promo` |
| 漫画 | 尺寸异常 | 尺寸显著小于漫画页判为广告横幅 | 正常 800x1200，广告 800x60 |
| 漫画 | 数据流 | 返回条目 type=ad 剔除 | `data-src` 指向广告域名 |
| **视频** | 播放列表 | m3u8 里广告段剔除 | `#EXT-X-DISCONTINUITY` 后广告流 |
| 视频 | DOM 节点 | 内嵌播放页屏蔽广告元素 | `.ad-container`, `#adbox`, `.banner-player` |
| 视频 | 遮罩/弹窗 | 播放前广告遮罩剔除 | `.ad-mask`, `.countdown-ad` |

### 规则文件格式（yaml）
```yaml
# rules_novel.yaml（示意）
version: 1
type: novel
css_selectors:
  - ".ads"
  - ".banner"
  - "#adtext"
text_patterns:
  - "请记住本站"
  - "最新章节请记住"
  - "手机阅读请访问"
position_filters:
  - "header"
  - "footer"
```

## 数据持久化（混合方案）

| 数据 | 格式 | 存储 |
|---|---|---|
| 源配置 | JSON | `sources/*.json` |
| 全局设置 | JSON | `app_config.json` |
| 搜索历史 | JSON | 本地文件 |
| 收藏库 | JSON | 本地文件 |
| 续读记录 | JSON | 本地文件 |
| 健康状态历史 | JSON | 本地文件 |
| 书架已下载目录索引 | SQLite | 本地库（量大时用） |
| 全量抓取索引（方案B） | SQLite | 本地库 |

## 数据流

```
GUI 页面
  ├─ 调用 Scheduler API（同步）或订阅 EventBus（异步）
  ├─ Scheduler → SourceManager → config（校验源）
  ├─ Search / Discovery / Content → http · parser · selfcheck · decrypter · adblock
  ├─ BulkFetch → Discovery/Search/Content → 聚合入库
  ├─ DownloadQueue → Downloader → http · adblock → 本地文件
  ├─ LibraryManager → 本地文件/SQLite
  └─ 事件 → EventBus → GUI 各页刷新
```

## 模块依赖（单向）

```
scheduler
  ├── source_manager → config
  ├── discovery/search/content → http · parser · selfcheck · decrypter · adblock
  ├── bulk_fetch → discovery · search · content
  ├── download_queue → downloader → http · adblock(可选)
  ├── library_manager → (本地文件/SQLite)
  ├── search_history → (本地文件)
  ├── preview → content · search · parser
  ├── render → (数据对象)
  └── events → (事件常量)
```

## 约束与边界

| 约束 | 说明 |
|---|---|
| 不写死站点规则 | 选择器/接口/翻页/广告规则全外置（源 JSON + 规则库 yaml） |
| 不内置解密 | 解密一律外置 `decrypt_api`，框架只有调用壳 |
| 广告规则可配置 | 内置规则库 + 源级补充 + 可指定额外规则目录 |
| 不做视频流播放 | 只解析播放地址，播放交给外部/内嵌源页 |
| 数据本地化 | 收藏/历史/续读本地 JSON；书架索引 SQLite；不依赖外网服务 |
| 鲁棒 | 单源失败不影响其他源；结构变更只告警；并发/页数硬上限 |

## 待讨论
- （内核模块清单已定稿，进入实现规划阶段）
