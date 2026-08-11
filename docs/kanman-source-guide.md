# 看漫画 kanman.com 手动制作源填写指南

> 调研日期：2026-08-10 · 已全链路实测（列表/详情/章节/正文/图片/CDN 防盗链）
> 适用：claw「添加源/编辑源」界面（`gui/components/source_editor.py`），按 **12 个 Tab 自上而下**填写；也可对照 §13 参照 JSON 直接落盘。

---

## 0. 站点机制真相（先读，决定怎么填）

| 环节 | 真相 | 对制源的影响 |
|---|---|---|
| 列表首页 `/sort/` | **纯 JS 渲染**：HTML 无列表项，页面 JS 调 `/api/getComicList/` 一次性拉全量，本地切页/过滤/搜索 | 发现必须走 **JSON API（API接口 Tab）**，不能走 HTML 选择器（发现 Tab） |
| 列表接口 `/api/getComicList/` | `?product_id=1&productname=kmh&platformname=pc` 返回 `{"status":0,"data":[115部]}`。**所有附加参数（page/分类/排序）都被忽略**，始终返回同一全量 | 发现**不要配 `{page}`**，第 1 页就是全站 |
| 搜索 | 没有服务端搜索接口，搜索框回车跳到 `/sort/#关键词` 本地过滤 | **不配搜索**（无服务端 URL 可用） |
| 详情页 `/17745/` | 服务端渲染。标题/简介/封面在 `meta[property="og:*"]`；章节在 `#detail-chapter li.item > a.last-update`（660 章全量 DOM，无需点击展开） | 详情、章节列表都可直接 HTML 抓 |
| 章节页 `/17745/1004.html` | 图片 URL 明文内嵌在 `<script>`：`window.comicInfo.current_chapter.chapter_img_list`（数组，带 `?auth_key=` 签名） | 正文必须用 **playwright + img_js_path** 取全局数组 |
| 图片 CDN | `hw-chapter2.kaimanhua.com` 返回**明文 JPEG**（无加密）；`image.yqmh.com` 封面同理 | **解密 Tab 无需配置**；但两张 CDN 都校验浏览器图片请求头 |
| 防盗链 | 图片请求必须带完整头（UA+Referer+Accept+`Sec-Fetch-Site/Mode/Dest`），缺任一带的会 404 | 网络 Tab 的请求头必须完整填（见 §2） |

一句话结论：**列表用接口、详情/章节用 HTML、正文用 Playwright 取 JS 全局数组、图片明文不带解密，传输头要带全。**

---

## 1. 顶部栏 + 基本信息 Tab

**顶部栏**（表单最上方，非页签内）：

| 控件 | 填写 |
|---|---|
| ID（sources id） | `kanman` |
| 类型 | `comic` |

**基本信息 Tab**（决定展示与筛选）：

| 控件 | 填写 |
|---|---|
| 源名 | `看漫画(KanMan)` |
| 官网 | `https://www.kanman.com/` |
| 简介 | 一句话站点说明（可留空） |
| 标签 | `漫画, 免费, 在线`（逗号分隔） |
| 图标 URL | 留空（用默认） |
| 语言 | `zh-CN` |
| 地区 | `cn` |
| 权重 | `1.0`（默认即可，搜索排序权重） |

---

## 2. 网络 Tab

「Base URL」以下每行对应一个请求头/参数。建议先点 **「一键补全默认值」** 按钮，再把防盗链三件套补上：

| 控件 | 填写 |
|---|---|
| Base URL | `https://www.kanman.com` |
| User-Agent | `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36` |
| Referer | `https://www.kanman.com/` |
| Accept | `text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8` |
| Accept-Language | `zh-CN,zh;q=0.9` |
| Cookie | 留空 |
| 跟随重定向 | 勾选（默认） |
| 字符集 | 留空（自动探测） |
| 超时(秒) | `12` |
| 重试 | `3` |
| 请求间隔(ms) | `600` |
| 重试退避(基础/上限/抖动) | 默认（1 / 10 / 0.3） |

> 防盗链关键：图片 CDN 长链还要求 `Sec-Fetch-Site= cross-site`、`Sec-Fetch-Mode= no-cors`、`Sec-Fetch-Dest= image`。这三个头表单无输入框，需在保存后的 JSON 里补到 `transports.headers`（见 §13 参照 JSON）。实际已验证：不带这三件套 + Referer 请求 `image.yqmh.com/mh/17745.jpg` → **404**；带全 → **200 (image/png)**。

---

## 3. 发现 Tab（HTML 发现）

**全部留空、不勾「启用发现」**——这不影响找内容。

「启用发现」勾选框（`_flag_discovery`）只控制**本 Tab 的 HTML 规则**是否写入 `endpoints.discovery`。kanman 的发现走的是 §10 API接口 Tab 的 JSON API 配置（`api_endpoints.discovery`），该配置**独立保存、不受此勾选影响**；框架只要有其中任一存在就认为该源可发现（`has_discovery()`）。所以：来源照常出现在发现页、能翻全站列表，只是 HTML 规则没用而已。

HTML 发现区所有控件都不要填，尤其：
- 列表入口 URL / 分类入口 URL / 分页方式 / 参数名 / 起始页 / 每页条数 / 页码占位符：留空
- 分类条目字段 / 作品列表条目字段：留空（`/sort/_p2.html` 等 URL 虽 200，但 HTML 里仍无列表项，是 JS 渲染，抓不到）
- 封面渲染：留空

---

## 4. 搜索 Tab

**不勾「启用搜索」**：本站无服务端搜索接口，搜索框回车是本地过滤，无 URL 可用。

> 「启用搜索」同样只控制 HTML 搜索规则（`endpoints.search`）。kanman 不配搜索，无论勾不勾都没有可用 URL，全局搜索会跳过本来源——这不是缺陷，是站点本身没有服务端搜索能力。找内容靠 §10 的发现接口（全站 115 部都能在发现页翻到）。

---

## 5. 详情 Tab

| 控件 | 填写 |
|---|---|
| 标题选择器 | `meta[property="og:title"]` |
| 作者选择器 | 留空（详情页无独立作者 DOM） |
| 封面选择器 | `meta[property="og:image"]` |
| 简介选择器 | `meta[property="og:description"]` |
| 状态选择器 | 留空（更新信息带日期，未给稳定选择器） |
| 标签选择器 | `.tags a.tag` |
| 书名选择器 | 留空 |
| 详情 URL 模板 | `/\d+/`（详情页地址规则，即 URL 匹配） |
| 详情 URL 后缀 | 留空 |
| 标题净化 | 留空 |

> `meta[property="og:*"]` 需要 `attr: content` 才能取到值。详情 Tab 的输入框只保存 css；`attr` 无法从本页选项卡直接填写，须取 `og:title`/`og:image`/`og:description` 的三个字段在保存后的 JSON 里补 `"attr": "content"`（见 §13）。或直接落盘参照 JSON，一步到位。

---

## 6. 正文 Tab（漫画 → page 块）

| 控件 | 填写 |
|---|---|
| 单图项选择器 | `#detail-chapter li.item` |
| 图片 URL 选择器 | `a.last-update` |
| 目录页 URL | 留空（目录即详情页） |
| 章节排序 | `asc`（默认） |
| 标题净化 | 留空 |
| 详情页即图片页 | **不勾**（有章节列表） |

**JS 渲染配置组**（点渲染方式选 `playwright` 后展开）：

| 控件 | 填写 |
|---|---|
| 渲染方式 | `playwright` |
| 提取方式 | `img` |
| 图片选择器 | 留空 |
| 图片 JS 路径 | `window.comicInfo.current_chapter.chapter_img_list` |
| 页面容器选择器 | 留空 |
| 等待元素 | 留空 |
| 就绪策略 | `domcontentloaded` |
| 超时(ms) | `30000` |
| 额外延迟(ms) | `1500` |
| 滚动到底 / 滚轮滚动 | 不勾 |
| 滚动步长(px) / 滚动重试轮 | 默认 |

> 原理：Playwright 加载章节页后，同步脚本已把整话 URL 数组写进 `window.comicInfo.current_chapter.chapter_img_list`，`img_js_path` 一次性求值即得全部 66 张，不用等懒加载、不用滚动。
>
> 章节列表 `fields.url` 需要 `"attr": "href"` 才能取到链接而非文字；与详情 Tab 相同，`attr` 需在保存后的 JSON 补（见 §13）。

---

## 7. 限制 Tab

保持默认即可（该 Tab 控制抓取量，防刷爆站点）：

| 控件 | 建议 |
|---|---|
| 搜索最大页 / 搜索最大结果 | 默认 |
| 详情最大页 | 默认 1 |
| 正文最大页 | 默认 50（660 章足够） |
| 正文最大条 | 默认 500 |
| 最大并发 / 详情单页超时 / 全局抓取超时 | 留默认 |

---

## 8. 诊断 Tab

| 控件 | 填写 |
|---|---|
| 自检策略 | `soft`（站点改版黄灯提醒，不影响使用） |
| 自检选择器 | `#detail-chapter` 或 `meta[property="og:title"]` |
| 连续失败自动禁用 | 不勾（慎用） |
| 失效判定次数 / 自检间隔(小时) | 默认 |

---

## 9. 广告过滤 Tab

| 控件 | 填写 |
|---|---|
| 启用广告过滤 | 可关（保守，manwa 先例）；kanman 详情页有站内广告但不影响正文提取，此项不影响「成功制源」 |
| URL 广告正则 / 广告域名 | 留空 |

---

## 10. API接口 Tab → 发现（Discovery）

本源发现的关键。只填「JSON API · 发现」这一组，其余（搜索/详情/播放）留空：

| 控件 | 填写 |
|---|---|
| 引擎 | 留空（默认 JSON） |
| 接口 URL | `/api/getComicList/` |
| 兜底 URL | 留空 |
| 响应路径 | `data` |
| 兜底响应路径 | 留空 |
| 请求参数 | `product_id`=`1`、`productname`=`kmh`、`platformname`=`pc`（**不要**加 `page`，该接口不分页） |
| 字段映射 | `title`=`comic_name`、`url`=`/{comic_id}/`、`cover`=`//image.yqmh.com/mh/{comic_id}.jpg`、`update`=`latest_cartoon_topic_name`、`author`=`comic_author_list_name` |
| 兜底字段映射 / 请求头 | 留空 |

分页说明：上面不配 `{page}`，UI 翻页会拿到相同全量，框架**跨页按 URL 去重**（discover_page `_seen_urls`），第 1 页已含全站 115 部，不会重复显示、也不会空转——这是 kanman 的「分页」正确形态（它本来就没服务端分页）。

分类：不配（发现页直接推全站列表）。

---

## 11. 解密 Tab

**不需要配置。**

实测章节图 `hw-chapter2.kaimanhua.com/.../1.jpg?auth_key=...` 直接返回 **200 image/jpeg 明文**（331KB，JPEG 魔数可解码）。`auth_key` 过期问题不存在——每话抓取时从章节页实时提取最新 URL，立即下载。

若想「试内置解密」：不用试，本站无加密目标，整个解密 Tab 全部留空即可。仅当未来站点改版出现加密图（下载后不是 JPEG 魔数）时，再到本 Tab 的「图片」组选 `AES-CBC` 填 key/iv。

---

## 12. 登录 Tab

**全部留空**（无需登录，无需 Cookie 检测）。

---

## 13. 参照 JSON（可直接落盘为 sources/kanman.json）

保存前先补两处 `attr`（详见 §5 / §6），或直接使用下面成品：

```json
{
  "$schema_version": 2,
  "$id": "kanman",
  "$type": "comic",
  "$name": "看漫画(KanMan)",
  "$enabled": true,
  "$weight": 1.0,
  "$metadata": {
    "homepage": "https://www.kanman.com/",
    "description": "看漫画-KMH系漫画源；列表走 /api/getComicList/ 全量接口（本地分页/搜索），正文图明文内嵌章节页 window.comicInfo 数组",
    "lang": "zh-CN",
    "region": "cn",
    "tags": ["漫画", "免费", "在线"]
  },
  "transports": {
    "base_url": "https://www.kanman.com",
    "headers": {
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
      "Accept-Language": "zh-CN,zh;q=0.9",
      "Referer": "https://www.kanman.com/",
      "Sec-Fetch-Site": "cross-site",
      "Sec-Fetch-Mode": "no-cors",
      "Sec-Fetch-Dest": "image"
    },
    "timeout": 12,
    "retries": 3,
    "interval_ms": 600
  },
  "api_endpoints": {
    "discovery": {
      "url": "/api/getComicList/",
      "params": {
        "product_id": "1",
        "productname": "kmh",
        "platformname": "pc"
      },
      "response_path": "data",
      "item_fields": {
        "title": "comic_name",
        "url": "/{comic_id}/",
        "cover": "//image.yqmh.com/mh/{comic_id}.jpg",
        "update": "latest_cartoon_topic_name",
        "author": "comic_author_list_name"
      }
    }
  },
  "endpoints": {
    "detail": {
      "url_pattern": "/\\d+",
      "fields": {
        "title": { "css": "meta[property=\"og:title\"]", "attr": "content" },
        "cover": { "css": "meta[property=\"og:image\"]", "attr": "content" },
        "summary": { "css": "meta[property=\"og:description\"]", "attr": "content" },
        "tags": { "css": ".tags a.tag" }
      }
    },
    "content": {
      "page": {
        "list": {
          "root_selector": { "css": "#detail-chapter li.item" },
          "fields": {
            "title": { "css": "a.last-update" },
            "url": { "css": "a.last-update", "attr": "href" }
          }
        },
        "body": {
          "render": "playwright"
        },
        "render_config": {
          "extract_mode": "img",
          "img_js_path": "window.comicInfo.current_chapter.chapter_img_list",
          "wait_until": "domcontentloaded",
          "extra_delay_ms": 1500,
          "timeout_ms": 30000
        }
      }
    }
  },
  "ad_block": { "enabled": false },
  "diagnostics": {
    "selfcheck": { "strategy": "soft", "selector": { "css": "#detail-chapter" } }
  }
}
```

> 注意：`endpoints.content.page.body` 里只放 `render: "playwright"`，`render_config` 放在 `page` 层（与 content.py `fetch_comic_pages` 读取逻辑一致：`block.get("render_config") or body_cfg.get("render_config")`）。

---

## 14. 自测命令（落盘后快速验证各环节）

```powershell
# 1) 列表接口（应返回 json，data 115 条）
Invoke-WebRequest "https://www.kanman.com/api/getComicList/?product_id=1&productname=kmh&platformname=pc" -Headers @{'User-Agent'='Mozilla/5.0';'Referer'='https://www.kanman.com/'}

# 2) 详情页含章节 DOM
#    搜索 "detail-chapter"

# 3) 章节页含全量图片数组
#    搜索 "chapter_img_list"

# 4) 章节图明文可下（带完整头，见 §2 头集合）
```

在 claw 中添加源后，推荐先跑 `tools_verify_*.py` 风格的全链路验证脚本逐环节断言（列表条数、详情字段、章节数、正文页数/图片 JPEG 魔数）。