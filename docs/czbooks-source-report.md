# CZBooks（czbooks.net，小說狂人）源交付报告

> 交付日期：2026-09-13 · 源类型 `$type: novel` · 架构：全站 SSR，无需 render

## 一、交付物

| 文件 | 说明 |
|---|---|
| `sources/czbooks.json` | CZBooks 源配置（discovery + search + detail + content 全功能） |
| `tests/test_czbooks.py` | 14 条离线单元测试（真实页面 HTML 快照驱动，零网络） |
| `tests/fixtures/czbooks/*.html` | 页面快照：`cz_search.html`（搜索页 40 条）、`cz_detail.html`（《劍來》s669hk，連載中）、`cz_detail2.html`（《寵你上癮》，已完結+真實封面+「作品簡介」前綴）、`cz_detail3.html`（《劍來》備用書版，目錄無 chapterNumber）、`cz_chapter.html`（正文頁）、`cz_cat.html`（玄幻分類頁，截取 10 條） |

## 二、测试结果

```
tests/test_czbooks.py ：14 passed（约 0.6s，纯离线）
全量 pytest           ：1 failed / 395 passed —— 唯一失败 test_comic_view_referer.py
                        为预存在问题（AttributeError 出在 gui/components/hover_title.py 的
                        GUI QSS 组件），与本源无交集。
```

真实验证（框架完整链路，实测通过）：
- 搜索 `劍來` → 40 条，首条标题/作者/URL/最新章节/封面全部正确；简体 `斗破苍穹` 同样可搜。
- 详情 `https://czbooks.net/n/s669hk` → 書名《劍來》、作者烽火戲諸侯、狀態連載中、標籤[玄幻奇幻]、简介完整、**1412 章全量目录**（fixture 截断仅保首尾用于测试）。
- 正文 `https://czbooks.net/n/s669hk/s6pk9ckm?chapterNumber=0` → 第 1 章全文 3188 字；末章 13990 字，免登录全文。
- 发现 `/c/xuanhuan` → 22 個分类入口、每类 70 本作品卡（标题/URL/封面/作者）。

## 三、站点与实现要点

1. **技术栈**：品牌「小說狂人」，纯 SSR（无 `__NEXT_DATA__`/`__NUXT__`），未发现字体混淆/登录付费墙；Cloudflare 托管质询 **requests 直连即可 200**（浏览器式 UA + Referer 即可，Chrome/126 UA），故全程无 render、无 playwright。
2. **搜索**：端点为 **path 风格 `/s/{keyword}`**（`?keyword=` query 形态实测 404）。每页固定 40 条、无搜索分页 → `constraints.search.max_pages: 1`。配置用 `paginator.url_template: "/s/{keyword}"`（与番茄同型）。
3. **详情**：`/n/{book}` 页即含完整目录（`ul#chapter-list`，1400+ 章单页）。书名带《》，状态 `連載中`/`已完結`，简介可能含「作品簡介：」前缀，作者 fallback 文本含「作者: 」前缀 —— 均已在 `fields.clean`/`title_clean` 配置清洗。分类即 `a#novel-category`（如「玄幻奇幻」）。
4. **章节链接两种形态**：带 `?chapterNumber=N`（N=章序-1）与不带（備用書）；目录解析统一用 `ul#chapter-list li a`，两者都能直接抓正文，无需 chapters_api。
5. **正文**：`div.content` 纯文本 + `<br>` 断行（框架转 `\n`），单页全文无章内分页。
6. **发现**：静态分类入口（站点导航 `/c/{slug}` × 22 个，成人工口分类有意排除）；作品列表 `div.novel-item`。

## 四、已知限制（如实记录）

- **详情页仅对 `/n/{book}` 形态有效**：正文页 `/n/{book}/{chapter}?chapterNumber=0` 无 `.novel-detail` 容器，也不含目录。用户直接粘贴章节 URL 打开时将抓不到详情元数据（框架无把章节 URL 归一化到 `/n/{book}` 的机制；搜索/目录流程产出的始终是规范 URL，实际不受影响）。
- **无封面书的占位图**：未配 `placeholder`，无封面书会直接展示站点统一占位图 `images/default_no_thumbnail.jpg`，非数据错误。
- **搜索无翻页**：最多拿前 40 条；如需更深层结果需走发现/分类（每类首页 70 本，分类页是否有分页未深挖）。
- 目录首项「正文卷/正文」是无链接的卷标题，解析产生空 URL 条目，框架抓目录自动跳过（已在测试断言）。

## 五、变更清单（Git）

- 新增 `sources/czbooks.json`
- 新增 `tests/test_czbooks.py`
- 新增 `tests/fixtures/czbooks/`（6 个页面快照）
- 未改动任何 framework / 既有 sources / 既有 tests 文件。（仓库中大量已修改/未跟踪文件为过往会话历史工作，本次未触碰。）