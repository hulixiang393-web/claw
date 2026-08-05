# Source 配置 Schema 字段速查

> 每个字段 3 列：键 / 类型 / 用途。
> 必填字段直接展示，选填字段用 `<details>` 折叠。
> 详细设计见 `architecture.md`、站点结构分析见 `site-analysis.md`。

---

## 1. 顶层字段（17 个）

### 1.1 必填（6 个）

| 键 | 类型 | 用途 |
|---|---|---|
| `$schema_version` | `int` | schema 版本（当前 2） |
| `$id` | `string` | 源唯一标识（小写蛇形命名） |
| `$type` | `enum` | 内容类型 `novel` / `comic` / `video` |
| `$name` | `string` | UI 展示名 |
| `transports` | `object` | 网络层配置 |
| `endpoints` | `object` | HTML 业务接口 |

### 1.2 选填（11 个）

<details>
<summary>选填顶层字段</summary>

| 键 | 类型 | 必填场景 | 用途 |
|---|---|---|---|
| `$enabled` | `bool` | - | GUI 一键开关（默认 true） |
| `$weight` | `float` | - | 搜索排序权重 0.0~10.0（默认 1.0） |
| `$metadata` | `object` | - | 站点名片 |
| `api_endpoints` | `object` | API 站 | API 直调端点（替代 endpoints） |
| `render` | `object` | SPA 站 | JS 渲染引擎（playwright） |
| `decryption` | `object` | 加密站 | 解密配置 |
| `constraints` | `object` | - | 各阶段爬取上限 |
| `diagnostics` | `object` | - | 诊断自检 |
| `media` | `object` | video 必填 | 视频媒体处理 |
| `auth` | `object` | 登录站 | 登录态管理 |
| `ad_block` | `object` | - | 广告拦截 |

</details>

按 `$type` 额外必填：

| `$type` | 必填补充 |
|---|---|
| `novel` | `endpoints.content.chapter` |
| `comic` | `endpoints.content.page` |
| `video` | `endpoints.content.episode` + `media` |

### 1.3 按类型字段矩阵（源编辑器动态表单用）

> 一眼看清 novel / comic / video 各需要哪些键。`●`=必填，`○`=选填，`-`=不需要。

| 字段块 | novel | comic | video |
|---|---|---|---|
| `$schema_version` / `$id` / `$type` / `$name` | ● | ● | ● |
| `transports` | ● | ● | ● |
| `endpoints.discovery` | ○ | ○ | ○ |
| `endpoints.search` | ● | ● | ● |
| `endpoints.detail` | ● | ● | ● |
| `endpoints.content.chapter` | ● | - | - |
| `endpoints.content.page` | - | ● | - |
| `endpoints.content.episode` | - | - | ● |
| `media` | - | - | ● |
| `api_endpoints` | ○ | ○ | ○ |
| `render` | ○ | ○ | ○ |
| `decryption` | ○ | ○ | ○ |
| `constraints` | ○ | ○ | ○ |
| `diagnostics` | ○ | ○ | ○ |
| `auth` | ○ | ○ | ○ |
| `ad_block` | ○ | ○ | ○ |
| `$enabled` / `$weight` / `$metadata` | ○ | ○ | ○ |

> 源编辑器（ui-editor.md）按此矩阵动态显示字段块：novel 只显示 chapter，不显示 page/episode。

---

## 2. transports（网络层）

**必填**：

| 键 | 类型 | 用途 |
|---|---|---|
| `base_url` | `string` | 站点根 URL，所有相对路径拼接基准 |

**选填**：

<details>
<summary>选填字段</summary>

| 键 | 类型 | 用途 |
|---|---|---|
| `headers` | `object<string,string>` | 请求头（含 UA） |
| `cookies` | `object<string,string>` | 每次请求携带的 Cookie |
| `proxy` | `string` | 代理 URL（`http://user:pass@host:port`） |
| `tls` | `object` | TLS 配置（`verify` / `fingerprint`） |
| `timeout` | `int` | 单请求超时秒（默认 10） |
| `retries` | `int` | 失败重试次数（默认 3） |
| `retry_backoff` | `object` | 退避策略（`base` / `max` / `jitter`） |
| `interval_ms` | `int` | 两次请求间隔 ms（默认 500） |
| `charset` | `string` | 响应编码（默认 `utf-8`） |
| `follow_redirects` | `bool` | 跟随重定向（默认 true） |

</details>

---

## 3. endpoints（HTML 业务接口）

### 3.1 discovery（分类页，全选填）

<details>
<summary>字段</summary>

| 键 | 类型 | 用途 |
|---|---|---|
| `list_url` | `string` | 分类入口页 URL |
| `list_paginator` | `paginator` | 分类页翻页器 |
| `list_item.root_selector` | `selector` | 分类项根节点 |
| `list_item.fields.title` | `selector` | 分类名 |
| `list_item.fields.url` | `selector` | 分类页 URL |
| `list_item.fields.cover` | `selector` | 封面图 |
| `list_item.fields.tags` | `selector` | 分类标签 |

</details>

### 3.2 search

**必填**：

| 键 | 类型 | 用途 |
|---|---|---|
| `base_url` | `string` | 搜索接口 URL |
| `item.root_selector` | `selector` | 搜索结果项根节点 |

**选填**：

<details>
<summary>选填字段</summary>

| 键 | 类型 | 用途 |
|---|---|---|
| `method` | `enum` | GET / POST |
| `query_template` | `string` | URL/Body 中关键词模板（`{keyword}`� |
| `page_param` | `string` | 页码参数名 |
| `extra_params` | `object` | 额外固定参数 |
| `headers` | `object` | 搜索专有请求头 |
| `body_format` | `enum` | POST 体格式 `form` / `json` |
| `response_type` | `enum` | 响应类型 `html` / `json` |
| `paginator` | `paginator` | 搜索翻页器 |
| `requires_token` | `bool` | 是否需要先拿 token |
| `token_path` | `string` | token 提取 selector |
| `token_url` | `string` | 先请求这个 URL 拿 token |
| `post_body` | `object` | POST 体 |
| `scroll_to_load` | `bool` | 是否滚动加载更多 |
| `max_results` | `int` | 全量显示多少 |
| `item.fields.title` | `selector` | 结果标题 |
| `item.fields.url` | `selector` | 详情页 URL |
| `item.fields.cover` | `selector` | 封面图 |
| `item.fields.author` | `selector` | 作者 |
| `item.fields.update` | `selector` | 最新更新 |
| `item.fields.tags` | `selector` | 标签 |
| `item.fields.status` | `selector` | 状态 |

</details>

### 3.3 detail（详情页）

**必填**：

| 键 | 类型 | 用途 |
|---|---|---|
| `fields.title` | `selector` | 主标题 |

**选填**：

<details>
<summary>选填字段</summary>

| 键 | 类型 | 用途 |
|---|---|---|
| `url_pattern` | `string` | URL 正则校验 |
| `fields.author` | `selector` | 作者 |
| `fields.cover` | `selector` | 封面 |
| `fields.summary` | `selector` | 简介 / 描述 |
| `fields.status` | `selector` | 状态 |
| `fields.tags` | `selector` | 标签 |
| `fields.meta` | `object` | 自定义额外字段 |

</details>

### 3.4 content（按 `$type` 分章节必填）

#### chapter（novel）

**必填**：

| 键 | 类型 | 用途 |
|---|---|---|
| `list.root_selector` | `selector` | 章节列表项根节点 |
| `list.fields.title` | `selector` | 章节标题 |
| `list.fields.url` | `selector` | 章节 URL |
| `body.selector` | `selector` | 章节正文节点 |

<details>
<summary>选填字段</summary>

| 键 | 类型 | 用途 |
|---|---|---|
| `list.paginator` | `paginator` | 章节列表翻页器 |
| `body.attr` | `string` | 取属性（默认取文本） |
| `body.attr_path` | `string` | 任意属性（如 `data-src`） |
| `body.paginator` | `paginator` | 章内分页器（`next_link`）。长章节跨多页（如 `xxx.html` / `xxx_1.html` / `xxx_2.html`）时，`next_link.selector` 指向"下一页"链接；框架抓完全部续页拼接后返回正文 |

> **章内分页（novel）**：框架默认对 novel 源开启分页探测——抓完一页正文后，
> 从页脚导航（`read-nav` 等）找同章续页链接（URL 基路径相同且带 `_<数字>.html`
> 后缀），跨页拼接；用 `body.paginator.next_link.selector` 可显式指定续页链接，
> 缺省时框架自动探测。防死循环：`max_pages` 上限 + URL 去重（seen）。
| `body.filter` | `object` | 内容层过滤（见下） |
| `list.root_selector.exclude` | `array<selector>` | 排除广告节点 |

`body.filter` 子键：

| 键 | 类型 | 用途 |
|---|---|---|
| `exclude_selectors` | `array<selector>` | 移除匹配的元素（连同子树） |
| `exclude_text_regex` | `array<string>` | 移除文本匹配这些正则的行 |
| `exclude_attr_regex` | `object<string,string>` | 移除属性匹配的元素 |
| `exclude_link_domain` | `array<string>` | 移除链接指向这些域名的链接 |
| `min_text_length` | `int` | 短于此长度的文本行当作噪声 |
| `replace_text_regex` | `object<string,string>` | 替换文本（regex → 替换串） |

</details>

#### page（comic）

**必填**：

| 键 | 类型 | 用途 |
|---|---|---|
| `list.root_selector` | `selector` | 单图项根节点 |
| `list.fields.url` | `selector` | 图片 URL |

<details>
<summary>选填字段</summary>

| 键 | 类型 | 用途 |
|---|---|---|
| `list.paginator` | `paginator` | 图片列表翻页器 |
| `list.fields.url.attr_path` | `string` | 懒加载用 `data-src` |
| `list.root_selector.exclude` | `array<selector>` | 排除广告图 |

</details>

#### episode（video）

**必填**：

| 键 | 类型 | 用途 |
|---|---|---|
| `list.root_selector` | `selector` | 集项根节点 |
| `list.fields.title` | `selector` | 集标题 |
| `list.fields.url` | `selector` | 播放页 URL |
| `play_url.selector` | `selector` | 实际视频源 URL 选择器 |

<details>
<summary>选填字段</summary>

| 键 | 类型 | 用途 |
|---|---|---|
| `list.paginator` | `paginator` | 分��列表翻页器 |
| `play_url.attr` | `string` | 取 src（默认） |
| `list.root_selector.exclude` | `array<selector>` | 排除广告集 |
| `source_switch` | `object` | 多播放源换源（见下） |

</details>

#### source_switch（换源，video 多播放源站）

> MacCMS 等"同剧多源"（如 `?sid=N`）站的换源配置。配置后：
> - `fetch_detail` 提取所有可用播放源 → `Detail.source_list`（GUI 显示源下拉框）
> - 分集按当前 `sid` 从 `ep_list_selector` 提取（各源分集独立）
> - 播放地址从 `player_aaaa` JS 配置解析，按 `ps` 决定直接用 / 走 `parse` 转码
> - `switch_source(source, detail_url, new_sid)` 切源重抓

| 键 | 类型 | 用途 |
|---|---|---|
| `param` | `string` | 换源 URL 参数名（如 `sid`） |
| `list_selector` | `string` | 播放源 tab 根选择器（含源 ID 属性，如 `.player_name[data-sid]`） |
| `list_attr` | `string` | 源 ID 属性名（如 `data-sid`） |
| `name_selector` | `string` | 源名选择器（缺省用根节点文本） |
| `ep_list_selector` | `string` | 分集列表选择器，`{sid}` 占位换成当前源 ID（如 `#playlist{sid} a[href*='/tv/']`） |
| `play_regex` | `string` | 播放配置 JS 提取正则（如 `var player_aaaa=(\{.*?\})\s*</script>`） |
| `play_url_path` | `string` | player_aaaa 里播放地址字段（默认 `url`） |
| `playerconfig_url` | `string` | 可选：加载 playerconfig.js 补全各源 `ps`/`parse` |

**ps/parse 机制**（参考 MacCMS playerconfig）：
- `ps=0`：`url` 直接用（m3u8 直链 / iframe embed 页）
- `ps=1`：`url` 需经 `parse` 接口转码 → `{parse}?url={url}`

</details>

---

## 4. api_endpoints（API 直调端点，全部选填）

> 与 `endpoints` 二选一或互补。能直调 API 的源优先用本块，跳过 HTML 解析。
> **发现界面识别**：配置了 `endpoints.discovery` **或** `api_endpoints.discovery` 的源才出现在发现界面。

<details>
<summary>4.0 discovery（发现/分类 API）</summary>

| 键 | 类型 | 用途 |
|---|---|---|
| `url` | `string` | 分类/列表 API URL |
| `method` | `enum` | GET / POST |
| `params` | `object` | 参数 |
| `headers` | `object` | 额外请求头 |
| `body` | `object\|string` | POST 体 |
| `response_path` | `string` | JSONPath 定位列表数据 |
| `item_fields.title` | `string` | 分类名 JSONPath |
| `item_fields.url` | `string` | 分类/列表 URL JSONPath |
| `item_fields.cover` | `string` | 封面 JSONPath |

</details>

<details>
<summary>4.1 search / detail / chapter / content 通用字段</summary>

| 键 | 类型 | 用途 |
|---|---|---|
| `url` | `string` | API URL（含 `{keyword}` / `{id}` 占位） |
| `method` | `enum` | GET / POST |
| `params` | `object` | URL/Query 参数 |
| `headers` | `object` | 额外请求头 |
| `body` | `object\|string` | POST 体 |
| `response_path` | `string` | JSONPath 定位数据 |
| `field_extractors` | `object` | 字段映射（目标字段 → JSONPath） |
| `field_renames` | `object` | 字段重命名（search / detail 用） |
| `field_defaults` | `object` | 字段默认值（search 用） |
| `field_filters` | `object` | 字段值过滤 regex（search 用） |

</details>

<details>
<summary>4.2 playurl（video 专用）</summary>

| 键 | 类型 | 用途 |
|---|---|---|
| `url` | `string` | 视频源 API URL |
| `method` | `enum` | GET / POST |
| `params` | `object` | 参数（`bvid` / `cid` / `qn` / `fnval`） |
| `response_path` | `string` | JSONPath 定位 |
| `quality_param` | `string` | 画质参数名 |
| `format_param` | `string` | 格式参数名（`fnval=16` = dash） |

</details>

---

## 5. render（SPA 站，全选填）

<details>
<summary>全部字段</summary>

| 键 | 类型 | 用途 |
|---|---|---|
| `engine` | `enum` | `static` / `playwright` / `pyppeteer` / `requests-html` |
| `wait_for.type` | `enum` | `selector` / `network_idle` / `timeout` / `dom_loaded` |
| `wait_for.value` | `string\|int` | selector 名 / 等待秒数 |
| `wait_for.timeout_ms` | `int` | 最长等待 ms（默认 8000） |
| `scroll.enabled` | `bool` | 是否启用无限滚动 |
| `scroll.max_scrolls` | `int` | 最多滚多少页 |
| `scroll.scroll_delay_ms` | `int` | 滚动间隔 |
| `scroll.scroll_step_px` | `int` | 每次滚动距离 |
| `scroll.to_bottom_until.selector` | `selector` | 滚到这个 selector 出现 |
| `scroll.to_bottom_until.max_idle_scrolls` | `int` | 无新内容滚动次数上限 |
| `execute_script` | `string` | 提取前执行 JS（拿 `window.__INITIAL_STATE__`） |
| `screenshot` | `bool` | 是否截图（调试 selector） |
| `engine_args.chromium_args` | `array<string>` | 传给 chromium 的参数 |
| `engine_args.user_data_dir` | `string` | 持久化浏览器数据目录 |
| `engine_args.proxy_via_browser` | `string` | 浏览器层代理 |
| `engine_args.locale` | `string` | 浏览器语言 |
| `engine_args.timezone` | `string` | 浏览器时区 |
| `engine_args.viewport` | `object` | 视口大小 |
| `engine_args.fingerprint` | `object` | 指纹伪装 |

**当前框架实际实现（render_config，content.page.render = "playwright" 时生效）**：

| 键 | 类型 | 默认 | 用途 |
|---|---|---|---|
| `wait_for` | `string` | `"canvas"` | 渲染目标选择器（等它出现再提取） |
| `wait_until` | `enum` | `"domcontentloaded"` | 页面就绪策略：`domcontentloaded` / `networkidle` / `load` |
| `extra_delay_ms` | `int` | `2500` | JS 绘制后的额外等待（ms） |
| `timeout_ms` | `int` | `30000` | 页面加载超时（ms） |
| `click_selector` | `string` | 无 | 渲染后点击该元素（展开弹层/触发更多内容） |
| `scroll_to_bottom` | `bool` | `false` | 是否滚动到页底触发懒加载 |
| `extract_mode` | `enum` | `"canvas"` | 提取方式：`canvas`（加密分片图 base64）/ `img`（图片 URL）/ `text`（JS 渲染文本） |
| `page_container_selector` | `string` | 无 | 页容器选择器（如 `div.cropped`），用于统计期望页数并等待全部绘制 |
| `scroll_step_px` | `int` | `600` | 滚动步长（懒加载画布，如 comicbox 用 900 触发绘制） |
| `scroll_stale_rounds` | `int` | `6` | 连续几轮无新内容判定到底 |
| `proxy`（transports） | `string` | 自动探测 | 浏览器代理，缺省读系统代理（Clash 7890） |

> **「边滚边收集」说明**：部分漫画站（如 comicbox）的 canvas 是"视口内复用"的——滚出视口即被 JS 回收。框架采取**滚动过程中逐步提取**（而非滚动完再一次性提取），并在滚到底后深等（等尾部 canvas 集中绘制）再补一轮，保证长章节图片爬全。

示例：
```json
"content": {
  "page": {
    "render": "playwright",
    "render_config": {
      "wait_for": "canvas",
      "wait_until": "domcontentloaded",
      "extra_delay_ms": 3000,
      "scroll_to_bottom": true,
      "extract_mode": "canvas",
      "page_container_selector": "div.cropped"
    }
  }
}
```

</details>

---

## 6. decryption（加密站必填）

**必填**：

| 键 | 类型 | 用途 |
|---|---|---|
| `targets.*.strategy` | `enum` | 解密策略 `xor` / `aes_cbc` / `aes_ecb` / `rsa` / `custom_endpoint` |

**选填**：

<details>
<summary>targets 子键（content / image / video_url / chapter_url 各自一套）</summary>

| 键 | 类型 | 用途 |
|---|---|---|
| `.key` | `string` | AES / XOR 密钥 |
| `.iv` | `string` | AES CBC 初始向量 |
| `.mode` | `enum` | CBC / ECB |
| `.key_path` | `string` | 从响应动态取 key（JSONPath） |
| `.iv_path` | `string` | 从响应动态取 iv |
| `.endpoint` | `string` | 自建解密服务 URL（`custom_endpoint` 策略用） |
| `.request_field` / `request_fields` | `string\|object` | 请求字段名 |
| `.response_field` | `string` | 响应字段名 |
| `.fallback_endpoint` | `string` | 备用解密服务 |

</details>

<details>
<summary>cache 子键</summary>

| 键 | 类型 | 用途 |
|---|---|---|
| `cache.enabled` | `bool` | 是否缓存解密结果 |
| `cache.ttl_seconds` | `int` | 缓存过期秒数 |
| `cache.key_template` | `string` | 缓存 key 模板 |

</details>

---

## 7. 高级选项（全部选填）

<details>
<summary>展开 6 个子块（constraints / diagnostics / media / auth / ad_block / $metadata）</summary>

**7.1 `constraints`（爬取上限）**：`search.max_pages`(3) / `search.max_results`(20) / `search.max_concurrency`(4) / `detail.max_pages`(1) / `detail.timeout_per_page_sec`(15) / `chapter|page|episode.max_pages`(50) / `max_items`(null=无限) / `max_concurrency`(4) / `global.total_timeout_sec`(3600)

**7.2 `diagnostics`（自检）**：`selfcheck.strategy`(`off`/`soft`/`strict`，默认 `soft`) / `selfcheck.selector` / `selfcheck.interval_hours`(24) / `selfcheck.max_failures`(3) / `selfcheck.auto_disable`(true)

**7.3 `media`（video 必填）**：必填 `format`(`hls`/`dash`/`mp4`/`raw`)；`select.video.{quality,codecs,bitrate_max}` / `select.audio.{quality,lang,bitrate_min}`；`merge.{tool,output_format,extra_args}`(`ffmpeg`/`mp4box`/`none` + `mp4`/`mkv`/`ts`)；`subtitle.{lang_priority,format,embed}`(`srt`/`ass`/`vtt`)；`hls.workers`

**7.4 `auth`（登录源）**：`login_required` / `cookie_check.{field(例 SESSDATA),min_length,on_expired(warn/error/auto_refresh)}` / `cookie_refresh.{url,match(regex),interval_hours}` / `csrf.{enabled,header,meta_selector}` / `po_token.{enabled,endpoint,fallback}`（YouTube 2024 必需）

**7.5 `ad_block`（广告拦截，源级补充规则）**：`enabled` / `extra_css`(`[".ads"]`，追加到全局该类型规则) / `block_domains`（支持 `*.xxx.com`，追加到全局域名黑名单） / `block_url_regex`（追加到全局正则） / `allow_cookies_from`(白名单) / `stats`(拦截计数)

> 设计对齐 core.md 的 adblock 引擎：**全局规则库（rules_{novel,comic,video}.yaml）为主，源级 `ad_block` 只做补充追加**，不重复实现独立拦截逻辑。

**7.6 `$metadata`（站点名片）**：`homepage`(GUI 打开官网) / `icon`(favicon) / `description`(详情页) / `lang`(`zh-CN`/`zh-TW`/`en`/`ja`) / `region`(`cn`/`hk`/`tw`/`jp`/`en`) / `tags`(筛选) / `last_verified`(ISO8601)

</details>

---

## 8. URL 填写规则

### 8.1 绝对 URL vs 相对 URL

所有 `list_url` / `base_url` / `url` 字段支持两种形式：

| 写法 | 含义 | 示例 |
|---|---|---|
| 绝对 URL | 直接请求 | `"https://example.com/search?q={keyword}"` |
| 相对 URL | 自动拼接 `transports.base_url` | `"/search?q={keyword}"` → `https://example.com/search?q=...` |

> **推荐**：相对 URL + `transports.base_url`，换域名只改一处。

### 8.2 占位符（URL / params / body / query_template 中可用）

| 占位符 | 来源 | 典型场景 |
|---|---|---|
| `{keyword}` | 搜索关键词（必填，自动 URL encode） | search 阶段 |
| `{page}` | 当前页码（paginator 推进） | search / list 翻页 |
| `{id}` | 通用 ID | detail / content |
| `{chapter_id}` | 章节 ID | novel content |
| `{comic_id}` | 漫画 ID | comic content |
| `{bvid}` | B 站视频 BV 号 | video playurl |
| `{cid}` | B 站�集 ID | video playurl |
| `{videoId}` | YouTube 视频 ID | video playurl |

### 8.3 解密专用模板变量（在 `decryption.*.key` / `iv` / `endpoint` 中）

| 变量 | 来源 |
|---|---|
| `{{ auto.cipherKey }}` | 从最近响应取 cipherKey |
| `{{ auto.cipherIv }}` | 从最近响应取 cipherIv |
| `{{ auto.token }}` | 从最近响应取 token |
| `{{ auto.timestamp }}` | 当前时间戳 |

### 8.4 `query_template` vs `params` 两种写法

`endpoints.search` 有两种 URL 写法，二选一：

**A. `query_template` 字符串**（HTML 站用，手动放占位符）：

```text
GET  /search?q={keyword}&page={page}
POST /api/search  body: {"q": "{keyword}", "p": {page}}
```

**B. `params` 对象**（API 站用，结构化参数，自动 URL encode）：

```yaml
method: GET
base_url: /api/search
params:
  q: "{keyword}"
  page: "{page}"
extra_params:        # 固定附加参数
  from: "web"
```

### 8.5 解密端点 `endpoint` 怎么填

`decryption.targets.*.endpoint` 是自建解密服务 URL，支持三种调用模式：

| 模式 | 配置 | 行为 |
|---|---|---|
| **GET 转发** | `endpoint` + `request_fields` | 把 `request_fields` 拼成 query string 直接 GET |
| **POST 转发** | `endpoint` + `body_template`（future） | POST JSON，自动套用 `response_field` |
| **JS 解释器** | `endpoint: "js:..."` | 调用本地 JS 函数（个人用最强但慢） |

**示例**（包子漫画图片 URL 解密，自建代理）：

```yaml
decryption:
  targets:
    image:
      strategy: custom_endpoint
      endpoint: "http://127.0.0.1:8000/decrypt/image?cipher={cipher}"
      request_fields:
        cipher: "{auto.cipherText}"   # 从 response.body 取
      response_field: "url"            # 解密后从响应取 url 字段
```

---

## 9. 通用类型写法

### 9.1 selector

**单 selector**：

| 键 | 类型 | 用途 |
|---|---|---|
| `css` | `string` | CSS 选择器（推荐） |
| `xpath` | `string` | XPath 表达式 |
| `attr` | `string\|null` | 取属性（`src` / `href` / `text` / `html`，缺省取文本） |
| `attr_path` | `string` | 任意属性（如 `data-src`，用于懒加载图） |
| `exclude` | `array<selector>` | 排除这些子节点 |

**fallback 链**（多选择器依次尝试，第一个匹配且非空即返回）：

```yaml
selector:
  fallback:
    - css: "div.content p"
    - css: "article"
    - css: "#main"
```

### 9.2 paginator（翻页器）

| type | 用途 | 必填子键 |
|---|---|---|
| `increment` | URL 页码递增 `?page=N` | `start`（默认 1） / `step`（默认 1） / `param`（默认 `page`） |
| `next_link` | 抓"下一页"链接 | `selector`（下一页 URL） |
| `cursor` | 游标偏移 `?offset=N` | `param`（默认 `offset`） / `start` / `step` / `max`（可选硬上限） |
