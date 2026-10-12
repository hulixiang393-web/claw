"""章节：目录列表、正文分页、解密、在途去重。

B4 拆分自 content.py，方法原样迁移，行为不改变。
"""

from __future__ import annotations

from .shared import (
    BookStackChapterResult, Chapter, ContentMissingError, _AD_IMAGE_RE,
    _extract_chapter_number, _re, _sort_chapters, _strip_html_tags,
    log, threading, urljoin, utils,
)


class ChapterMixin:
    def _fetch_chapters(
        self,
        source: SourceConfig,
        doc,
        book_title: str = "",
        detail_url: str = "",
        html: str = "",
    ) -> List[Chapter]:
        """从详情页提取章节列表。按 content_type 读 content 配置。

        支持源配置：
        - list.chapter_order: "desc" → HTML 倒序，反转成正序（asc）
        - list.title_clean: true    → 从章节标题移除书名
        - content.<type>.single_chapter: true → 详情页即单章图集（无章节列表），
          直接以详情 URL 作为唯一章节（如 wnacg 每 aid 一图集）。
        """
        content_cfg = self._content_block(source)
        if source.content_type == "novel":
            block = content_cfg.get("chapter") or {}
        elif source.content_type == "comic":
            block = content_cfg.get("page") or {}
        else:
            block = content_cfg.get("episode") or {}
        if source.source_id == "bookstack":
            return self._fetch_bookstack_chapters(source, doc, block, detail_url)
        # 换源站：分集从 source_switch.ep_list_selector 提取（按当前 sid）
        switch_cfg = block.get("source_switch") or {}
        if switch_cfg.get("ep_list_selector") and detail_url:
            # 当前 sid 从详情 URL 提取；URL 无 sid 时默认用第一个播放源（sid=1）
            m_sid = _re.search(r"sid=(\d+)", detail_url)
            sid = m_sid.group(1) if m_sid else switch_cfg.get("default_sid", "1")
            return self._fetch_chapters_for_source(
                source, doc, detail_url, sid
            )
        # 单章图集：详情页即唯一章节。若同时配置了 list（如 hanime1 详情页
        # 侧边栏「播放清單」= 同系列/相关分集），优先按列表解析；解析失败
        # 或无结果回退单章（detail 自身仍可播）。
        list_cfg = block.get("list") or {}
        root_sel = list_cfg.get("root_selector")
        fields = list_cfg.get("fields") or {}
        list_page_url = self._abs_url(source, detail_url) if detail_url else source.base_url
        if block.get("single_chapter"):
            if root_sel and fields:
                try:
                    items = self._parser.parse_items(
                        doc, root_sel, fields, source.base_url
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "[%s] 播放清單解析失败，回退单章：%s", source.source_id, exc
                    )
                    items = []
                if items:
                    chapters: List[Chapter] = []
                    seen: set[str] = set()
                    for it in items:
                        url = it.get("url", "")
                        if not url or url in seen:
                            continue
                        seen.add(url)
                        chapters.append(
                            Chapter(
                                title=it.get("title", "")
                                or f"第{len(chapters)+1}集",
                                url=url,
                                cover=it.get("cover", "") or "",
                            )
                        )
                    if chapters:
                        # 帖子分页 first_page_is_current：详情页自身（<span> 无
                        # href）不在播放清單里 → 复用常规列表的同款补全，避免合集
                        # 丢失第 1 集（如 hciyuan 合集帖：清單只有 2..N 页）。
                        self._prepend_first_page(source, doc, list_cfg, detail_url, chapters)
                        # 当前视频置首（用户打开的那一集放列表顶部）
                        own = (detail_url or "").rstrip("/").lower()
                        if own:
                            for idx, ch in enumerate(chapters):
                                if ch.url.rstrip("/").lower() == own:
                                    cur = chapters.pop(idx)
                                    chapters.insert(0, cur)
                                    break
                        return chapters
            return [
                Chapter(
                    title=book_title or "全本",
                    url=detail_url or self._abs_url(source, source.base_url),
                )
            ]
        if not root_sel and not list_cfg.get("chapters_api"):
            return []

        def _norm_url(url: str) -> str:
            """归一化 URL：去尾 /、去 fragment、转小写。"""
            from urllib.parse import urlparse, urlunparse

            parsed = urlparse(url)
            return urlunparse(parsed._replace(fragment="")).rstrip("/").lower()

        # 独立目录页：content.chapter.list.chapters_url 为模板（如 /other/chapters/id/{id}.html），
        # 从详情 URL 提取 book id 并二次抓取，在该页提取完整章节列表。
        # id 来源两级：详情 URL（/{id}/ 段）→ 详情页 HTML 正则（chapters_id_regex，
        # 别名 URL 站如 dm5 /manhua-xxx/ 的 JS 变量 COMIC_MID）。
        chapters_url_tpl = list_cfg.get("chapters_url") or ""
        if chapters_url_tpl and detail_url:
            m_id = _re.search(r"/(?:novel|book|comic|detail)/(\w+)", detail_url)
            book_id = m_id.group(1) if m_id else ""
            if not book_id:
                id_regex = list_cfg.get("chapters_id_regex")
                if id_regex and html:
                    m_id2 = _re.search(str(id_regex), html)
                    if m_id2:
                        book_id = m_id2.group(1) if m_id2.groups() else m_id2.group(0)
            if book_id:
                try:
                    cat_url = chapters_url_tpl.replace("{id}", book_id)
                    # 目录页复用 _get_detail_html 的 TTL 缓存：同一本书 5 分钟内
                    # 重复打开不再二次整页下载（novel 源详情 = 详情页 + 目录页
                    # 两次串行请求，缓存目录页后重复打开秒级）。
                    cat_html = self._get_detail_html(
                        source, cat_url, self._abs_url(source, cat_url)
                    )
                    cat_doc = self._parser.parse(cat_html)
                    doc = cat_doc  # 用目录页 doc 提取章节
                    list_page_url = self._abs_url(source, cat_url)
                except Exception:
                    pass  # 目录页抓取失败回退详情页

        items = None
        # API 目录（SPA 目录站，如纵横小说）：详情页目录为 JS 渲染（SSR 空），
        # HTML 解析抓不到；直调官方章节 API 拿全部章节。
        # 配置（content.chapter.list.chapters_api）：
        #   url             章节 API（支持 {id} 占位）
        #   method          GET / POST（缺省 GET）
        #   body            请求体（POST，支持 {id} 占位）
        #   body_format     form（urlencoded，缺省）/ json
        #   response_path   JSONPath 定位卷/章列表（如 result.chapterList）
        #   children_path   卷内章节子数组字段（如 chapterViewList），缺省把
        #                   response_path 结果直接当章节列表
        #   item_fields     title → 章节名字段；chapter_id → 章节 ID 字段
        #   url_template    章节 URL 模板（支持 {id}/{chapter_id}/{任意字段} 占位）
        chapters_api = list_cfg.get("chapters_api") or {}
        if chapters_api and detail_url:
            try:
                api_items = self._fetch_chapters_api(source, chapters_api, detail_url, html)
                if api_items is not None:
                    items = api_items
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] API 目录抓取失败，回退 HTML 解析：%s", source.source_id, exc)
        if items is None:
            items = self._parser.parse_items(doc, root_sel, fields, source.base_url)
            # 目录页分页必须在统一去重/排序之前全部展开，否则跨页目录会被
            # 当成一页处理，最终顺序也会随站点的分页方向变化。
            paginator = list_cfg.get("paginator") or {}
            next_sel = (paginator.get("next_link") or {}).get("selector")
            if next_sel:
                constraints = source.raw.get("constraints") or {}
                chapter_limit = (constraints.get("chapter") or {}).get("max_pages")
                max_pages = int(paginator.get("max_pages") or chapter_limit or 20)
                max_pages = max(1, max_pages)
                visited_pages = {_norm_url(list_page_url)}
                current_doc = doc
                current_url = list_page_url
                for _ in range(max_pages - 1):
                    next_url = self._parser.extract_first(
                        current_doc, next_sel, current_url
                    )
                    if not next_url:
                        break
                    next_url = self._abs_url(source, next_url)
                    normalized = _norm_url(next_url)
                    if not normalized or normalized in visited_pages:
                        break
                    visited_pages.add(normalized)
                    try:
                        next_html = self._get_detail_html(source, next_url, next_url)
                        current_doc = self._parser.parse(next_html)
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "[%s] 目录分页抓取失败，已保留前面页面：%s",
                            source.source_id,
                            exc,
                        )
                        break
                    page_items = self._parser.parse_items(
                        current_doc, root_sel, fields, source.base_url
                    )
                    if not page_items:
                        break
                    items.extend(page_items)
                    current_url = next_url
        chapters: List[Chapter] = []
        seen_norm = {}
        seen_title = set()

        def clean_title(t: str) -> str:
            """从章节标题移除书名 + 清理多余分隔符/后缀词。"""
            t = t.strip()
            if not t:
                return t
            if book_title:
                t = t.replace(book_title, "")
            # 移除《》及其中的书名残留（含空《》）
            t = _re.sub(r"《[^》]*》", "", t)
            # 移除常见后缀词
            for suffix in ("最新章节", "全文阅读", "全文免费阅读", "最新", "TXT", "txt"):
                t = t.replace(suffix, "")
            # 清理孤立分隔符
            t = t.replace(" - ", " ").strip(" -：:　 ").strip()
            t = _re.sub(r"\s+", " ", t)
            return t

        start_reading_titles = {
            "从第一章开始阅读", "开始阅读", "从第一话开始阅读", "从头开始阅读"
        }
        for it in items:
            url = it.get("url", "")
            if not url:
                continue
            title = it.get("title", "")
            raw_title = str(title or "").strip()
            title = clean_title(raw_title) if list_cfg.get("title_clean") else raw_title
            if title and self._decrypter is not None:
                title = self._decrypter.decrypt(
                    source, title, target="chapter", html=html
                )
            nurl = _norm_url(url)
            if nurl in seen_norm:
                existing = chapters[seen_norm[nurl]]
                if existing.title in start_reading_titles and title not in start_reading_titles:
                    existing.title = title
                continue
            title_key = _re.sub(r"\s+", " ", title.strip().lower())
            if title_key and title_key in seen_title:
                continue
            if title_key:
                seen_title.add(title_key)
            chapters.append(Chapter(title=title or f"第{len(chapters)+1}章", url=url))
            seen_norm[nurl] = len(chapters) - 1

        # WordPress 帖子分页（div.page-links）：当前页（详情页自身）是
        # <span class="...current">（无 href），列表只提取后续页 <a> 链接 →
        # 第一集丢失。first_page_is_current: 启用时读 current span 的页号，
        # 若小于已提取的最小页号（即列表缺第 1 页），把详情 URL 作为该页补入
        # 列表头部（详情页本身即第 1 页内容）。常规列表与 single_chapter
        # 播放清單共用 _prepend_first_page。
        self._prepend_first_page(source, doc, list_cfg, detail_url, chapters)

        # 倒序反转（HTML 倒序 → 正序）
        order = list_cfg.get("chapter_order", "asc")
        if order == "desc":
            chapters.reverse()
        # 章节序号自动排序：处理「先最新后顺序」与「反爬乱序」。
        # 三种类型（novel/comic/video）共用此入口，无需源配置。
        # 仅当标题含序号数字（阿拉伯/汉字/罗马）时才改变顺序，
        # 原顺序本就正序则保持不变；无数字章节排末尾。
        if len(chapters) > 1 and any(
            _extract_chapter_number(ch.title) is not None for ch in chapters
        ):
            chapters = _sort_chapters(chapters)
        return chapters

    # ------------------------------------------------------------------ #
    def _fetch_chapters_api(
        self, source: SourceConfig, cfg: dict, detail_url: str, html: str = ""
    ) -> Optional[List[dict]]:
        """API 目录：从详情 URL 提取 book id，直调章节 API，返回 [{title, url}]。

        供 SPA 目录站（详情页目录为 JS 渲染、SSR 空，如纵横小说）直接拿 JSON
        章节列表，避免渲染详情页。配置见 _fetch_chapters 的 chapters_api 注释。

        - 从 detail_url 提取 book id（/{id} 占位）或从详情页 HTML 取（id_from
          = "html_attr" + id_css/id_attr，如 shuyous 的 .page2[data-aid]）：
          无法提取 → 返回 None（调用方回退 HTML 解析）
        - `encrypt`（{key, iv, field}）→ body JSON 先 AES-CBC 加密再 POST
          （等价站点 JS 的 CryptoJS 加密请求，如 shuyous loadChapterPage）
        - body 含 {page} 占位 + `max_pages` → 逐页轮询拼接（每页固定条数，
          如每 100 章一页），返回空页即停；轮询结果按 URL 去重
        - 网络/解析失败 → 抛异常（调用方 catch 后回退 HTML 解析）
        - 成功（含空列表）→ 返回 [{title, url}]，调用方直接用（不回退）
        """
        from urllib.parse import urlencode, urljoin

        book_id = ""
        if (cfg.get("id_from") or "") == "html_attr" and html:
            try:
                doc = self._parser.parse(html)
                aid = self._parser.extract_first(
                    doc,
                    {"css": cfg.get("id_css") or "body", "attr": cfg.get("id_attr") or "data-aid"},
                    "",
                )
                book_id = str(aid or "").strip()
            except Exception:  # noqa: BLE001
                book_id = ""
        if not book_id:
            m_id = _re.search(r"/(?:novel|book|comic|detail|bookinfo|program|shuku)/(\w+)", detail_url)
            book_id = m_id.group(1) if m_id else ""
        if not book_id:
            return None
        api_url = str(cfg.get("url") or "").replace("{id}", book_id)
        abs_url = urljoin(source.base_url, api_url)
        headers = dict(self._headers(source))
        method = (cfg.get("method") or "GET").upper()
        body = cfg.get("body") or {}
        max_pages = max(1, int(cfg.get("max_pages") or 1))
        encrypt = cfg.get("encrypt") or {}
        item_fields = cfg.get("item_fields") or {}
        title_key = item_fields.get("title") or "title"
        cid_key = item_fields.get("chapter_id") or "chapter_id"
        url_tpl = str(cfg.get("url_template") or "")
        out: List[dict] = []
        seen_url = set()
        volume_ids = cfg.get("volume_ids") or []
        request_params = dict(cfg.get("params") or {})
        if cfg.get("volumes_url"):
            volumes_url = str(cfg["volumes_url"]).replace("{id}", book_id)
            if request_params:
                volumes_url += ("&" if "?" in volumes_url else "?") + urlencode(
                    {k: str(v).replace("{id}", book_id) for k, v in request_params.items()}
                )
            volumes_resp = self._http.get_json(
                urljoin(source.base_url, volumes_url),
                headers=headers, timeout=self._timeout(source),
                retries=self._retries(source), proxy_pool=source.proxy_pool(),
            )
            volume_node = self._jsonpath(
                volumes_resp, cfg.get("volumes_response_path") or "data"
            )
            if isinstance(volume_node, list):
                volume_ids = [
                    item.get(cfg.get("volume_id_field") or "volumeId")
                    for item in volume_node if isinstance(item, dict)
                ]
        for volume_id in (volume_ids or [None]):
            for page in range(1, max_pages + 1):
                api_url_for_page = api_url.replace("{volume_id}", str(volume_id or ""))
                page_abs_url = urljoin(source.base_url, api_url_for_page)
                filled = dict(request_params)
                filled.update(utils.fill_json(body, id=book_id, page=page, volume_id=volume_id or ""))
                filled = utils.fill_json(filled, id=book_id, page=page, volume_id=volume_id or "")
                # JSON chapter APIs commonly expose page as a query parameter even
                # when no request body template is configured.
                if page > 1 or cfg.get("page_param"):
                    filled.setdefault(str(cfg.get("page_param") or "page"), page)
                resp = None
                if method == "POST":
                    if encrypt:
                        # 请求体 JSON 加密为 AES base64，POST {field: 密文}
                        field = encrypt.get("field") or "data"
                        enc_body = self._encrypt_request_body(encrypt, filled)
                        text = self._http.post_form(
                            page_abs_url, form_data={field: enc_body}, headers=headers,
                            timeout=self._timeout(source), retries=self._retries(source),
                            interval_ms=self._interval_ms(source),
                            proxy_pool=source.proxy_pool(),
                        )
                        import json as _json

                        resp = _json.loads(text) if text else {}
                    else:
                        body_format = (cfg.get("body_format") or "form").lower()
                        if body_format == "json":
                            resp = self._http.post_json(
                                page_abs_url, json_body=filled, headers=headers,
                                timeout=self._timeout(source), retries=self._retries(source),
                                proxy_pool=source.proxy_pool(),
                            )
                        else:
                            text = self._http.post_form(
                                page_abs_url, form_data=filled, headers=headers,
                                timeout=self._timeout(source), retries=self._retries(source),
                                proxy_pool=source.proxy_pool(),
                            )
                            import json as _json

                            resp = _json.loads(text) if text else {}
                else:
                    qs = urlencode(filled)
                    page_url = f"{page_abs_url}&{qs}" if "?" in page_abs_url else f"{page_abs_url}?{qs}"
                    resp = self._http.get_json(
                        page_url, headers=headers,
                        timeout=self._timeout(source), retries=self._retries(source),
                        proxy_pool=source.proxy_pool(),
                    )

                node = resp
                rpath = cfg.get("response_path") or ""
                if rpath:
                    node = self._jsonpath(resp, rpath)
                children_path = cfg.get("children_path") or ""
                raw_items = []
                if children_path and isinstance(node, list):
                    for sub in node:
                        if isinstance(sub, dict) and isinstance(sub.get(children_path), list):
                            raw_items.extend(sub[children_path])
                elif isinstance(node, list):
                    raw_items = node
                if not raw_items:
                    break  # 空页：轮询结束
                for it in raw_items:
                    if not isinstance(it, dict):
                        continue
                    c_title = str(it.get(title_key) or "").strip()
                    c_url = url_tpl
                    for ph, val in (("{id}", book_id), ("{chapter_id}", str(it.get(cid_key) or ""))):
                        c_url = c_url.replace(ph, val)
                    c_url = utils.fill_template(c_url, it)
                    if not (c_title and c_url):
                        continue
                    norm = c_url.rstrip("/").lower()
                    if norm in seen_url:
                        continue
                    seen_url.add(norm)
                    out.append({"title": c_title, "url": c_url})
        return out

    def _encrypt_request_body(self, encrypt: dict, filled: dict) -> str:
        """把请求体 JSON 序列化后 AES-CBC 加密为 base64（源 JS 等价逻辑）。"""
        import json as _json

        text = _json.dumps(filled, ensure_ascii=False, separators=(",", ":"))
        if self._decrypter is not None:
            return self._decrypter.aes_cbc_encrypt_text(
                str(encrypt.get("key") or ""), str(encrypt.get("iv") or ""), text
            )
        raise ContentMissingError("请求体需 AES 加密但未配置解密器", source_id="")

    # ------------------------------------------------------------------ #
    def fetch_chapter(self, source: SourceConfig, url: str, use_cache: bool = True) -> str:
        """抓取单章正文。按类型取正文选择器，支持章节分页拼接。

        长章节在部分站点会拆成多页（如 xxx.html / xxx_1.html / xxx_2.html）。
        若源配置 content.chapter.pagination 开启，则通过页脚导航「下一章」
        链接判定：URL 基路径（去 _<数字>.html 后缀）与当前页相同 → 同章续页，
        继续抓取并拼接；否则为真正的下一章，停止。
        """
        content_cfg = self._content_block(source)
        if source.content_type == "novel":
            block = content_cfg.get("chapter") or {}
        elif source.content_type == "comic":
            block = content_cfg.get("page") or {}
        else:
            block = content_cfg.get("episode") or {}
        # 章内分页（source-schema §9.2 body.paginator）：novel 默认开启自动探测，
        # 长章节跨多页（xxx.html / xxx_1.html）时拼接；seen+max 防死循环。
        pag_cfg = (block or {}).get("pagination") or {}
        pag_enabled = bool(pag_cfg.get("enabled", True))
        # 正文抓取（可能分页）
        pages = []
        cur = url
        seen = set()
        max_pages = int(pag_cfg.get("max_pages") or 20)
        # 开头：cached body 命中直接返回（重启后/预加载后免抓）
        cache_key = self._chapter_cache_key(source, url)
        cached = self._cache_get(cache_key) if use_cache else None
        if cached is not None:
            return cached
        return self._inflight_call(
            ("body", source.source_id, self._abs_url(source, url), bool(use_cache)),
            lambda: self._fetch_chapter_body(
                source, url, pag_enabled, max_pages, use_cache, cache_key
            ),
        )

    def _fetch_chapter_body(
        self,
        source: SourceConfig,
        url: str,
        pag_enabled: bool,
        max_pages: int,
        use_cache: bool,
        cache_key: str,
    ) -> str:
        """抓取章节正文（含分页拼接），供 fetch_chapter 去重复用。"""
        cur = url
        seen = set()
        pages = []
        while cur and len(pages) < max_pages:
            page_text, nxt = self._fetch_chapter_page(source, cur, pag_enabled)
            if page_text:
                pages.append(page_text)
            # 分页判定：URL 路径去掉 _<数字>.html 后相同且数量不递增，继续抓取
            if not nxt:
                break
            nxt_abs = self._abs_url(source, nxt)
            cur_base = self._chapter_base_for(source, cur)
            nxt_base = self._chapter_base_for(source, nxt_abs)
            if cur_base == nxt_base and nxt_abs not in seen:
                seen.add(nxt_abs)
                cur = nxt_abs
                continue
            break  # 路径不同或目录数不递增则停止分页
        text = "\n".join(pages)
        # 末尾写 body：保留 7 天，shelf 池
        if text and use_cache:
            self._cache_set(cache_key, text, ttl=7 * 86400)
        return text
    def fetch_bookstack_chapter(
        self, source: SourceConfig, url: str
    ) -> BookStackChapterResult:
        if source.source_id != "bookstack":
            raise ValueError("fetch_bookstack_chapter requires source_id=bookstack")
        self._bg_check(source, self._abs_url(source, url))
        body = (self._content_block(source).get("chapter") or {}).get("body") or {}
        if body.get("render") == "playwright":
            html = self._fetch_rendered_html(source, url, body.get("render_config") or {})
        else:
            html = self._get(source, url)
        doc = self._parser.parse(html)
        block = (self._content_block(source).get("chapter") or {})
        selector = (block.get("body") or {}).get("selector")
        roots = self._image_selector_nodes(self._parser, doc, selector)
        if not roots:
            raise ContentMissingError("BookStack 正文选择器未命中", source_id=source.source_id)
        from lxml.html import tostring

        image_urls = []
        seen = set()
        parts = []
        base_url = self._abs_url(source, url)
        for root in roots:
            for node in root.iter():
                if node.tag == "img":
                    image_value = self._image_url_from_node(node)
                    if image_value:
                        absolute = urljoin(base_url, image_value)
                        node.set("src", absolute)
                        if absolute not in seen:
                            seen.add(absolute)
                            image_urls.append(absolute)
                    continue
                for attr in ("href", "src"):
                    value = (node.get(attr) or "").strip()
                    if not value or value.startswith("data:"):
                        continue
                    if attr == "href" and value.startswith("#"):
                        # Keep Markdown's in-page anchors local to the rich
                        # document so the reader can scroll instead of loading
                        # the same chapter again.
                        continue
                    node.set(attr, urljoin(base_url, value))
            parts.append(tostring(root, encoding="unicode"))
        body_html = "".join(parts)
        return BookStackChapterResult(
            body_html=body_html,
            text=_strip_html_tags(body_html),
            image_urls=image_urls,
        )

    def fetch_chapter_with_images(self, source: SourceConfig, url: str) -> tuple[str, list[str]]:
        content_cfg = self._content_block(source)
        if source.content_type == "novel":
            block = content_cfg.get("chapter") or {}
        elif source.content_type == "comic":
            block = content_cfg.get("page") or {}
        else:
            block = content_cfg.get("episode") or {}
        pag_cfg = block.get("pagination") or {}
        pag_enabled = bool(pag_cfg.get("enabled", True))
        max_pages = int(pag_cfg.get("max_pages") or 20)
        pages, images, seen_pages, seen_images = [], [], set(), set()
        cur = url
        while cur and len(pages) < max_pages:
            page_text, nxt, page_images = self._fetch_chapter_page(
                source, cur, pag_enabled, include_images=True
            )
            if page_text:
                pages.append(page_text)
            for entry in page_images:
                if not isinstance(entry, dict):
                    if isinstance(entry, str) and entry and entry not in seen_images:
                        seen_images.add(entry)
                        images.append({"type": "image", "url": entry})
                    continue
                if entry.get("type") == "text":
                    images.append({"type": "text", "text": str(entry.get("text") or "")})
                    continue
                image_url = entry.get("url")
                if not isinstance(image_url, str) or not image_url:
                    continue
                image_url = self._abs_url(source, image_url)
                if _AD_IMAGE_RE.search(image_url) or image_url in seen_images:
                    continue
                seen_images.add(image_url)
                images.append({"type": "image", "url": image_url})
            if not nxt:
                break
            nxt_abs = self._abs_url(source, nxt)
            cur_abs = self._abs_url(source, cur)
            if self._chapter_base_for(source, cur_abs) != self._chapter_base_for(source, nxt_abs) or nxt_abs in seen_pages:
                break
            seen_pages.add(nxt_abs)
            cur = nxt_abs
        return "\n".join(pages), images

    def precache_chapters(
        self,
        source: SourceConfig,
        chapters,
        current_idx: int,
        ahead: int = 3,
        enabled: bool = True,
        use_cache: bool = True,
    ) -> None:
        """后台预加载当前章+后 ahead 章正文到缓存。不满 ahead 按实际。

        enabled=False 时完全 no-op（设置页关闭预加载）。
        chapters：可迭代对象，元素含 .url 属性。current_idx 为当前章下标。
        """
        if not enabled:
            return
        if not chapters:
            return
        if use_cache and self._cache is None and self._repository is None:
            return
        ahead = max(0, int(ahead))
        end = min(current_idx + ahead + 1, len(chapters))
        for i in range(current_idx, end):
            ch = chapters[i]
            url = getattr(ch, "url", "")
            if not url:
                continue
            key = self._chapter_cache_key(source, url)
            if use_cache and self._cache_get(key) is not None:
                continue  # 已缓存
            try:
                try:
                    text = self.fetch_chapter(source, url, use_cache=use_cache)
                except TypeError as exc:
                    if "use_cache" not in str(exc):
                        raise
                    text = self.fetch_chapter(source, url)
            except Exception as exc:  # noqa: BLE001
                log.warning("[cache] 预加载失败 %s: %s", url, exc)
                continue
            if text and use_cache:
                self._cache_set(key, text, ttl=7 * 86400)

    def _chapter_cache_key(self, source: SourceConfig, url: str) -> str:
        """Build a versioned chapter cache key when a source changes decoding."""
        version = (source.raw.get("chapter_cache_version") if source else None) or ""
        marker = f":v{version}" if version else ""
        return f"body:{source.source_id}{marker}:{self._abs_url(source, url)}"

    @staticmethod
    def _image_selector_nodes(parser, doc, selector):
        if isinstance(selector, list):
            for item in selector:
                nodes = ChapterMixin._image_selector_nodes(parser, doc, item)
                if nodes:
                    return nodes
            return []
        if isinstance(selector, dict) and "fallback" in selector:
            return ChapterMixin._image_selector_nodes(parser, doc, selector.get("fallback"))
        if not isinstance(selector, dict):
            return []
        return parser._query(doc, selector.get("css"), selector.get("xpath"))

    def _extract_body_fragments(self, source, url, doc, selector):
        fragments = []
        seen = set()

        block_tags = {
            "address", "article", "blockquote", "div", "dl", "h1", "h2",
            "h3", "h4", "h5", "h6", "li", "ol", "p", "pre", "section",
            "table", "ul",
        }

        def add_text(value):
            value = str(value or "")
            if value and (value.strip() or value == "\n"):
                if fragments and fragments[-1].get("type") == "text":
                    fragments[-1]["text"] += value
                else:
                    fragments.append({"type": "text", "text": value})

        def walk(node, is_root=False):
            if node.tag == "img":
                raw_url = self._image_url_from_node(node)
                if raw_url:
                    image_url = urljoin(self._abs_url(source, url), raw_url)
                    if image_url not in seen and not _AD_IMAGE_RE.search(image_url):
                        seen.add(image_url)
                        fragments.append({"type": "image", "url": image_url})
                return
            add_text(node.text)
            for child in node:
                walk(child)
                add_text(child.tail)
            if node.tag in block_tags and not is_root:
                add_text("\n")

        for root in self._image_selector_nodes(self._parser, doc, selector):
            walk(root, is_root=True)
        if fragments and fragments[-1].get("type") == "text":
            fragments[-1]["text"] = fragments[-1]["text"].rstrip("\n")
        return fragments

    def _extract_body_images(self, source, url, doc, selector):
        entries = []
        for root in self._image_selector_nodes(self._parser, doc, selector):
            text_offset = 0
            for node in root.iter():
                if node.tag == "img":
                    raw_url = self._image_url_from_node(node)
                    if not raw_url:
                        continue
                    image_url = urljoin(self._abs_url(source, url), raw_url)
                    if _AD_IMAGE_RE.search(image_url):
                        continue
                    if image_url not in {entry["url"] for entry in entries}:
                        entries.append({"url": image_url, "position": text_offset})
                if node.text:
                    text_offset += len(node.text)
                if node.tail:
                    text_offset += len(node.tail)
        return entries

    @staticmethod
    def _image_url_from_node(node) -> str:
        """Return the real lazy-loaded image URL, not a placeholder src."""
        for attr in ("data-src", "data-original", "data-lazy-src", "src"):
            value = (node.get(attr) or "").strip()
            if not value or value.startswith("data:"):
                continue
            lowered = value.lower()
            if any(token in lowered for token in ("loading", "placeholder", "lazyload", "nopic", "blank.gif")):
                continue
            return value
        return ""

    def _fetch_chapter_page(
        self, source: SourceConfig, url: str, pag_enabled: bool = True,
        include_images: bool = False,
    ) -> tuple:
        """抓取单页正文，返回 (plain_text, next_page_url 或 "")。

        自动探测分页链接（pag_enabled 时）：
        1. 源配置 content.chapter.pagination.next_selector 显式给出「下一页」选择器；
        2. 否则从页面导航（read-nav / .nav 等）找「下一页」文本的链接；
        3. 再兜底从页面找 _<数字>.html 的分页链接。
        返回的 nxt 是否同章续页由调用方 _chapter_base 判定。
        """
        content_cfg = self._content_block(source)
        if source.content_type == "novel":
            block = content_cfg.get("chapter") or {}
        elif source.content_type == "comic":
            block = content_cfg.get("page") or {}
        else:
            block = content_cfg.get("episode") or {}
        body = block.get("body") or {}
        selector = body.get("selector")

        api_cfg = body.get("api") or {}
        api_text = ""
        if api_cfg:
            api_text = self._fetch_chapter_api_text(source, url, api_cfg)
            html = api_text
        elif body.get("render") == "playwright":
            html = self._fetch_rendered_html(source, url, body.get("render_config") or {})
        else:
            self._bg_check(source, self._abs_url(source, url))
            html = self._get(source, url)
        doc = self._parser.parse(html)

        # API chapter payloads are already plaintext after decryption; do not
        # feed the Base64/decoded text through an HTML selector a second time.
        if api_text:
            text = self._normalize_para_indent(api_text)
            return (text, "", []) if include_images else (text, "")

        nxt = ""
        if pag_enabled:
            # 显式续页配置优先：body.paginator.next_link.selector（source-schema §9.2）
            body_pag = body.get("paginator") or {}
            next_sel = (body_pag.get("next_link") or {}).get("selector")
            if next_sel:
                nxt = self._parser.extract_first(doc, next_sel, self._abs_url(source, url))
            else:
                # 自动探测：导航区「下一页」链接
                nxt = self._detect_next_page(doc, source, url)
            if nxt:
                nxt = self._abs_url(source, nxt)

        image_entries = self._extract_body_fragments(source, url, doc, selector) if include_images else []

        if not selector:
            raise ContentMissingError("源未配置正文选择器", source_id=source.source_id)

        paragraphs = self._parser.extract(doc, selector)
        if not paragraphs:
            # 正文选择器未命中：可能是混淆/加密正文，尝试解密
            text = self._decrypt_chapter(source, html, url)
        else:
            text = "\n".join(paragraphs)
        # 小说正文段落开头空格规范化（18mh 等站原文多个空格缩进）
        if source.content_type == "novel":
            text = self._normalize_para_indent(text)
            # 字符映射解密（番茄小说字体混淆等）：decryption.targets.chapter.strategy=translit
            if self._decrypter is not None:
                # Font-face URLs are commonly relative to the chapter page;
                # resolve them before the font matcher downloads the glyph map.
                from framework.fanqie_font import extract_font_urls

                font_urls = [
                    urljoin(self._abs_url(source, url), font_url)
                    for font_url in extract_font_urls(html)
                ]
                targets = (source.raw.get("decryption") or {}).get("targets") or {}
                text_target = "content" if targets.get("content") else "chapter"
                text = self._decrypter.decrypt(
                    source, text, text_target, html=html, font_urls=font_urls
                )
        image_entries = image_entries if include_images else []
        # 正文广告行过滤（ad_block 引擎：剔除「广告/推广/点击领取」等特征行）
        try:
            from framework.adblock import adblock_for

            ad = adblock_for(source)
            if ad.enabled:
                text = ad.filter_text(text)
        except Exception:
            pass  # 广告过滤是增强能力，失败不影响正文
        return (text, nxt, image_entries) if include_images else (text, nxt)

    def _fetch_chapter_api_text(self, source, url: str, cfg: dict) -> str:
        """Fetch and decrypt a JSON chapter payload configured by the source."""
        from urllib.parse import parse_qs, urlencode, urlsplit

        match = _re.search(
            r"/(?:novel|book|comic|detail|bookinfo|program|shuku|chapter)/(\w+)",
            url,
        )
        parts = urlsplit(url)
        book_id = match.group(1) if match else ""
        chapter_id = parts.path.rstrip("/").split("/")[-1].split(".")[0]
        api_url = str(cfg.get("url") or "").replace("{id}", book_id).replace("{chapter_id}", chapter_id)
        params = dict(cfg.get("params") or {})
        for key, value in parse_qs(parts.query).items():
            if value:
                params.setdefault(key, value[0])
        query = urlencode({k: str(v).replace("{id}", book_id).replace("{chapter_id}", chapter_id) for k, v in params.items()})
        abs_url = self._abs_url(source, api_url)
        if query:
            abs_url += ("&" if "?" in abs_url else "?") + query
        payload = self._http.get_json(
            abs_url, headers=self._headers(source), timeout=self._timeout(source),
            retries=self._retries(source), proxy_pool=source.proxy_pool(),
        )
        value = self._jsonpath(payload, cfg.get("response_path") or "data.content.0.text")
        if not value:
            raise ContentMissingError("17K 正文接口未返回内容", source_id=source.source_id)
        text = str(value)
        if self._decrypter is not None:
            text = self._decrypter.decrypt(source, text, cfg.get("decrypt_target") or "chapter")
        return text

    def _fetch_rendered_html(self, source, url: str, render_config: dict) -> str:
        """Render a page with the same headers/cookies as the HTTP transport."""
        from framework.playwright_helper import fetch_rendered_html_sync

        headers = source.request_headers()
        browser_headers = {
            key: value for key, value in headers.items()
            if key.lower() not in {
                "user-agent", "cookie", "referer", "host", "content-length",
            }
        }
        cookies = []
        from urllib.parse import urlsplit

        host = (urlsplit(source.base_url).hostname or "").lstrip(".")
        cookie_header = next(
            (value for key, value in headers.items() if key.lower() == "cookie"),
            "",
        )
        referer = next(
            (value for key, value in headers.items() if key.lower() == "referer"),
            "",
        )
        if host and cookie_header:
            for item in cookie_header.split(";"):
                name, sep, value = item.strip().partition("=")
                if sep and name and value:
                    cookies.append({"name": name, "value": value, "domain": host, "path": "/"})
        rendered = fetch_rendered_html_sync(
            self._abs_url(source, url),
            wait_for=render_config.get("wait_for") or "",
            wait_until=render_config.get("wait_until") or "domcontentloaded",
            timeout_ms=int(render_config.get("timeout_ms") or 30000),
            extra_delay_ms=int(
                render_config["extra_delay_ms"]
                if "extra_delay_ms" in render_config
                else 2000
            ),
            proxy=source.transports().get("proxy"),
            cookies=cookies or None,
            referer=referer,
            headers=browser_headers,
        )
        if not rendered:
            raise ContentMissingError(
                f"渲染页面为空（{url}）", source_id=source.source_id
            )
        return rendered

    @staticmethod
    def _normalize_para_indent(text: str) -> str:
        """段落开头空格规范化：前导空白（半角/全角）>2 的行统一为 2 个全角缩进。

        18mh 等站正文段落原文用多个半角空格缩进（实测 29 个），中文排版
        标准是「　　」两格全角。只压缩**异常多**的前导空白，正常缩进/无
        缩进/标题行保持原样，避免误伤。
        """
        out = []
        for ln in (text or "").split("\n"):
            stripped = ln.lstrip(" \t　")
            lead = len(ln) - len(stripped)
            if lead > 2 and stripped:
                out.append("　　" + stripped)
            else:
                out.append(ln)
        return "\n".join(out)

    def _detect_next_page(self, doc, source: SourceConfig, url: str) -> str:
        """自动探测章节分页的「下一页」链接。

        依次尝试：
        1. 文本为「下一页/下页/尾页」的 <a> 链接；
        2. 页脚导航（read-nav / pagenav 等）内与当前页**同基路径**的链接
           （即 _<数字>.html 分页续页）；
        3. 页面任意同基路径的 _<数字>.html 链接。
        返回原始 href（可能相对），无则空串。
        """
        base = self._chapter_base_for(source, self._abs_url(source, url))
        url_abs = self._abs_url(source, url)

        # 页脚导航内同基路径链接（read-nav / pagenav 等）。
        # 导航按「上一章 | 目录 | 下一章」排列；同章续页链接带 _<数字>.html
        # 后缀且基路径相同，取导航中**最后一个**这样的链接（"下一页"通常在末尾）。
        # 自指链接（href 指向当前页）排除。
        for cls in ("read-nav", "chapter-page-nav", "pagenav", "bottem1"):
            for nav in doc.xpath(f'//*[contains(concat(" ", normalize-space(@class), " "), " {cls} ")]'):
                match = ""
                for a in nav.xpath('.//a[@href]'):
                    href = a.get("href") or ""
                    if not href:
                        continue
                    href_abs = self._abs_url(source, href)
                    if (self._chapter_base_for(source, href_abs) == base
                            and href_abs != url_abs):  # 同基路径 → 续页
                        match = href
                if match:
                    return match
        # 兜底：页面任意同基路径的 _<数字>.html 链接
        for a in doc.xpath('//a[@href]'):
            href = a.get("href") or ""
            if (href and self._chapter_base_for(source, self._abs_url(source, href)) == base):
                return href
        return ""

    def _chapter_base_for(self, source: SourceConfig, url: str) -> str:
        """按源配置归一章节 URL，仅用于分页归属判断。"""
        content_cfg = self._content_block(source)
        if source.content_type == "novel":
            block = content_cfg.get("chapter") or {}
        elif source.content_type == "comic":
            block = content_cfg.get("page") or {}
        else:
            block = content_cfg.get("episode") or {}
        pagination = block.get("pagination") or {}
        pattern = pagination.get("same_chapter_regex")
        if pattern:
            try:
                replacement = pagination.get("same_chapter_replacement") or ""
                return _re.sub(str(pattern), str(replacement), url)
            except (TypeError, ValueError, _re.error):
                log.warning(
                    "[%s] same_chapter_regex 无效，回退默认章节分页规则",
                    source.source_id,
                )
        return self._chapter_base(url)

    @staticmethod
    def _chapter_base(url: str) -> str:
        """去 URL 的 _<数字> 分页后缀，返回基路径（兼容 .html/.htm/.shtml/.php/无后缀）。

        https://x/1/73976498_1.html → https://x/1/73976498.html
        https://x/1/73976498_2.htm  → https://x/1/73976498.htm
        https://x/1/73976498.html   → https://x/1/73976498.html
        """
        import re as _re2
        return _re2.sub(r"_(\d+)(?=\.\w+$|$)", "", url)

    def _decrypt_chapter(self, source: SourceConfig, html: str, url: str) -> str:
        """正文被混淆时的解密路径（源配置 decryption 驱动）。"""
        dec_cfg = source.raw.get("decryption") or {}
        if not dec_cfg:
            raise ContentMissingError(
                f"未解析到正文内容（{url}）", source_id=source.source_id
            )
        if self._decrypter is None:
            raise ContentMissingError(
                f"正文需解密但未配置解密器（{url}）", source_id=source.source_id
            )
        content_cfg = dec_cfg.get("targets", {}).get("content") or {}
        source_type = content_cfg.get("source", "obfuscated_script")
        try:
            if source_type == "obfuscated_script":
                # 从混淆脚本提取 base64 并解码（extract_base64_blocks 已解码）
                text = self._decrypter.extract_base64_blocks(html)
                if not text:
                    raise ContentMissingError(
                        f"混淆正文提取为空（{url}）", source_id=source.source_id
                    )
                # 解码后可能含 <p>...</p> 等 HTML 标签（混淆编码原样保留了标签结构），
                # 需要去标签转纯文本；无标签则直接返回
                if _re.search(r"<[a-z]", text, _re.IGNORECASE):
                    text = _strip_html_tags(text)
                return text
            # 其他 source 类型：把原始内容交给 decrypt（按 strategy 处理）
            return self._decrypter.decrypt(source, html, "content")
        except ContentMissingError:
            raise
        except Exception as exc:
            raise ContentMissingError(
                f"正文解密失败（{url}）：{exc}", source_id=source.source_id
            )

    # ------------------------------------------------------------------ #
    def _inflight_call(self, key, worker):
        """进程内并发去重：同一 key 的抓取只发一份网络请求。

        并发调用方复用同一份在途结果（结果/异常共享）；取消、代际守卫等
        取消路径仍由调用方负责（本层仅负责去重，不捕获取消信号）。
        """
        with self._inflight_lock:
            bucket = self._inflight.get(key)
            first = bucket is None
            if first:
                bucket = {
                    "event": threading.Event(),
                    "result": None,
                    "error": None,
                    "done": False,
                }
                self._inflight[key] = bucket
        if not first:
            bucket["event"].wait()
            if bucket["error"] is not None:
                raise bucket["error"]
            result = bucket["result"]
            return list(result) if isinstance(result, list) else result
        try:
            result = worker()
            with self._inflight_lock:
                bucket["result"] = result
                bucket["done"] = True
                bucket["event"].set()
                self._inflight.pop(key, None)
            return result
        except Exception as exc:  # noqa: BLE001
            with self._inflight_lock:
                bucket["error"] = exc
                bucket["done"] = True
                bucket["event"].set()
                self._inflight.pop(key, None)
            raise

    # ------------------------------------------------------------------ #
