# 源编辑器完整制作手册

> 适用项目：Claw 内容源系统
>
> 适用版本：Source Schema v2
>
> 适用对象：需要新增、修改、调试小说、漫画、视频源的开发者和维护者

## 1. 文档目的

本手册用于指导你从零制作一个可用内容源。内容覆盖：

- 如何判断站点是否适合接入；
- 如何抓取站点结构和识别反爬机制；
- 如何填写源编辑器中的每个字段；
- 哪些字段是必填，哪些字段是可选；
- CSS、XPath、属性提取、正则提取的写法；
- 小说、漫画、视频三种类型的差异；
- 搜索、发现、详情、章节、正文、图片和播放地址的配置；
- 加密正文、动态渲染、分页、懒加载、代理和登录态配置；
- 保存、测试、诊断、回归和上线流程；
- 常见失败原因与排查顺序。

源编辑器最终写入 `sources/*.json`。建议先使用界面生成配置，再根据需要手工补充高级字段。

## 2. 开始前需要准备什么

### 2.1 必须准备

| 项目 | 是否必需 | 说明 |
|---|---:|---|
| 站点首页地址 | 是 | 用于填写 `transports.base_url` |
| 内容类型 | 是 | `novel`、`comic`、`video` 三选一 |
| 一个可访问的作品详情页 | 是 | 用于测试详情字段和章节列表 |
| 一个可访问的章节/正文/播放页 | 是 | 用于测试实际内容解析 |
| 浏览器开发者工具 | 是 | 用于检查 HTML、请求、Cookie 和图片/播放地址 |
| 可复现的测试关键词 | 搜索源需要 | 用于测试搜索接口 |
| 可复现的分类页 | 发现功能需要 | 没有分类页可以不配置发现 |

### 2.2 建议准备

| 项目 | 是否必需 | 用途 |
|---|---:|---|
| Chrome 或 Chromium | 可选 | 检查动态渲染、懒加载和网络请求 |
| 站点登录账号 | 仅登录站需要 | 配置 Cookie 或登录态 |
| 站点的移动端/桌面端页面 | 可选 | 某些站两套页面结构不同 |
| 一部连载作品 | 强烈建议 | 验证最新章节、分页和缓存刷新 |
| 一部包含特别篇/预告的作品 | 可选 | 验证目录排序 |
| 一个包含多张图片的漫画章节 | 漫画源建议 | 验证全部图片是否提取 |

## 3. 源配置的最小骨架

### 3.1 小说最小骨架

```json
{
  "$schema_version": 2,
  "$id": "example_novel",
  "$type": "novel",
  "$name": "示例小说源",
  "$enabled": false,
  "transports": {
    "base_url": "https://example.com"
  },
  "endpoints": {
    "search": {
      "base_url": "/search",
      "keyword_param": "keyword",
      "item": {
        "root_selector": {"css": ".book-item"}
      }
    },
    "detail": {
      "fields": {
        "title": {"css": "h1"}
      }
    },
    "content": {
      "chapter": {
        "list": {
          "root_selector": {"css": ".chapter-item"},
          "fields": {
            "title": {"css": ".title"},
            "url": {"css": "a", "attr": "href"}
          }
        },
        "body": {
          "selector": {"css": ".chapter-content"}
        }
      }
    }
  }
}
```

### 3.2 漫画最小骨架

```json
{
  "$schema_version": 2,
  "$id": "example_comic",
  "$type": "comic",
  "$name": "示例漫画源",
  "$enabled": false,
  "transports": {
    "base_url": "https://example.com"
  },
  "endpoints": {
    "search": {
      "base_url": "/search",
      "keyword_param": "keyword",
      "item": {
        "root_selector": {"css": ".book-item"}
      }
    },
    "detail": {
      "fields": {
        "title": {"css": "h1"}
      }
    },
    "content": {
      "page": {
        "list": {
          "root_selector": {"css": ".chapter-item"},
          "fields": {
            "title": {"css": ".title"},
            "url": {"css": "a", "attr": "href"}
          }
        },
        "body": {
          "root_selector": {"css": ".comic-page img"},
          "fields": {
            "url": {"css": ".comic-page img", "attr": "src"}
          }
        }
      }
    }
  }
}
```

### 3.3 视频最小骨架

视频源除详情和分集列表外，还需要配置媒体信息。播放地址可能来自 HTML、JavaScript、JSON API、M3U8 或 MP4。

```json
{
  "$schema_version": 2,
  "$id": "example_video",
  "$type": "video",
  "$name": "示例视频源",
  "$enabled": false,
  "transports": {
    "base_url": "https://example.com"
  },
  "endpoints": {
    "search": {
      "base_url": "/search",
      "keyword_param": "keyword",
      "item": {
        "root_selector": {"css": ".video-item"}
      }
    },
    "detail": {
      "fields": {
        "title": {"css": "h1"}
      }
    },
    "content": {
      "episode": {
        "list": {
          "root_selector": {"css": ".episode-item"},
          "fields": {
            "title": {"css": ".title"},
            "url": {"css": "a", "attr": "href"}
          }
        },
        "play_url": {
          "selector": {"css": "video", "attr": "src"}
        }
      }
    }
  },
  "media": {
    "format": "hls",
    "select": {
      "video": {
        "quality": "best"
      }
    }
  }
}
```

## 4. 顶层字段说明

### 4.1 必填字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `$schema_version` | integer | 是 | 当前填写 `2` |
| `$id` | string | 是 | 源唯一 ID；建议使用小写字母、数字、下划线 |
| `$type` | enum | 是 | `novel`、`comic`、`video` |
| `$name` | string | 是 | 界面显示名称 |
| `transports` | object | 是 | 网络请求配置 |
| `endpoints` | object | 是 | 站点业务页面配置 |

### 4.2 可选字段

| 字段 | 类型 | 必填场景 | 说明 |
|---|---|---|---|
| `$enabled` | boolean | 正式启用时 | 新建草稿建议 `false`；默认可按项目规则处理 |
| `$weight` | number | 需要调整搜索排序时 | 搜索排序权重，通常 `0.0–10.0`；默认 `1.0` |
| `$metadata` | object | 无 | 站点描述、首页、语言、地区、标签、图标和 18+ 标记 |
| `api_endpoints` | object | API 站点 | 使用 JSON/API 替代 HTML 解析 |
| `render` | object | 动态页面 | 使用浏览器渲染或 Playwright |
| `decryption` | object | 加密正文/图片 | 配置解密策略 |
| `constraints` | object | 需要限制抓取时 | 限制页数、条数、并发和总超时 |
| `diagnostics` | object | 需要自检时 | 配置源健康检查 |
| `media` | object | 视频源 | 播放格式、画质和合并参数 |
| `auth` | object | 登录站点 | 登录态、Cookie 或授权配置 |
| `ad_block` | object | 需要广告过滤时 | 广告过滤配置 |

### 4.3 `$type` 对应的正文必填项

| 类型 | 必填正文块 |
|---|---|
| `novel` | `endpoints.content.chapter` |
| `comic` | `endpoints.content.page` |
| `video` | `endpoints.content.episode` 和 `media` |

## 5. 基本信息页

源编辑器的“基本信息”页用于填写源身份和展示信息。

| 编辑器字段 | JSON 路径 | 必填 | 填写说明 |
|---|---|---:|---|
| 源 ID | `$id` | 是 | 必须唯一；保存文件通常为 `sources/<id>.json` |
| 源类型 | `$type` | 是 | 决定“正文”页显示小说、漫画或视频字段 |
| 源名 | `$name` | 是 | 例如“瓜子漫画” |
| 官网 | `$metadata.homepage` | 可选 | 源管理中的官网入口 |
| 简介 | `$metadata.description` | 可选 | 一句话说明站点内容 |
| 标签 | `$metadata.tags` | 可选 | 界面筛选用；按逗号分隔 |
| 图标 URL | `$metadata.icon` | 可选 | 源管理显示的图标；留空使用默认图标 |
| 语言 | `$metadata.lang` | 可选 | 例如 `zh-CN` |
| 地区 | `$metadata.region` | 可选 | 例如 `cn`、`global` |
| 18+ 内容源 | `$metadata.adult` | 可选 | 成人内容源必须勾选；否则可能被内容过滤逻辑误判 |
| 权重 | `$weight` | 可选 | 搜索结果排序权重，通常保持 `1.0` |
| 启用状态 | `$enabled` | 正式使用时 | 草稿阶段建议关闭，完成测试后再启用 |

### 5.1 ID 命名规则

推荐：

```text
guazimanhua
comic_example
novel_site_01
```

不推荐：

```text
瓜子漫画
Comic Box
example.com
```

ID 一旦被收藏、阅读进度或缓存使用，后续不要随意修改，否则会被视为新源。

## 6. 网络页

“网络”页决定所有请求如何发送。它是排查“浏览器能打开、应用打不开”的第一位置。

| 编辑器字段 | JSON 路径 | 必填 | 建议 |
|---|---|---:|---|
| Base URL | `transports.base_url` | 是 | 站点根地址，例如 `https://example.com` |
| User-Agent | `transports.headers.User-Agent` | 可选 | 反爬站建议填写浏览器 UA |
| Referer | `transports.headers.Referer` | 可选 | 图片防盗链或站点反爬时常需要首页 Referer |
| Accept | `transports.headers.Accept` | 可选 | 通常可留空；图片源可使用 `image/avif,image/webp,*/*` |
| Accept-Language | `transports.headers.Accept-Language` | 可选 | 中文站可填 `zh-CN,zh;q=0.9` |
| Cookie | `transports.cookie` | 可选 | 登录、年龄验证或风控站点才填写；不要提交个人账号 Cookie |
| 跟随重定向 | `transports.follow_redirects` | 可选 | 默认开启 |
| 字符集 | `transports.charset` | 可选 | 中文乱码时填写 `utf-8` 或站点实际编码 |
| 超时 | `transports.timeout` | 可选 | 默认通常足够；慢站可适当增加 |
| 重试 | `transports.retries` | 可选 | 网络不稳定时增加；不要用过大值轰炸站点 |
| 请求间隔 | `transports.interval_ms` | 可选 | 反爬站可增加到 800ms 或更高 |
| 退避基础 | `transports.retry_backoff.base` | 可选 | 重试等待基础秒数 |
| 退避上限 | `transports.retry_backoff.max` | 可选 | 重试等待最大秒数 |
| 退避抖动 | `transports.retry_backoff.jitter` | 可选 | 避免固定间隔重复请求 |
| 代理 | `transports.proxy` | 可选 | 站点地域限制时使用；不要硬编码凭据 |
| 代理池 | `transports.proxy_pool` | 可选 | 仅在项目代理策略允许时配置 |
| TLS | `transports.tls` | 可选 | 仅遇到证书或指纹问题时配置 |

### 6.1 一键补全默认值

源编辑器的“一键补全默认值”只会补空字段，通常包括：

- 根据 Base URL 生成 Referer；
- 补充 `utf-8`；
- 补充通用 Accept；
- 补充中文 Accept-Language。

它不会覆盖已经填写的值。

### 6.2 如何判断是反爬问题

浏览器可以、应用不可以时，依次检查：

1. 请求 URL 是否完全一致；
2. User-Agent 是否为空或过旧；
3. Referer 是否需要站点首页或详情页；
4. 图片域名是否不同于主站域名；
5. 是否需要 Cookie；
6. 是否有 403、429、验证码、空 HTML 或伪装成功的 200 响应；
7. 是否必须执行 JavaScript 才能生成真实链接；
8. 是否存在懒加载 `data-src`、`data-original`、`data-url`；
9. 是否需要 Playwright 而不是普通 HTTP 请求。

## 7. 发现页

发现页是可选功能。没有稳定分类页时不要勉强配置，搜索和详情仍然可以单独使用。

| 字段 | JSON 路径 | 必填 | 说明 |
|---|---|---:|---|
| 启用发现 | `endpoints.discovery` / `_flag_discovery` | 可选 | 有分类页才启用 |
| 列表入口 URL | `endpoints.discovery.list_url` | 启用发现时必填 | 分类或作品列表入口 |
| 分类入口 URL | `endpoints.discovery.list_categories_url` | 可选 | 独立分类页入口 |
| 分页类型 | `endpoints.discovery.list_paginator.type` | 可选 | 常见为 `increment` |
| 页码参数 | `...list_paginator.param` | 分页时需要 | 例如 `page` |
| 起始页 | `...list_paginator.start` | 可选 | 通常为 `1` |
| 步长 | `...list_paginator.step` | 可选 | 通常为 `1` |
| 页码占位符 | `...list_paginator.page_placeholder` | 模板分页时需要 | 例如 URL 中的 `{page}` |
| 作品项根选择器 | `endpoints.discovery.works_list_item.root_selector` | 作品列表时建议 | 每个作品卡片的外层节点 |
| 作品字段 | `...works_list_item.fields.*` | 作品列表时需要 | 标题、详情 URL、封面等 |
| 封面渲染 | `...works_list_item.cover_render` | 动态封面时需要 | 普通页面留空，动态页面填 `playwright` |

### 7.1 作品字段

常用字段：

| 字段 | 作用 | 必填 |
|---|---|---:|
| `title` | 作品标题 | 建议必填 |
| `url` | 详情页 URL | 必填 |
| `cover` | 封面 URL | 可选 |
| `author` | 作者 | 可选 |
| `update` | 更新时间/最新章节 | 可选 |
| `status` | 连载/完结 | 可选 |
| `tags` | 题材标签 | 可选 |
| `duration` | 视频时长 | 视频源可选 |
| `views` | 播放/阅读量 | 可选 |

## 8. 搜索页

搜索配置决定用户输入关键词后如何请求和解析结果。

| 字段 | JSON 路径 | 必填 | 说明 |
|---|---|---:|---|
| 启用搜索 | `endpoints.search` / `_flag_search` | 搜索源必填 | 没有搜索接口可不启用 |
| 搜索 URL | `endpoints.search.base_url` | 是 | 例如 `/search` 或 `/category.php` |
| 关键词参数 | `endpoints.search.keyword_param` | GET 搜索必填 | 例如 `keyword`、`q` |
| 方法 | `endpoints.search.method` | 可选 | `GET` 或 `POST`；多数站使用 GET |
| 页码模板 | `endpoints.search.paginator.url_template` | 分页时需要 | 例如 `/search?q={keyword}&page={page}` |
| 结果项根选择器 | `endpoints.search.item.root_selector` | 是 | 每个搜索结果卡片外层节点 |
| 正文格式 | `endpoints.search.body_format` | POST 时需要 | 例如 `form` |
| 条目字段 | `endpoints.search.item.fields.*` | 标题/URL建议必填 | 同发现页字段 |
| 最大结果数 | `endpoints.search.max_results` | 可选 | 防止一次抓取过多 |
| 搜索渲染 | `endpoints.search.render` | 动态搜索时需要 | 需要浏览器执行 JS 时配置 |

### 8.1 搜索 URL 示例

如果浏览器地址变为：

```text
https://example.com/search?keyword=斗破苍穹&page=2
```

应该填写：

```json
{
  "base_url": "/search",
  "keyword_param": "keyword",
  "paginator": {
    "url_template": "/search?keyword={keyword}&page={page}"
  }
}
```

## 9. 详情页

详情页负责解析作品本身，不负责解析正文图片或正文文本。

| 字段 | JSON 路径 | 必填 | 说明 |
|---|---|---:|---|
| 标题选择器 | `endpoints.detail.fields.title` | 是 | 通常是 `h1` |
| 作者选择器 | `...fields.author` | 可选 | 作者节点 |
| 封面选择器 | `...fields.cover` | 可选 | 注意 `src`、`data-src`、`data-original` 的区别 |
| 简介选择器 | `...fields.summary` | 可选 | 简介正文 |
| 状态选择器 | `...fields.status` | 可选 | 连载/完结 |
| 标签选择器 | `...fields.tags` | 可选 | 题材标签，可多值 |
| 书名选择器 | `...fields.book_name` | 可选 | 标题被拆分时使用 |
| URL 模式 | `endpoints.detail.url_pattern` | 可选 | 对详情 URL 进行校验或补全 |
| URL 后缀 | `endpoints.detail.url_suffix` | 可选 | 少数站需要固定后缀 |
| 标题净化 | `endpoints.detail.fields.title_clean` | 可选 | 去掉“在线阅读”等噪声 |
| 字段清洗 | `endpoints.detail.fields.clean` | 可选 | 正则替换字段内容 |
| gallery | `endpoints.detail.fields.gallery` | 可选 | 详情页内嵌图集图片 |
| meta | `endpoints.detail.fields.meta` | 可选 | 自定义额外元数据 |

## 10. 正文页：小说

小说源使用 `endpoints.content.chapter`。

| 字段 | JSON 路径 | 必填 | 说明 |
|---|---|---:|---|
| 章节项根选择器 | `...chapter.list.root_selector` | 是 | 目录中每个章节的外层节点 |
| 章节标题 | `...chapter.list.fields.title` | 是 | 从章节项提取标题 |
| 章节 URL | `...chapter.list.fields.url` | 是 | 通常取 `href` 属性 |
| 章节顺序 | `...chapter.list.chapter_order` | 可选 | `asc`、`desc`；项目还会进行全局数字排序 |
| 标题净化 | `...chapter.list.title_clean` | 可选 | 清理书名、后缀和噪声 |
| 目录分页 | `...chapter.list.paginator` | 可选 | 目录跨页时配置 |
| 独立目录 URL | `...chapter.list.chapters_url` | 可选 | 详情页没有完整目录时使用 |
| 正文选择器 | `...chapter.body.selector` | 是 | 正文容器或正文段落 |
| 正文属性 | `...chapter.body.attr` | 可选 | 正文在属性中时填写 |
| 正文分页 | `...chapter.body.paginator` | 可选 | 一章跨多个 HTML 页面时配置 |
| 正文过滤 | `...chapter.body.filter` | 可选 | 移除广告、导航和噪声 |

### 10.1 小说正文选择器注意事项

优先选择正文容器，而不是整个 `body`：

```text
推荐：#content
推荐：article.chapter-content
推荐：div.read-content
不推荐：body
不推荐：main（可能包含推荐和广告）
```

如果正文每段都是 `<p>`，可以配置段落选择器，但必须确认解析器支持该结构并保持换行。

### 10.2 目录排序

项目会综合处理：

- `chapter_order` 的站点原始顺序；
- 普通数字章节；
- 中文数字、全角数字、罗马数字；
- `开始阅读` 导航入口；
- 预告、序章、楔子；
- 特别篇和番外。

如果站点目录中有“从第一章开始阅读”这类导航链接，必须保留其真实 URL；不要把它伪装成普通章节 URL。

## 11. 正文页：漫画

漫画源使用 `endpoints.content.page`。

| 字段 | JSON 路径 | 必填 | 说明 |
|---|---|---:|---|
| 章节项根选择器 | `...page.list.root_selector` | 有章节目录时必填 | 章节链接外层节点 |
| 章节标题 | `...page.list.fields.title` | 有章节目录时建议 | 根节点本身是 `<a>` 时使用根节点读取 |
| 章节 URL | `...page.list.fields.url` | 有章节目录时必填 | 通常为 `href` |
| 章节顺序 | `...page.list.chapter_order` | 可选 | 原始目录顺序提示 |
| 图片根选择器 | `...page.body.root_selector` | 是 | 每张图片或图片容器 |
| 图片 URL | `...page.body.fields.url` | 是 | `src`、`data-src` 或 `data-original` |
| 详情即图片页 | `...page.single_chapter` | 图集站需要 | 没有章节列表时启用 |
| 动态渲染 | `...page.render` | JS/Canvas 站需要 | 普通 HTML 图片不要启用 |
| 渲染配置 | `...page.render_config` | 动态渲染时需要 | 等待、滚动、提取方式等 |

### 11.1 图片懒加载

常见图片地址字段：

```text
src
 data-src
data-original
data-lazy-src
data-url
```

必须通过浏览器开发者工具确认页面真正使用的属性。只填写 `src` 可能得到占位图或空白图。

### 11.2 图片根节点是当前节点时

如果根选择器已经选中了 `<img>` 本身，不要再写一个内部 `img` 选择器。应直接读取当前节点属性：

```json
{
  "root_selector": {"css": "main img.comic-page"},
  "fields": {
    "url": {"xpath": ".", "attr": "src"}
  }
}
```

如果根选择器选中的是外层卡片，则从子元素读取：

```json
{
  "root_selector": {"css": ".comic-page"},
  "fields": {
    "url": {"css": "img", "attr": "data-src"}
  }
}
```

### 11.3 Canvas、虚拟列表和动态漫画

如果浏览器中没有真实 `img.src`，而是通过 Canvas 绘制：

1. 先确认页面是否需要滚动才触发加载；
2. 确认 Canvas 是否会被虚拟列表回收；
3. 确认图片是否需要点击或等待 JS；
4. 使用 `render: playwright`；
5. 配置 `wait_for`、`extra_delay_ms`、`scroll_to_bottom`、`extract_mode`；
6. 逐页验证抓取数量和图片可解码性；
7. 不要把空白 Canvas 写入缓存。

## 12. 正文页：视频

视频源使用 `endpoints.content.episode`。

| 字段 | JSON 路径 | 必填 | 说明 |
|---|---|---:|---|
| 集项根选择器 | `...episode.list.root_selector` | 是 | 每集外层节点 |
| 集标题 | `...episode.list.fields.title` | 是 | 集标题 |
| 集 URL | `...episode.list.fields.url` | 是 | 播放页 URL |
| 播放地址选择器 | `...episode.play_url.selector` | HTML 播放地址时需要 | 例如 `video`、播放器节点 |
| 播放地址属性 | `...episode.play_url.selector.attr` | 可选 | 默认 `href` 或 `src` |
| 播放地址正则 | `...episode.play_url.regex` | JS 内嵌地址时需要 | 从 HTML/脚本提取 M3U8/MP4 |
| 地址后缀 | `...episode.play_url.suffix` | 可选 | 站点需要固定后缀时填写 |
| 单集作品 | `...episode.single_chapter` | 无分集列表时需要 | 单集视频勾选 |
| 分季 | `...episode.series` | 多季视频时可选 | 配置季列表和最少季数 |
| 换源 | `...episode.source_switch` | 多播放源站可选 | 例如 `sid` 参数切换 |

### 12.1 media 配置

| 字段 | JSON 路径 | 必填 | 说明 |
|---|---|---:|---|
| 媒体格式 | `media.format` | 视频源建议 | `hls`、`dash`、`mp4`、`raw` |
| 画质 | `media.select.video.quality` | 可选 | `best`、`1080p`、`720p` 等 |
| 合并工具 | `media.merge.tool` | 分段下载时需要 | 通常 `ffmpeg` |
| 输出格式 | `media.merge.output_format` | 合并时可选 | `mp4`、`mkv` |

## 13. 选择器写法

### 13.1 CSS 选择器

```json
{"css": "h1"}
{"css": ".book-card a.title"}
{"css": "article.card:nth-of-type(1)"}
```

适合结构稳定、选择器简洁的站点。

### 13.2 XPath

```json
{"xpath": "//h1"}
{"xpath": ".//a[contains(@href, '/chapter.php')]"}
{"xpath": ".", "attr": "href"}
```

在根节点本身取属性时，优先使用：

```json
{"xpath": ".", "attr": "href"}
```

### 13.3 属性提取

```json
{"css": "img.cover", "attr": "src"}
{"css": "img.cover", "attr": "data-src"}
{"css": "a.chapter", "attr": "href"}
```

### 13.4 正则提取

适合从 `onclick`、内嵌 JSON、脚本字符串中提取地址。只有普通 CSS/XPath 无法取到时才使用。

### 13.5 Base64 解码

部分站点将跳转地址编码在属性中。使用编辑器支持的 `b64decode` 选择器字段时，必须先确认编码确实是 Base64，不要盲目启用。

## 14. 动态渲染与反爬配置

### 14.1 什么时候需要 Playwright

以下情况通常需要动态渲染：

- HTML 初始内容为空；
- 章节或图片由 JavaScript 生成；
- 图片实际绘制在 Canvas；
- 必须滚动后才生成图片；
- 页面需要点击按钮才能展开内容；
- 关键数据只存在 `window.__INITIAL_STATE__` 或脚本中。

### 14.2 常见 render_config 字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `wait_for` | 动态渲染时建议 | 等待的选择器，例如 `canvas` |
| `wait_until` | 可选 | `domcontentloaded` 或 `networkidle` |
| `timeout_ms` | 可选 | 页面加载超时 |
| `extra_delay_ms` | 动态站建议 | 页面加载后额外等待 |
| `click_selector` | 可选 | 点击展开按钮 |
| `scroll_to_bottom` | 懒加载时需要 | 滚动触发更多内容 |
| `extract_mode` | 可选 | `img`、`canvas`、`text` |
| `page_container_selector` | 虚拟列表时需要 | 每页容器选择器 |
| `scroll_stale_rounds` | 可选 | 连续无新增页面的容错轮数 |
| `min_images` | 漫画动态源可选 | 少于此数量不写入持久缓存 |

### 14.3 反爬排查顺序

1. 用浏览器打开详情页和正文页；
2. 记录最终 URL、重定向、Cookie、Referer；
3. 检查图片是否来自独立 CDN；
4. 检查 CDN 是否要求首页 Referer；
5. 对比浏览器和普通 HTTP 请求的状态码和响应头；
6. 判断是否为 403、429、验证码、空白 200 或伪装成功页面；
7. 增加合理间隔和退避，不要无限重试；
8. 必要时使用 Playwright；
9. 仍失败时配置代理或 Cookie，但不要提交个人凭据。

## 15. 加密和解密

只有确认正文或图片确实加密后才配置 `decryption`。

常见目标：

```text
image：图片地址或图片内容
content：小说正文
chapter：章节标题或章节数据
```

常见策略：

- Base64；
- URL-safe Base64；
- XOR；
- AES-CBC；
- AES-ECB；
- 字体映射；
- 自定义接口；
- 自定义 JS。

### 15.1 不要把什么写入源文件

- 个人账号密码；
- 私人 Cookie；
- 私有 API Token；
- 代理用户名和密码；
- 仅属于个人账户的授权信息。

## 16. 分页、排序和特殊章节

### 16.1 目录分页

目录跨页面时配置 `list.paginator` 或 `chapters_url`。必须确认：

- 第 1 页是否从 0 或 1 开始；
- 下一页参数是 `page`、`p`、`offset` 还是 URL 模板；
- 页码是否倒序；
- 空页是否代表结束；
- 是否存在重复章节。

### 16.2 章节排序

项目支持：

- 阿拉伯数字：`第12话`；
- 中文数字：`第十二话`；
- 全角数字；
- 罗马数字；
- `Chapter 5`、`Ch.5`、`Vol.2`；
- `开始阅读`导航入口；
- 预告、序章、楔子；
- 特别篇和特別篇。

当前规则：

- 普通章节按数字排序；
- 预告/序章是否在前，参考站点原始目录首次出现位置；
- `开始阅读`按第 1 章入口处理；
- 标题包含“特别篇”或“特別篇”的项目统一放到目录最后；
- 同类项目保持原始稳定顺序。

## 17. API 站点

如果 HTML 页面没有数据，但开发者工具中可以看到 JSON 请求，应优先考虑 `api_endpoints`。

需要记录：

- API URL；
- 请求方法；
- Query 参数；
- POST body；
- 返回 JSON 路径；
- 章节 ID 字段；
- 详情字段；
- 分页字段；
- Token、签名和时间戳要求。

API 配置不是 HTML 选择器的替代品，不能把 API 字段路径写成 CSS 选择器。

## 18. 保存、测试和启用流程

### 18.1 推荐流程

```text
1. 源管理 → 添加源
2. 选择 novel / comic / video
3. 填基本信息和 Base URL
4. 填详情页必需字段
5. 填正文类型对应字段
6. 保存为草稿（$enabled=false）
7. 测试详情
8. 测试搜索
9. 测试章节列表
10. 测试正文/图片/播放地址
11. 检查排序、分页、封面和状态
12. 运行相关回归测试
13. 确认没有个人凭据
14. 将 $enabled 改为 true
15. 重启应用做最终验证
```

### 18.2 必须验证的结果

#### 小说

- 搜索能返回标题和详情 URL；
- 详情能返回标题；
- 作者、封面、简介不应错位；
- 章节列表数量合理；
- 第一章、最后一章和连载最新章均可打开；
- 正文没有广告、导航和推荐污染；
- 预告、特别篇、番外排序符合规则；
- 更新中的作品重新打开能够刷新目录。

#### 漫画

- 章节目录完整；
- 每章图片数量合理；
- 第一张和最后一张图片都能解析；
- `src`/`data-src` 选择正确；
- Canvas/懒加载图片不会只抓到首屏；
- 图片 data URI 或直链能够被 Qt 解码；
- 失败或残缺结果不应写入缓存。

#### 视频

- 集数完整；
- 播放地址真实可访问；
- M3U8、MP4、DASH 类型与 media 配置一致；
- 画质选择和换源不产生错误 URL；
- 下载和在线播放分别验证。

## 19. 常见错误

| 现象 | 常见原因 | 处理 |
|---|---|---|
| 源无法保存 | `$id`、`$type`、`$name` 或必需正文字段缺失 | 先补顶层和类型必填字段 |
| 搜索无结果 | 搜索 URL、关键词参数或结果根节点错误 | F12 查看真实请求和结果卡片 |
| 详情标题为空 | `h1` 选择器不对或页面需要 JS | 检查 HTML 初始内容，必要时启用渲染 |
| 封面为空 | 使用了 `src`，实际地址在 `data-src` | 修改属性字段 |
| 章节为 0 | 根选择器指向容器而非章节项，或章节由 JS 生成 | 检查根节点和动态渲染 |
| 第一章在最后 | 标题没有识别出数字或被导航链接污染 | 检查开始阅读和章节标题清洗 |
| 特别篇插入普通章节中间 | 未应用全局特别篇排序 | 检查标题是否包含“特别篇” |
| 正文为空 | 正文选择器错误、正文加密或需要分页 | 检查正文容器、解密和 body paginator |
| 漫画只有两张图 | 虚拟 Canvas、懒加载、提前停止或空 Canvas 被缓存 | 启用渲染、逐页滚动、过滤残缺缓存 |
| 图片 403 | Referer、Cookie、UA 或 CDN 防盗链 | 对比浏览器请求头 |
| 视频能打开但无法播放 | 取到的是播放页而不是媒体 URL | 配置 play_url selector/regex |
| 保存后源不显示 | `$enabled=false` 或成人源被内容过滤 | 检查启用状态和 `$metadata.adult` |
| 改了配置但没效果 | 旧进程仍在运行或加载了旧源 | 关闭所有 Python 进程后重启应用 |

## 20. 上线前检查清单

### 必填检查

- [ ] `$schema_version` 为 `2`；
- [ ] `$id` 唯一且符合命名规范；
- [ ] `$type` 正确；
- [ ] `$name` 已填写；
- [ ] `transports.base_url` 可访问；
- [ ] 搜索配置完整，或明确说明该源不支持搜索；
- [ ] 详情标题选择器有效；
- [ ] 类型对应的正文块完整；
- [ ] 正文/图片/播放地址实测成功；
- [ ] `$enabled` 状态符合当前阶段。

### 可选项检查

- [ ] `$metadata.homepage`；
- [ ] `$metadata.description`；
- [ ] `$metadata.tags`；
- [ ] `$metadata.lang`；
- [ ] `$metadata.region`；
- [ ] `$metadata.adult`；
- [ ] `discovery`；
- [ ] `constraints`；
- [ ] `diagnostics`；
- [ ] `render_config`；
- [ ] `decryption`；
- [ ] `auth`；
- [ ] `proxy_pool`；
- [ ] `ad_block`；
- [ ] `media`。

### 安全检查

- [ ] 没有提交 Cookie；
- [ ] 没有提交登录密码；
- [ ] 没有提交 Token；
- [ ] 没有提交代理凭据；
- [ ] 没有把私人 API 写入源配置；
- [ ] 没有绕过站点权限或付费限制；
- [ ] 请求间隔和重试次数合理；
- [ ] 失败时不会无限请求。

## 21. 相关文件

| 文件 | 用途 |
|---|---|
| `docs/source-schema-v2.md` | Schema v2 字段速查 |
| `docs/ui-editor.md` | 源编辑器功能和界面说明 |
| `gui/components/source_editor.py` | 源编辑器实际实现 |
| `gui/components/editor_grids.py` | 选择器表格和键值表格控件 |
| `gui/components/source_presets.py` | 默认模板、网络默认值、解密/渲染预设 |
| `framework/config.py` | 源配置加载、校验和 SourceConfig |
| `framework/parser.py` | CSS/XPath/属性/正则解析 |
| `framework/content.py` | 搜索、详情、章节、正文和图片抓取 |
| `framework/http.py` | 网络请求、重试、代理和请求头 |
| `sources/*.json` | 实际源配置文件 |
| `tests/test_*source*.py` | 源配置和解析回归测试 |

## 22. 最简判断标准

一个源只有在以下条件全部满足时，才应标记为启用：

1. 可以通过源 ID 加载；
2. 详情页能拿到正确标题；
3. 搜索或发现至少有一种入口能返回作品；
4. 章节列表不是空列表；
5. 第一章和最新章可打开；
6. 正文、漫画图片或视频地址可用；
7. 失败不会把空内容写入缓存；
8. 站点更新后可以重新获取最新目录；
9. 没有硬编码个人凭据；
10. 相关测试和 `compileall` 通过。
