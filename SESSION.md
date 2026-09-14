# SESSION — claw（D:\code\claw）

## 2026-09-14 五问题修复（ho5ho 图 / 懒加载闪屏 / 17k / quanben / h-comic）
### 已修复（含测试，全量 --ignore flaky 548 passed）
- **ho5ho 阅读图全部「加载失败」**：根因 = 章节 URL 含中文（`/中字h漫/...`）未百分号编码，作为 `Referer` 头时 `requests` latin-1 编码抛 UnicodeEncodeError → 48/48 图请求全挂（实测）。修 `framework/http.py` 新增 `_latin1_header_value()`，`_headers_with_ua` 对所有头值做非 latin-1 百分号编码。实测 48/48 恢复。测试 `tests/test_http_header_encoding.py`（5）。
- **懒加载卡片跳动/闪屏**：根因 = `discover_page._apply_column_stretch` 无条件 `setColumnStretch`（整网格重排），懒加载每 16ms 一批都调 → 持续重排；`search_page` 早有列数守卫，发现页漏。加守卫，`_last_columns` 统一由 `_apply_column_stretch` 维护（`_reflow`/`_append_works`/`_render_restored` 不再提前赋值）。测试 `tests/test_discover_column_stretch.py`（3）。
- **17k 下载不完全**：根因 = 站点强反爬，短时间约 16 次请求后返回 **HTTP 405 封禁**（持续约 90s），后续章节全失败。修：① `framework/download_queue.add_task` 支持源级 `constraints.max_concurrency` 上限；② qk17 设 `max_concurrency:1` + `interval_ms:2000`；③ `ANTI_SCRAPE_STATUSES` 加 405（→ 触发代理池换 IP）。测试 `tests/test_download_source_concurrency.py`（4）。
- **quanben 封面**：源站已无封面数据（详情封面 `www.quanben.com` 现为停放/封锁页 503，当前域名无图）。按用户选择做**文字占位封面**：`gui/components/work_card.py` 新增 `_make_text_cover()`（标题首字 + 散列主题色渐变），无封面/加载失败时启用（`_apply_placeholder`/`resizeEvent`）。测试 `tests/test_work_card_placeholder.py`（4）。
- **h-comic 发现页慢**：根因 = `wait_until:networkidle` 每页约 28s，3 页串行约 90s 像「打不开」。改 `domcontentloaded`（配 `wait_for`）→ 约 14s/页。测试：引擎实测 10 部/页。
- **阅读器自动滚动晃动**：根因 = 漫画懒加载图片由占位高（600px）变实际高，视口上方内容高度变化使可视内容上下位移。修 `comic_view._relayout_gallery`：新增 `_visible_anchor()`，以视口顶部所在图为锚，重排后补偿滚动值（贴底仍跟底）。测试 `tests/test_comic_scroll_anchor.py`（2）。
- 顺带修 `gui/_smoke_reader.py` 的 `MockHttp.get_text` 签名（缺 `direct`，既有失效）。
- **自动滚动改为「记住位置 + 按速度递增」**（用户要求，替换上一版滚动锚定）：`novel_view`/`comic_view` 用独立浮点 `_auto_pos` 累积，`_auto_scroll_tick` 只做 `_auto_pos += speed`；`comic_view._relayout_gallery` 在自动滚动中不改动滚动值（避免与速度推进叠加 → 跳过某页）。测试 `tests/test_comic_scroll_anchor.py`（3，无图 stub，避免异步解码 Qt 竞态）。
- **阅读进度跨重启**：新增 `ReaderPage.flush_progress()`；`app` 在 `aboutToQuit` 与「离开阅读 Tab」时落盘当前进度（此前退出不落盘）。测试 `tests/test_reading_progress_shelf.py`（3）。
### 提交状态
- 已 commit + push 到 origin/master（`9113ce1` + `48d5594`）。全量 `--ignore flaky` **556 passed**。
### 待办（@followup）
- 17k 需用户填 `data/proxies.json` 代理池才可绕过 405 封禁；未填则仅串行降速。
- 用户反馈 h-comic「一页12卡片、1万多页」待确认是否仍有具体问题（引擎/分页实测正常）。
- 阅读进度「重启后从头开始」：代码链路实测可续读（15/16 书架书 URL 命中）；已补退出落盘。若仍复现，需用户提供具体书/源/步骤（疑与打包 exe 的独立 data 目录有关）。



## 爱丽丝书屋封面不加载修复（2026-09-13，未提交）
### 根因（实测证据）
- 列表/分类页**本身无封面**（真实 HTML：`div.rec_rullist` 内多 `<ul>`，书名在 `li.two`，`lazyload_book_cover=0`；列表解析正确、cover 恒空，依赖 `cover_backfill`）。
- 详情页封面提取正确（`img.lazyload_book_cover` 的 `data-src` → `img.321cdn.com/...webp`）；`get_bytes` 直连/代理均 200 webp。
- **真实缺陷**：发现页/搜索封面回填只传 `source_id` 不传 SourceConfig → CoverLoader `_dispatch_http` 取不到 `transports.direct` → 封面走系统代理（本机 127.0.0.1:65532 加速器，图床经代理慢/不稳），列表封面长时间空白/加载不出。
### 修复
- `gui/components/cover_loader.py`：新增 `_sources` 注册表 + `register_source()` / `_resolve_source()`；`load()` 开头 `source = self._resolve_source(source, source_id)`（仅传 source_id 也能补出源级 direct / AES 策略）。
- `gui/app.py`：启动 `use_http` 后遍历 `source_manager.all()` 注册源；`_on_source_saved` 保存后重注册。
- `gui/pages/discover_page.py` / `search_page.py`：回填刷新处显式传 `source`（除注册表外的直接兜底）。
- 新增 `tests/test_alicesw_cover.py`（7 条：列表 li.two 提取、列表无封面、详情 data-src、source_id→direct 透传、未注册不误伤）。
### 验证
- 定向 16 passed；全量 `--ignore test_comic_view_referer` **441 passed**。
- 真实网络 probe：列表 50 部/页；fetch_cover 取到 webp；CoverLoader 死代理下注册源仍成功（direct 生效）。
### 遗留
- alicesw 列表封面需后台抓 150 次详情（3 并发 + 0.3s 间隔），首屏约 55s 才全出封面——可考虑缓存/并发调优。
- 未 commit（等用户确认）。

## 和图书 hetushu 源交付完成（2026-09-13，未提交；框架提案待批准）
### 交付物（全部离线/活体验证通过）
- `sources/htt.json`（$id htt / 和图书 / novel）：search render playwright、detail 纯 fields（status 不可用省略）、chapter list 1646 章 `dl#dir dd`、body `#content div` + render。
- `tests/test_htt.py` 11 passed；fixtures `tests/fixtures/htt/{search,detail,chapter}.html`；`data/cf_cookies.json` 注入现役 cookie（UA153 绑定，有时效）。
### 关键结论
- **CF 直连无解**（requests/curl_cffi 全 403）；**唯一可行 = 真实浏览器过质询 → 存 cf_cookies.json → 框架 headless 渲染复用**。
- **cf_clearance 绑定 IP+TLS+UA**：MCP 浏览器 UA=Chrome/153；框架渲染 UA 硬编码 126 → 复用被拒。同 cookie + UA153 headless 直接过（搜索 5 条直出）。
- **框架改动提案（待批准，批准前未动 framework/）**：
  ① `_default_user_agent()` 提到 bundled Chromium 实际版本（153）——一个字符级改动，让所有 render 源指纹真实；② 或 render_config 支持按源覆盖 UA；③ `_get_detail_html` 加 `detail.render=="playwright"` 分支（详情/TOC 才能 app 内可用）。
### 验证
- test_htt 11/11；SourceManager 加载 htt OK；全量 pytest 380 passed，2 failed（既有 comic_view_referer，无关）。
- 活体：搜索结果 5 条 / 正文 317 段、水印随 inner_text 消失 / TOC 1646 / title·author 正确。
### 待办（@followup）
- 待用户批准 ①②③ 后落地框架最小改动 + app 内端到端（搜索→详情→阅读）。
- cf_cookies.json 过期后需重新取 cookie（浏览器解一次质询导出）。

## 番茄搜索翻页打通（2026-09-12e，未提交）
### 结论
- **根因①**：`page.evaluate(js, sel, target)` 两参数 → Playwright 只支持单 arg → TypeError 被 `except: pass` 吞 → 翻页点击从未发生（恒 10 条）。→ 改单 arg 对象 `{"sel":..., "target":...}`，JS `(arg)=>{const {sel,target}=arg;...}`。
- **根因②**：真实 `mouse.click(x,y)` 对视口外坐标无效（分页条 y≈2523 > viewport 768）→ 先 `el.scrollIntoView({block:'center'})` 再取坐标点击。
- **根因③**：`_search_html_rendered` response 分支构造 `SearchResult` 漏传 author → 作者恒空 → 已补 `author=...`。
### 机制（逆向 Search.53ebdaa2.js 确认）
- 番茄分页条 `li.byte-pagination-item`（Arco），有数据必渲染；点页码 → `page_index=N + page_count=10 + query_word + a_bogus/msToken` 重新签名请求；`a_bogus` 闭包内生成（window 无暴露）→ 走 UI 点击是唯一合法翻页路径。
- 响应 `data.search_book_data_list[]`（book_id/book_name/author/thumb_url/book_abstract…），`total_count` 多词=300。
### 实现
- `playwright_helper.fetch_search_response_json` 加 `pages`/`page_item_selector`；`_SCROLL_PAGE_JS`/`_PAGE_POS_JS`（单 arg 对象）+ 真实鼠标点击 + 逐页监听 `_merge_response_pages` 合并（page0 骨架 extend 列表），单页 8s 限时失败跳过保底。
- `search._response_pages`：`response_pages`×`max_results` 折算；fanqie.json `response_pages: 3` + render_config 缩进规范化。
### 验证
- 剑来/万古神帝/诡秘之主 多轮全部 **30 条 / uniq URL / 封面齐全 / 作者 30/30，约 15s**；单测 18 + **全量 347 passed, 7 deselected**。
### 待办
- 集中 commit 待用户确认（累计未提交：dm5 直连 + Q1/Q2/Q3 + fanqie 空格/搜索/登录 + 下载历史 + 番茄翻页）；番茄搜索登录态（token/ GUI 登录）稳定性提升待用户验证。
### 需求（用户已确认四问）
- 同 source_id + 同主书名（去尾部「第N话/集/卷/季」，含中文数字、可带分隔符）→ 并成一张卡。
- 合并卡展示：首部封面 + 角标（全季 → 「第3季」/「第1-3季」区间；话/集 → 「共 N 话/集」；混合/无后缀 → 「共 N 部」）；悬停 tooltip 列成员；标题=主书名。
- 点击 → 弹成员选集（QMenu，成员短标签 + 全名副标），选中走原打开链路。存储不动（收藏仍独立，右键落在首部）。
### 实现
- 新 `framework/series_group.py`（纯逻辑）：split_series/group_favorites/series_badge/series_member_label/_range_text。正则仅 ASCII+常见中文标点（花引号致 SyntaxError 已剔除）。
- `gui/pages/library_page.py`：`_render` 收藏先 `group_favorites` → `_to_shelf_item(group)`（len>1 ⇒ {"rec":leader+series_*, "members"}）；`_add_group` 收 items；`_on_card_clicked(item)` 拆分 `_pick_series_member` + `_open_rec(rec)`；`_ShelfCard` 封面区改 QGridLayout 容器 + 右上角 badge + tooltip。
- 测试：`tests/test_series_group.py` 18 + `tests/test_library_series_gui.py` 5（offscreen，避免真实 QMenu.exec 阻塞——改用 mock 派发 + 直接验证打开链路）。
### 验证
- 本轮累计：cover fallback 8 + login_required 5 + series 23 = 36 新测试；全量 --ignore flaky **329 passed**。全部未提交。
### 封面垃圾 base_url（已修 + 8 测试）
- 根因三段：parser.regex 是节点值正则 → 番茄详情 img src 被 SSR 清空后 extract() regex 命中失败清空 → `_abs(base_url,"")` 拼出 base_url 本体；`_extract_cover_fallback` 对非空非占位 current 直接放行（含 base_url）；backfill 只挑空 cover → 已污染记录永不重补。
- 修复：`content.fetch_cover` 走 fetch_detail 同款 fallback 链；`_extract_cover_fallback` 开头把「current 去尾斜杠 == source.base_url」归一为空强制走 regex/state 兜底；`gui/app.py _backfill_favorite_covers` 判定扩为三元组（空 / base_url / placeholder 命中）、`library_page._maybe_backfill_covers` 全量透传判定交给 App；fetch_cover 仍无效则不写库。新 `tests/test_cover_fallback.py`（8 条）。
### 番茄搜索需登录（已修 + 5 测试）
- 根因：本机 data/cookies/ 不存在 → GUI 从未登录番茄，web 搜索页未登录空 body → 渲染恒 0。登录 cookie 注入链路上一轮已就绪，待用户 GUI 登录验证。
- 修复：`sources/fanqie.json` search render_config 加 `login_required:true`；`search._search_html_rendered` 0 结果+关键词非空+未登录 → 抛 SourceError 引导「源管理→登录」；GUI 源状态 chip 自动显示。新 `tests/test_search_login_required.py`（5 条）。
### 验证
- 新增 13 测试，全量 --ignore flaky **306 passed**（301 上轮 + 5）。全部未 commit（累计未提交：dm5 直连 + Q1/Q2/Q3 + fanqie 空格/搜索 + 下载历史 + 本次两组）。
- 遗留：合并卡片（Q3）需求待澄清 → 待实现。
### 实现
- `framework/download_history.py`（新）：DownloadHistory 读写 data/downloads.json；白名单序列化（任务字段 + 章节 dict）；恢复归一化（DONE 原样 / 其余统一 FAILED + error=「程序退出时任务未完成」且清空下载记忆 → retry 退化成整本安全重下）；损坏/缺失文件静默空。
- `framework/download_queue.py`：`history_path` 参数；构造 load 恢复 + `_seq` 基线=max 恢复 dl-N+1；`_save()` 挂 add_task/_run_task finally/cancel_task/remove_done/retry_task/restart_task；新 `clear_done()`（清 DONE）/`clear_all()`（全清）。
- `gui/pages/download_page.py`：工具栏加「清除已完成」「清除记录」（全清弹确认、注明不影响本地文件）。`gui/app.py` 构造传 history_path=base_dir/data/downloads.json。
- 测试 `tests/test_download_history.py` 14 条 + 核心回归 **293 passed**。
### 待办
- 集中提交待用户确认（dm5 直连 + Q1/Q2/Q3 + fanqie 空格/搜索 + 下载历史 = 一批 commit）。
- fanqie 搜索真实可用性待用户在 GUI 登录验证；发现页首页 `list_works(fanqie,"/")` 0 部 bug 未查。


## 最近一次会话（2026-09-12 续）：番茄发现页标题空格 + 搜索不可用修复（未提交）
### 标题空格（已修 + 4 测试）
- 根因：`Discovery.list_works` HTML 分支有 title/author 解密，**API 分支 `_list_works_api` 漏了** → 番茄 rank 榜单 API 的 bookName/author PUA 密文直接进 Work.title → GUI 渲染成「标题中间多个连续空格」（HTML/详情/章节/正文路径均已实测干净）。
- 修 `framework/discovery.py` `_list_works_api` return works 前补与 HTML 分支同款解密块；新 `tests/test_discovery_api_decrypt.py` 4 条。
### 搜索不可用（三层 + cookie 注入链路）
- ① `search.py:_search_html` 无 `item.fields` 直接 return []，render=playwright 分支本不依赖 fields → 改为仅非 playwright 分支判空。
- ② 真跑渲染仍 0 结果：番茄 web 搜索页**未登录**返回空（search_book API 空 body / 页面「共 0 项」/ SSR 无数据 / a_bogus 签名正常）——站点登录墙。
- ③ GUI 登录 cookie（login_dialog→data/cookies/<sid>.json）只进 http headers、**playwright 渲染不注入** → 登录了也搜不到。修复：`cookie_manager.py` `to_playwright_cookies()`；`playwright_helper.py` `_sync_page(extra_cookies=)` + `fetch_rendered_items_sync`/`fetch_rendered_search_sync` `cookies` 参数；`search.py` Search 加 `cookie_manager` + rendered 组装注入；`gui/app.py` 接线。新 `tests/test_cookie_playwright_inject.py` 5 条。
### 验证
- pytest 核心组 **279 passed**；py_compile 全过。**待用户**: GUI 番茄登录保存 cookie 后搜索验证（本机无登录态）。
### 未提交（累计）
- dm5 直连组 + Q1 番茄标题解密 + Q2 VPN 健康探测 + Q3 下载重试 + 本次两组，均未 commit（用户未确认）。全量验证 `--ignore=tests/test_comic_view_referer.py`（已知 flaky，决策 A）。
### 遗留
- 发现页首页 `list_works(fanqie, "/")` 返回 0 部为独立 bug 待查（手工解析同 HTML 有 23 部）。
- Temp 临时脚本待删：bench_fanqie_*.py、bench_fanqie_pw_*.py、bench_fanqie_ssr*.py、bench_fanqie_api_probe.py、fanqie_detail.html。

## 最近一次会话（2026-09-12 续）：阅读页「漫画加载变慢」修复完成——源级强制直连（未提交）
### 背景
- 用户反馈阅读界面漫画加载又变慢。实测定位：本机加速器全局系统代理 127.0.0.1:65532 存活，dm5（国内直连源）全部请求（HTML/接口 wave/图片 CDN）被转发放大 5~50 倍。
### 根因实测（临时脚本体东，已删）
- dm5 主页：default(ttl sysproxy) 中位 1.22s / explicit 1.16s / **direct 0.10s**；图片 162KB default 5.09s vs direct 0.84s；真实链路 fetch_comic_pages 整章 56.5s；Playwright 渲染 key 固定前置 ~14s（非音频）。dm5.json 并行配置未变。
### 修复（源级直接 + 全链路透传）
- `framework/http.py`：`_effective_proxy(explicit, force_direct=False)`；get_text/get_bytes/post_json/post_form/get_status 加 `direct: bool = False`；各 `_once` 传 `_effective_proxy(current_proxy, direct)`；**`if proxy is None: proxy = self.defaults.proxy` 改为 `None if direct else ...`**（初版漏、ut 抓出）。
- `framework/content.py`：`_get`/`_fetch_api` 传 `direct=bool(source.transports().get("direct"))`；渲染取 key `proxy=(None if ... else transports.proxy)`。
- `framework/discovery.py` `_get`、`framework/search.py` `_http_get` + :516/:526 渲染调用：同款 direct 透传。
- `gui/components/cover_loader.py`：`_HttpFetchTask` 加 direct；`_dispatch_http` 从 item[7]=source 解 direct，direct 时 proxy=None。
- `sources/dm5.json`：transports 加 `"direct": true`。
### 验证
- 新增 tests/test_http_direct.py 13 条（`_effective_proxy` 三态 / get_bytes|get_text proxies=None / content|discovery|search / cover_loader 透传）；4 个测试文件显式签名 fake 补 `direct=False`。回归：核心 195 + GUI 62 + Playwright 20 = **277 passed**；py_compile 全过。
- 真实链路重测：fetch_detail 2.2s / fetch_comic_pages 39.95s（原 56.5s，快 30%）/ 单图 0.31s（原 5.09s，16x）。
- 待定一致性：content.py content 其它渲染/图片路径（:1237/:1456/:2216）未加 direct——dm5 阅读主链路不命中，暂不动。

## 最近一次会话（2026-09-12）：三任务收束——VPN 系统代理健康探测 + 番茄标题解密 + 下载重试回档
### 背景
- 并行工作流（video-recommend / auto-scroll / search-multi-source / 91porna / hciyuan）均有未提交 WIP，本会话只做本组三件事，不碰他人改动。

### Q2 VPN 兼顾（已实现，未提交）
- 根因：`requests` 默认 `trust_env=True` 隐式读 Windows 注册表代理 `127.0.0.1:65532`；VPN 关后端口死但注册表项在 → 所有请求连向僵尸代理（国内站也挂）。实测 trust_env=True 170ms（走代理）/ False 12191ms（IPv6 慢路径）。
- 改 `framework/http.py`：新增系统代理健康探测块（`_port_open`/`_is_loopback`/`_parse_proxy_host_port`/`_system_proxy_candidates`/`_live_system_proxy` TTL10s + `_urllib_opener`）；`HttpClient.__init__` `trust_env=False` + `_system_proxy_enabled=True`；`_effective_proxy(explicit)` 优先级：显式>defaults.proxy>存活系统代理>直连；get_text/get_bytes/post_json/post_form/_get_once_raw/get_status 全接线。
- `framework/playwright_helper.py`：`_system_proxy()` 委托 http `_live_system_proxy()` + Clash 端口兜底。
- 测试：新增 `tests/test_system_proxy_fallback.py` 16 条；既有 test_http_retry / test_proxy_pool `_make_http` 补 `http._system_proxy_enabled=False`。
- **注意（已决策 A）**：全量 pytest 因本模块把并行 WIP 的 `test_comic_view_referer.py` 时序 flaky 稳定触发（comic_view 应用级 eventFilter 泄漏 SystemError :147/:796-802、hover_title `_marquee_on` GC 竞态 :109，均他人未提交代码）。用户选「接受已知 flake」→ 全量验证用 `--ignore=tests/test_comic_view_referer.py`（264 passed）。

### Q1 番茄标题解密（已实现，未提交）
- discovery 已解密 title/author；search.py / content.py detail 缺失。
- `framework/search.py`：新增 `_search_decrypter`/`_decrypt_results`；`search_one` ytdlp/api 出口统一包；`_search_html` rendered 分支包 + HTML 合并循环逐条解密（on_page 增量也是明文）。
- `framework/content.py` fetch_detail HTML 路径（:342）title/author 解密。
- 测试：`tests/test_fanqie_title_decrypt.py` 9 条（真实字形 \uE3EA→主、\uE3EC→家）。

### Q3 下载重试回档 + 重新下载（已实现，未提交）
- `framework/download_queue.py`：`retry_task` 不再整体失败清空记忆（保留 done_chapters/epub/bytes，续下未完成）；新增 `restart_task`（整本从零，保勾选）。
- `gui/pages/download_page.py` FAILED 分支加「重新下载」按钮 → `restart_task`。
- 测试：`tests/test_download_retry_restart.py` 9 条（框架语义 + offscreen 卡片按钮）。

### 验证
- 新测试 34 条 + 回归 80 条全绿；`--ignore` flaky 文件全量 **264 passed**；py_compile 全过。全部未 commit（等用户确认）。

## 最近一次会话（2026-09-11）：恢复会话缓存提速——分批铺设 + 卡顿修复
### 背景
- 用户反馈「缓存过的会话加载再快一点，有明显卡顿感」。分析：`_render_restored` 一次性同步建全部 `WorkCard`（几百部）阻塞主线程；恢复后 80% 滚动误发网络。
### 改动（未提交）
- `gui/pages/discover_page.py`：
  - `_render_restored` 改为**两段式**：先铺「首屏 + 1 屏缓冲」（视口高 ÷ CARD_HEIGHT × cols × 2），设 `_restore_rest_scheduled`/`_restore_session`，剩余由 `_schedule_pending_restore` 每 16ms 分批铺（QTimer.singleShot 循环）。status 用 `len(fresh)`。
  - 新增 `_schedule_pending_restore(session)`：分批铺设；`_flush` 里 `session != self._restore_session` 自弃（防快速切走污染新网格）；`len(_works) <= _work_count` 已铺完则停。
  - `__init__` 加 `_restore_rest_scheduled=False`、`_restore_session=0`。
  - `_on_scroll`：`_restore_rest_scheduled or _work_count < len(_works)` → `_fetch_more_cached()`（只补缓存不发网络）并 return；铺完才走 80% `_load_next_page`。新增 `_fetch_more_cached`（按一屏量批次立刻补铺 + 续定时器）。
- 顶部导入 `from gui.components.work_card import CARD_HEIGHT`。
### Bug 修复（执行中发现）
- `CARD_HEIGHT` 未导入导致 `_render_restored` 抛 NameError → `_try_restore_source_snapshot` 的 except 吞掉返回 False。导航：`WorkCard.CARD_HEIGHT` 不行（模块级常量非类属性），直接 `from gui.components.work_card import CARD_HEIGHT`。
- `_work_count` 双重计数：`_render_restored` 先 `_work_count = len(fresh)` 又经 `_append_card` 递增，导致 `_work_count` 多算 → `_on_scroll`/分批判断误判已铺完。修复：删掉预置，置后只靠 `_append_card` 自然增长，status 用 `len(fresh)`。
### 实测
- profiler 定位 472ms 首屏中的大头是 `cover_loader._ensure_proxy` → `playwright_helper._port_open`（一次性代理端口探测，真机命中缓存即 0）。
- 200 部快照：首屏同步仅铺 6 张（`restore_rest_scheduled=True`），1.65s 内 QTimer 分批铺完，主线程峰值 ~4ms/张不卡。
### 验证
- pytest **212 passed**（无回归）；`_smoke_discover`（含会话往返断言）、`_smoke_regression`、`_smoke_reader`、`_smoke_library` 全过。临时调试脚本已删。未 commit。

## 最近一次会话（2026-09-10）：发现页换源会话缓存（切回源 0 请求还原）
### 改动（未提交）
- `framework/cache_service.py`：新增 `get_session_cache(data_dir=None)` 单例——**不落盘** RedisLikeStore（`persist_path=None`, quota=2GB）。纯内存缓存从进程启动持续到进程结束，**退出应用即由进程回收自动清空**，无需清理代码、不阻塞关闭（不是启动/重启时清，是退出时清）。
- 新增 `framework/discover_session.py`（纯逻辑无 GUI）：`SOURCE_CACHE_MAX=5`、`snapshot_key`（`disc:{sid}`）、`cat_list_from_buttons`（`(text(), cat.url|None)`）、`make_snapshot`（cats/cat_url/works/pages/has_more/current_page/scroll/ts）、`from_snapshot`（结构净化）、`register_source`（LRU 元键 `disc:_order`，超 5 逐出最旧并同步截断 order）、`prune_excess`（纯函数）、`should_restore`。
- `gui/pages/discover_page.py`：`__init__` 末尾加 `session_cache=None`；`_save_source_snapshot`（切源离开/每页加载完成/滚动节流 500ms `_schedule_scroll_snapshot` 时写快照）；`_on_source_changed` 切走前落盘旧源 + 换回命中缓存 `_try_restore_source_snapshot` **0 请求**还原（重建分类栏 + `_reset_works` 消费 `_pending_restore` → `_render_restored` 直接铺卡片 + `_restore_scroll` QTimer.singleShot 恢复滚动比例，shiboken6 防御）；未命中照走 `_load_categories`。
- `gui/app.py`：`_build_discover` 注入 `session_cache=get_session_cache()`。
- 测试：`tests/test_cache_service.py`+1、`tests/test_discover_session.py`+6、`tests/test_discover_restore_no_request.py`+3（should_restore/LRU/缺失分支）；`gui/_smoke_discover.py` 增会话往返断言（快照写→清空→还原 2 卡片）。
### 关键实现认知
- 恢复命中时 `_try_restore_source_snapshot` 内部调 `_reset_works()` 消费 `_pending_restore`（否则分类栏重建后无渲染入口）；命中即 `register_source` 刷新 LRU。
- 快照序列化一律存 `Work` dataclass 对象直接 pickle（RedisLikeStore 内部 pickle）——禁用 `Work.as_dict()`/`Work(**d)`（键 `update_info` vs 字段 `update` 不对称，会 TypeError）。
- 换分类不触发恢复：`_try_restore_*` 仅在 `_on_source_changed` 里调用；`_pending_restore` 默认 None → 走网络。
- 与 2026-09-08 cache-policy「发现/搜索页不走持久缓存」不冲突：会话缓存独立 store、`disc:` 前缀、不触碰 list_works/list_categories/Content._cache。
### 验证
- pytest **212 passed**（202 基线 + 10 新增）；`_smoke_regression`/`_smoke_reader`/`_smoke_library`/`_smoke_discover` 全过。未 commit。

## 上一条会话（2026-09-10）：dm5 阅读提速——image_api 并行 + partial 边抓边显 + wave 延迟平摊
### 改动（未提交）
- `framework/content.py`：`_fetch_comic_image_api` 加 `on_page` 参数——并行 wave 每批完成后 `on_page(list(urls))` 回调已就绪前缀，顺序回退分支每页也回调；`_fetch_comic_page_imgs`/`_fetch_comic_pages_impl` 透传 on_page（打通 dm5 → GUI `_LoadComicTask._emit_partial` → partial 信号 → `_on_images_partial` 增量渲染链路，此前 dm5 该回调为空）。
- 并行分支 `interval_ms=0`：逐页礼貌延迟改由 wave 间 `wave_gap`（dm5_config=150ms）平摊——去掉 8 并发各自 sleep 900ms 的 wave 内浪费。
- `sources/dm5.json`：`image_api.parallel={"window":8,"interval_ms":150}`（上半段已加，会话内验证）。
### 实测
- 22 图：35.8s → 19.5s；partial 3 批（13.2s/9张 → 16.7s/17张 → 19.5s/22张）。window=16 首批 17 张略快但服务端限速波动。剩余主要是 Playwright 渲染 key ~14s + HTML ~3s（`pages:` 7 天磁盘缓存兜底重复打开）。
- GUI 离屏实际渲染：progress「已加载 9 张」首批即显——todo ④「先爬前面页数」达成（dm5）。
- 关键认知：dm5 `/manhua-aiqing/` 是恋爱分类页（slug 撞名），真实详情用列表页 `manga-info` 的 `uk`（如 `/manhua-lanxiang/`）；章节走 COMIC_MID → `/template-{id}-s2/` 二次抓取。
### 验证
pytest **202 passed**；`_smoke_regression` + `_smoke_reader` 过；临时探测脚本已删。未 commit。
- `gui/components/cover_loader.py`：正文图/封面字节统一走 `HttpClient.get_bytes`。模块级共享单例 `_shared_http_client()`（Lock 惰性）；`use_http(http)` 注入 App 层共享实例；队列元组扩 9 位（use_http 标志）；`_pump` 分流 → QThreadPool `_HttpFetchTask`（headers=`_BROWSER_UA`+精确 Referer，proxy=系统代理，timeout=20s，retries=1）→ Signal 回主线程 `_on_http_result`；失败/解码失败 use_http=False 回队走 QNAM 原路径；AES 解密 `_decode_pixmap` 通道不变。
- `gui/app.py`：模块级 `network_defaults_from_settings(settings)`（新键 impersonate/user_agents，`or None` 默认关闭）；`CoverLoader.instance().use_http(self.http)`。
- `framework/discovery.py`：check_links 复用 `HttpClient(defaults=self._http.defaults)`。
- `app_config.json`：network 加 `impersonate:null` / `user_agents:[]`。
- `framework/downloader.py`：单章/批量正文图按章节 URL 透传精确 Referer（订正旧注释）。
- 新增 tests/test_cover_loader_http.py（5）+ test_downloader_referer.py（4）+ test_network_wiring.py（4），全 mock。
### 验证
- pytest **202 passed**（189 基线 + 13 新增）。
- `_smoke_reader / _smoke_library / _smoke_discover` 全过；app_config.json 解析 OK。

## 上一条会话（2026-09-10）：91pornacomic 阅读界面封面 + 18mh-video 推荐显示双修复
### 改动（未提交）
- `framework/parser.py`：`extract()` 字段级可选 `url_replace`（[regex, repl]，repl 支持 `{n}` 捕获组、正则局部 `import re`，无匹配保留原值）。
- `sources/91pornacomic.json`：`detail.fields.cover` 配 `url_replace`（expose.eisees.com 死链 0 字节 → pic.zdmhyg.cn，丢弃 `?auth=`）。
- `sources/18mh-video.json`：`detail.fields` 增 `tags`（`meta[property='video:tag']` content）。
- `gui/components/cover_loader.py`：新增模块级 `_decode_pixmap(data, source=None)`——先直接解码，失败且有 source 时按源 `decryption.image` AES-CBC 解密后再解码，返回解密后字节供持久化；`load()` 增 `source=None` 参数，Redis/命中分支接解密；queue 变 8 元组并透传 source 至 `_pump/_on_reply/_on_direct_reply/_retry_later`。
- `gui/pages/reader/video_view.py`：`_GridCard`/`_render_rec_cards`/`_populate_ep_cards` 传 `source`；`_recommend_keyword_of` 优先 tags[0]、回退标题 `strip()[:3]`（防只命中自身）；`_on_recommend_done` URL 去重 + 剔自身 + 前 8 条。
- `gui/pages/reader/comic_view.py`：comic 正文图 `CoverLoader.load` 传 `source`（换站失败 URL fallback 时也能解密）。
### 验证
- pytest **160 passed**（基线 144 + cover 解密 4 + content url_replace/tags 3 + video_cards 2 改/增等）。
- `_smoke_reader / _smoke_library / _smoke_discover` 三脚本全过。
- 端到端：真实配置 fetch_detail → 91porna cover=`https://pic.zdmhyg.cn/u/a.jpg`（无 query）；18mh tags 配置生效。
- 源目录全量加载 OK（40 个源）。

## 上一条会话（2026-09-09）：视频阅读器「多集源不自动播、停在选集态」
### 改动（未提交）
- `gui/pages/reader/video_view.py`（未动 `_GridCard`）：新增 `_selection_mode` 标志；`load()`/`reload_detail()` 对 `len(chapters) >= 2` 走选集态——设 `_current_idx`、`_paint_card_selection()` 高亮默认/续读集、`_show_status("请选择要播放的集数")`、不取流直接 return；单集/无分集保持自动取流播放（reload_detail 补齐无分集自动播分支对齐 load）。`_load_episode` 进入即退出选集态；`_select_episode` 选集态点当前高亮卡也算选择播放（播放中不重载同集）；`_toggle_play_pause` 选集态无流时先按默认集取流。load/reload_detail 重置 `_has_played=False`，选集态退出不误存续读进度。
### 验证
- pytest tests/ = **141 passed**（与本会话改动前基线持平，无回归）。
- `gui/_smoke_reader.py` 通过（2 集 detail 现走选集态，current_episode_no() 仍返回 0，原断言不受影响）。
- 临时离屏冒烟 9 场景全过（已删）：多集不取流/▶ 播默认集/点当前高亮卡播放/点第 1 集播放/续读高亮不取流/单集自动播/season 自动播/reload_detail 多集与单集。

## 上一条会话（2026-09-09）：avgood/18mh-video/18j 视频分集显示修复
### 改动（未提交）
- `sources/avgood.json`：episode 块加 `single_chapter:true` + note（hanime1 同款语义：list 解析 iframe 播放页作唯一分集、无 iframe 页回退详情 URL 单集不空白）。avgood 分集 URL 必须保持 iframe src——详情 URL 取流失败（详情 id≠播放 id，api_endpoints.episode 取不到 playlink）。
- `tests/test_video_single_chapter.py`（新增 4 例）：锁定 avgood 有/无 iframe 两分支 + 18j 空 list 回退 + 18mh 无 list 回退。
### 结论（联网实测）
- avgood / 18mh-video / 18j 详情页均为「单视频 + 相关推荐」，无真分集/选集/播放清单结构。
- 三源 fetch_detail 各返回 1 条非空分集，fetch_video_episode 取流地址核实为真实 m3u8（#EXTM3U）——「1 个分集条目」即正确行为，符合用户验收。
- 18mh-video / 18j 上会话已验证正确，本次复测确认无需改；框架 content.py 零改动。
- 离屏 GUI 三源各渲染 1 张分集卡、选集区可见、首卡选中。
### 验证
pytest 136 passed（基线 130 + 并行 agent 新增 2 + 本任务 4）。

## 状态
- **并行约束**：工作区有其它任务（video-recommend / auto-scroll / search-multi-source / 91porna 封面解密 / hciyuan 集合分集等）的未提交改动，勿动；tests/ 下 `test_single_chapter_list.py`、`test_content_detail.py` 等为并行 agent 新增。
- **待办**：用户确认后按需 git 提交 avgood.json + test_video_single_chapter.py。
- **待办**：todo ④ 另一半——novel_view 小说正文「边抓边显」懒加载未排查（dm5 漫画已实现）。相关未提交改动勿动（含本会话 framework/content.py）。

## 常用命令
- 测试：`$env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="D:\code\claw"; python -m pytest tests/ -q`
- 冒烟：`$env:QT_QPA_PLATFORM="offscreen"; python gui/_smoke_reader.py`
- 无 lint 配置（无 flake8/ruff/pyproject）。

## 下一会话 · 2026-09-12（懒加载不跳屏 / 番茄未登录可搜 / 包子封面 403）
### 改动（未提交）
- **懒加载不跳屏**：`gui/pages/search_page.py` 加 `_scroll_ratio`/`_restore_scroll_ratio`（合并/筛选重建后按比例恢复滚动，`_restoring_scroll` 抑制 on_scroll 级联）；`_load_more_results` 追加后 `_finish_more_anchor` 钉底续接/保持原位。`gui/pages/discover_page.py` 同套路：`_append_works` 追加后 `_finish_append_anchor`，`_restore_scroll` 的 setValue 包 `_restoring_scroll`。根因：懒加载贴底追加时 value 钉旧 max → 新内容被「踢下去」需再滚，视觉像上跳。
- **番茄未登录可搜**：`framework/playwright_helper.py` 新增 `fetch_search_response_json`（监听接口响应返回 JSON 文本）；`framework/search.py` `_search_html_rendered` 新增 response 模式分支 + `_items_from_response_json`（字段映射 + url_template）；**补 `import json`（模块级缺失，新代码 json.loads 曾 NameError 被 except 吞成空结果）**；`sources/fanqie.json` search.render_config 去掉 `login_required:true`，改 `response_json_url:/api/author/search/search_book/v1`、`response_items:data.search_book_data_list`、`response_fields`。注意 `jsonpath` 工具不支持 `$.` 前缀，用点号路径。
- **包子封面 403**：根因 `static-tw.baozimh.com` 该域 403（防盗链），`static-tw.bzmgcn.com` 200。`sources/baozimh.json` 三处 cover（discovery.works_list_item / search.item / detail）加 `url_replace` 把 `static-tw.(baozimh|bzimg).com` 重写到 `static-tw.bzmgcn.com`（`([^?#]*)` 捕获路径，query 被剥，实测无 query 200@49KB）。
### 新测试（全量 342 passed, 7 deselected，16.37s）
- `tests/test_search_response_json.py`（7）：response 模式提取/空 JSON/字段映射/无登录提示。
- `tests/test_baozimh_cover_rewrite.py`（6）：三处 cover 重写 + bzimg 域 + 不动其他域 + 幂等。
### 验证
- 引擎 probe（Discovery.list_works）cover 已变 `https://static-tw.bzmgcn.com/...` 且下载 200。
- 番茄真实抓包探针（headless）显示未登录 API 返回完整数据，但当下真实 `fetch_search_response_json` 跑超时（网络/反爬波动）——由用户 GUI 实测；逻辑由 mock 测试覆盖。
- PowerShell 不支持 heredoc（`<<'PY'`）已再用经验佐证。
### 状态
- 未 commit（等用户确认）；并行 WIP 未动。
- 遗留清理可选：`probe_comic_display.py`、`probe_fanqie_*.py/html/txt`、`sources/fanqie.json.bak-fanqie-categories` 为探索期残留。
### 待办
- 用户 GUI 实测三修复 → 确认后一并 git 提交。
### 补充 · 番茄「还是不能搜索」终局修复（根因+解法已定案）
- 根因：番茄网关对**全新会话直接访问搜索页**静默返回 200 空 body（fingerprint/签名/cookie 全排除；user/info 同会话 104B 正常 → 仅搜索接口被无声风控）。requests 直连/系统代理/headful/真 Chrome audio channel 全空。
- 解法：`fetch_search_response_json` 加 `warm_up_url`——先访问站点首页停留+模拟滚动（真实行为信号），再 goto 搜索页 → 接口返回完整数据 7492B（两次复现 7488B）。search.py response 分支默认 `warm_up_url = source.base_url`。
- 端到端：真实 `_search_html_rendered` 13.2s → 10 条，URL=book_id、封面 fqnovelpic 正确；标题 PUA 混淆由 GUI 主入口 `_decrypt_results` 解密。全量 342 passed。
- 后续若再报「番茄空结果」：先确认是否走了预热路径再怀疑代码。
### 追加 · 2026-09-12h（番茄「详情进不去+乱码」双根因，含新模块）
#### 改动（未提交）
- **详情 URL**：`sources/fanqie.json` search `response_fields.url_template` `{base}/book/{book_id}` → `{base}/page/{book_id}`（真 URL；/book/ 404。fetch_detail HTTP 直连验证 title/author/736 章）。
- **正文/详情标题乱码根治**：新模块 `framework/fanqie_font.py`（@font-face woff2 → Pillow 光栅化 PUA glyph → 微软雅黑 IoU 模板匹配建表，v2 快筛，阈值 0.66，TTL 6h 缓存 key "v2:"）；`framework/decrypter.py` `decrypt(..., html="")`、`_translit` 动态解析优先，字体不可达回落静态 `framework/data/fanqie_glyph_map.py`（已过期，仅兜底）；`framework/content.py` `_fetch_chapter_page`/`fetch_detail` 两处注入 html。
- 实测正文 PUA-LEFT=0/2110 可读；首次建表 7s。
#### 新测试
- `tests/test_fanqie_font.py`（8）：extract/空 html/无 PUA 直通/假 URL 网络不可达安全回落/不带 html 静态行为不变。
- fanqie 全量相关 17 passed；全量 384 中除 card_layout_qss 均过。
#### 验证
- 详情链路：HTTP 直连 `/page/7654553567902043161` → title「剑来：我有无敌剑神系统」/author「速速编辑部」/cover/736 章 0.8s。
- 正文链路：`/reader/{id}` → 解密后 PUA-LEFT=0。
- **回归注意**：批跑 `tests/test_card_layout_qss.py` 偶发 Qt 渲染 `access violation` 进程级崩溃（无 pytest 报告），单独跑 6 passed；属环境间歇。批跑无输出时先查日志文件头是否 `Windows fatal exception`。
#### 状态
- 全部待 commit（等用户确认）：dm5 直连 / Q1-Q3 / fanqie / 下载历史 / 懒加载 / 包子 / 搜索预热 / 本次 /page/+动态字体。
- 遗留：搜索列表 title/author 仍密文（搜索页无字体+异源）待后续；搜索接口空响应风控待接入（warm_up 有效但今日仍高的空率）。
- 探针残留已清理（probe_* / verify_* / fq_font.woff2 / fq_reader.html）。
### 待办
- 用户确认后一次 git commit。
- （后续）搜索列表密文显示方案：会话内抓详情/搜索字体或 DOM 真字。