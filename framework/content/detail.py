"""详情扩展：系列展开、api/ytdlp 详情、初始状态、漫画书栈章节。

B4 拆分自 content.py，方法原样迁移，行为不改变。
"""

from __future__ import annotations

from .shared import (
    Chapter, ContentMissingError, Detail, _re,
    json, log, utils,
)


class DetailMixin:
    def _maybe_expand_video_series(self, source, detail, chapters) -> list:
        """把同系列的独立分部并入视频分集列表（配置 content.episode.series 驱动）。

        - base_from_title：提取系列基底的正则（字符串或正则列表，如
          `(.+?)第(\\d+)[話话]`、`(.+?)(?:前編|後編)`——base=捕获组1，话数=捕获组2
          数字或 part_map 文本标记映射）
        - part_map：文本分部标记 → 话数（如 {"前編":1,"後編":2}）
        - min_parts：分部数 ≥ 该值才聚合（防误把普通视频当系列）
        返回章节列表：当前集置首，其余分部按话数正序在后。
        """
        block = self._content_block(source) or {}
        s_cfg = (block.get("episode") or {}).get("series") or {}
        if not s_cfg.get("enabled"):
            return chapters
        patterns = self._series_patterns(s_cfg)
        if not patterns:
            return chapters
        match = self._series_match(detail.title or "", patterns, s_cfg.get("part_map") or {})
        if not match:
            return chapters  # 标题无系列标记 → 非系列作品
        base, _ = match
        # 搜系列基底，收集分部 {话数: (标题, url)}（不限精确基底——站点可能以
        # 中文/日文不同标题展示同系列，如 hanime1 详情日文、搜索中文）
        parts = self._search_video_series_parts(source, base, patterns, s_cfg.get("part_map") or {})
        min_parts = int(s_cfg.get("min_parts") or 2)
        if len(parts) < min_parts:
            return chapters  # 分部太少，保持单集
        # 确认同一系列：当前视频 URL 出现在搜索结果中（标题语言可能不同，
        # 用 URL 判定最可靠）。
        cur_url = detail.url
        if cur_url not in {u for _, u in parts.values()}:
            return chapters  # 搜索没命中当前集，可能是误判系列
        cur = chapters[0] if chapters else None
        merged = [cur] if cur is not None else []
        for n in sorted(parts):
            title, url = parts[n]
            if url == cur_url:
                continue  # 当前集已置首
            merged.append(Chapter(title=title, url=url))
        return merged

    @staticmethod
    def _series_patterns(s_cfg: dict) -> list:
        """规范化 series.base_from_title 为正则列表。"""
        raw = s_cfg.get("base_from_title") or ""
        if isinstance(raw, str):
            raw = [raw]
        return [p for p in raw if p]

    def _series_match(self, title: str, patterns: list, part_map: dict):
        """在标题上尝试系列模式，返回 (base, part) 或 None。"""
        import re as _re

        for pat in patterns:
            m = _re.search(pat, title or "")
            if not m:
                continue
            base = (m.group(1) or "").strip()
            if not base:
                continue
            part = None
            if m.lastindex and m.lastindex >= 2:
                g2 = (m.group(2) or "").strip()
                if g2.isdigit():
                    part = int(g2)
            if part is None:
                # 文本分部标记（前編/後編/上篇/下篇…）→ part_map
                for k, v in (part_map or {}).items():
                    if k in (title or ""):
                        part = v
                        break
            if part is not None:
                return base, part
        return None

    def _search_video_series_parts(self, source, base: str, patterns: list, part_map: dict) -> dict:
        """搜系列基底，返回分部 {话数: (标题, URL)}。失败返回 {}。

        不限精确基底匹配（同一系列在 hanime1 详情/搜索用不同语言标题），
        由调用方用「当前 URL 命中」确认系列。
        """
        try:
            from framework.search import Search

            results = Search(self._http, self._parser, self._checker).search_one(source, base)
        except Exception:  # noqa: BLE001
            return {}
        parts = {}
        for r in results:
            match = self._series_match(r.title or "", patterns, part_map)
            if not match:
                continue
            _, rpart = match
            parts.setdefault(rpart, (r.title or "", r.url or ""))
        return parts

    def _fetch_detail_ytdlp(self, source: SourceConfig, url: str, cfg: dict, context=None) -> Detail:
        """yt-dlp 引擎：详情元数据 + 章节列表。"""
        self._check_stream_context(context)
        yt = self._get_ytdlp()
        try:
            d = self._call_with_remaining_timeout(
                yt.fetch_detail, (url,), {},
                context.deadline if context is not None else None,
            )
        except Exception as exc:
            raise ContentMissingError(
                f"yt-dlp 详情失败（{url}）：{exc}", source_id=source.source_id
            ) from exc
        self._check_stream_context(context)
        detail = Detail(
            source_id=source.source_id,
            content_type=source.content_type,
            url=url,
            title=d.get("title") or "",
            author=d.get("author") or "",
            cover=d.get("cover") or "",
            status=d.get("status") or "",
            summary=d.get("summary") or "",
            actor=d.get("actor") or "",
        )
        chapters = d.get("chapters") or []
        detail.chapters = [Chapter(title=c.get("title") or "", url=c.get("url") or url) for c in chapters]
        # 站内相关推荐（detail.related 块，如 pornhub 详情 HTML 的 relatedVideos
        # JS 变量）：优先用详情页内嵌的相关影片，替代同源关键词搜索。详情元数据
        # 走 ytdlp，但详情 URL 本身可 GET（复用 transports 请求头）。异常静默空。
        related_cfg = cfg.get("related") or {}
        if related_cfg:
            related: List[dict] = []
            try:
                html = self._get(source, url)

                pat = str(

                    related_cfg.get("js_regex")
                    or r"relatedVideos\s*=\s*(\[.*?\]);"
                )
                m = _re.search(pat, html, _re.DOTALL)
                data = json.loads(m.group(1)) if m else None
                if isinstance(data, list):
                    own = (detail.url or "").rstrip("/").lower()
                    rel_max = int(related_cfg.get("max") or 8)
                    for it in data:
                        if not isinstance(it, dict):
                            continue
                        vkey = it.get("vkey")
                        title = it.get("title") or ""
                        if not vkey or not title:
                            continue  # 缺 vkey/title 无法构造卡片
                        r_url = self._abs_url(
                            source, "/view_video.php?viewkey=" + str(vkey)
                        )
                        if r_url.rstrip("/").lower() == own:
                            continue  # 剔除自身
                        related.append({
                            "title": title,
                            "url": r_url,
                            "cover": "",  # 未知字段留空，卡片支持无封面纯文字
                        })
                        if len(related) >= rel_max:
                            break
            except Exception:  # noqa: BLE001
                related = []  # 相关推荐失败不影响主流程
            detail.related = related
        return detail

    # ------------------------------------------------------------------ #
    def _fetch_detail_api(self, source: SourceConfig, url: str, cfg: dict, context=None) -> Detail:
        """api_endpoints.detail：JSON API 取详情元数据 + 章节列表。

        支持 sign 签名。字段映射用 field_extractors（目标字段 → JSONPath）。
        章节列表用 chapters 块：
            items          JSONPath 到章节列表
            title / number 每项标题/序号字段名
            url_template   章节 URL 模板（可用 {cid} / {page} / {part} 占位）
        """
        from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

        self._check_stream_context(context)
        api_url = str(cfg.get("url") or "")
        params = cfg.get("params") or {}
        filled = {}
        # 详情 URL 的 query（?title=... 等）并入占位符：detail POST 接口需要
        # 精确标题等额外字段（如 ikanpp /api/detail 要 title 才命中正确 vod），
        # bvid 只取 URL 的 path 末段（BV 号优先，query 不参与）。
        _sp = urlsplit(url)
        m_bv = _re.search(r"(BV[0-9A-Za-z]+)", _sp.path)
        bvid = m_bv.group(1) if m_bv else _sp.path.rstrip("/").split("/")[-1] or url
        if bvid.lower().endswith(".html"):
            bvid = bvid[:-5]
        _ph = {"id": bvid, "bvid": bvid}
        if _sp.query:
            _ph.update({k: v[0] for k, v in parse_qs(_sp.query).items()})
        method = (cfg.get("method") or "GET").upper()
        if method == "POST":
            # JSON API（GraphQL 等）：POST body 递归替换 {id}/{bvid}/{query 参数}
            body_filled = utils.fill_json(
                cfg.get("body") or {}, **_ph
            )
            for k, v in params.items():
                body_filled.setdefault(k, utils.fill_json(v, **_ph))
            sign_cfg = cfg.get("sign") or {}
            strategy = sign_cfg.get("strategy")
            if strategy:
                from framework.signers import get_signer

                signer = get_signer(strategy, self._http)
                body_filled = signer.sign(body_filled)
            resp = self._http.post_json(
                urljoin(source.base_url, api_url),
                json_body=body_filled,
                headers=self._endpoint_headers(source, cfg),
                timeout=(min(self._timeout(source), context.remaining()) if context is not None else self._timeout(source)),
                retries=(0 if context is not None else self._retries(source)),
                proxy_pool=source.proxy_pool(),
            )
            self._check_stream_context(context)
        else:
            # URL 路径占位符同样替换（同 episode 分支处理，avgood 类路径 {id} 原样发出会 404）
            api_url = str(cfg.get("url") or "")
            for _k, _v in _ph.items():
                api_url = api_url.replace("{" + _k + "}", str(_v))
            for k, v in params.items():
                _val = str(v)
                for _pk, _pv in _ph.items():
                    _val = _val.replace("{" + _pk + "}", str(_pv))
                filled[k] = _val
            sign_cfg = cfg.get("sign") or {}
            strategy = sign_cfg.get("strategy")
            if strategy:
                from framework.signers import get_signer

                signer = get_signer(strategy, self._http)
                filled = signer.sign(filled)
            qs = urlencode(filled)
            abs_url = urljoin(source.base_url, api_url)
            if "?" in api_url:
                abs_url = f"{abs_url}&{qs}"
            else:
                abs_url = f"{abs_url}?{qs}"
            resp = self._http.get_json(
                abs_url,
                headers=self._endpoint_headers(source, cfg),
                timeout=(min(self._timeout(source), context.remaining()) if context is not None else self._timeout(source)),
                retries=(0 if context is not None else self._retries(source)),
                proxy_pool=source.proxy_pool(),
            )
            self._check_stream_context(context)
        if not isinstance(resp, dict):
            return Detail(source_id=source.source_id, content_type=source.content_type, url=url)
        data = resp.get("data") if isinstance(resp.get("data"), dict) else resp

        extractors = cfg.get("field_extractors") or {}
        title = self._jsonpath(data, extractors.get("title")) or ""
        detail = Detail(
            source_id=source.source_id,
            content_type=source.content_type,
            url=self._abs_url(source, url),
            title=str(title),
            author=str(self._jsonpath(data, extractors.get("author")) or ""),
            cover=str(self._jsonpath(data, extractors.get("cover")) or ""),
            status=str(self._jsonpath(data, extractors.get("status")) or ""),
            summary=str(self._jsonpath(data, extractors.get("summary")) or ""),
            actor=str(self._jsonpath(data, extractors.get("actor")) or ""),
        )
        # 标签（可空，逗号分隔列表）
        tags = self._jsonpath(data, extractors.get("tags"))
        if isinstance(tags, list):
            detail.tags = [str(t) for t in tags]
        elif tags:
            detail.tags = [str(t) for t in str(tags).split(",")]

        # 章节列表
        chapters_cfg = cfg.get("chapters") or {}
        items = self._jsonpath(data, chapters_cfg.get("items"))
        if isinstance(items, list):
            title_key = chapters_cfg.get("title") or "title"
            num_key = chapters_cfg.get("number")
            url_tpl = chapters_cfg.get("url_template") or url
            for it in items:
                if not isinstance(it, dict):
                    continue
                c_title = str(it.get(title_key) or f"第{len(detail.chapters)+1}集")
                c_url = url_tpl
                if num_key:
                    c_url = c_url.replace("{cid}", str(it.get(num_key) or ""))
                c_url = c_url.replace("{bvid}", bvid).replace("{id}", bvid)
                # 模板里 {title}/{part} 等占位填充
                c_url = utils.fill_template(c_url, it)
                provider = it.get("provider") or it.get("source") or ""
                if not isinstance(provider, str):
                    provider = ""
                detail.chapters.append(Chapter(title=c_title, url=c_url, provider=provider))
        chapters_api = chapters_cfg.get("api") or {}
        if chapters_api and hasattr(self, "_fetch_chapters_api"):
            try:
                api_items = self._fetch_chapters_api(source, chapters_api, url, "")
                if api_items is not None:
                    detail.chapters = [
                        Chapter(title=item["title"], url=item["url"])
                        for item in api_items
                        if item.get("title") and item.get("url")
                    ]
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] API 目录抓取失败：%s", source.source_id, exc)
        return detail

    @staticmethod
    def _jsonpath(node, path: str):
        """极简 JSONPath：data.owner.name / data.list.0 点号路径。"""
        return utils.jsonpath(node, path)

    def _extract_cover_fallback(
        self, source: SourceConfig, html: str, cover_cfg, current: str = ""
    ) -> str:
        """详情封面兜底：选择器封面为空/命中占位时，按配置从 JSON-LD / INITIAL_STATE 取真实封面。

        fields.cover 可选键（配置驱动，全部容错，失败保留原值）：
            regex       对原始 html 做 re.search 取 group(1)（如 JSON-LD image[0]）
            state       解析 window.__INITIAL_STATE__ 后按 JSONPath 取（如 page.thumbUri）
            placeholder 共享占位封面标记（子串/正则，命中视为无有效封面 → 触发兜底）
        尝试顺序：regex → state。
        """
        # 选择器命中但节点无有效 src（如番茄详情页 SSR 模板把 src 清空）时
        # extract_first 会把 base_url 当作落点返回；这类退化值必须视为无封面
        # 继续走兜底，否则垃圾 URL 会写进收藏库且永远不再重补。
        base = (getattr(source, "base_url", "") or "").rstrip("/")
        if (current or "").rstrip("/") == base:
            current = ""
        if current and not self._is_placeholder_cover(current, cover_cfg):
            return current
        cfg = cover_cfg or {}
        # 1) 正则兜底（JSON-LD image 等）
        pat = cfg.get("regex")
        if pat:
            m = _re.search(pat, html)
            if m:
                cover = m.group(1) if m.groups() else m.group(0)
                cover = cover.replace("\\/", "/").strip()
                if cover:
                    return utils.abs_url(source.base_url, cover)
        # 2) INITIAL_STATE 兜底（page.thumbUri 等）
        state_path = cfg.get("state")
        if state_path:
            state = self._parse_initial_state(html)
            if state:
                val = self._jsonpath(state, state_path)
                if val:
                    return str(val)
        return current

    @staticmethod
    def _is_placeholder_cover(cover: str, cover_cfg) -> bool:
        """封面是否命中配置的占位标记（命中 → 视为无有效封面，触发兜底）。"""
        cfg = cover_cfg if isinstance(cover_cfg, dict) else {}
        pat = cfg.get("placeholder")
        if not pat or not cover:
            return False
        try:
            return _re.search(str(pat), cover) is not None
        except Exception:  # noqa: BLE001  非法正则退化为子串匹配
            return str(pat) in cover

    @staticmethod
    def _parse_initial_state(html: str):
        """从 html 中提取 window.__INITIAL_STATE__ = {...}; 并解析为 dict。

        用大括号平衡扫描（跳过字符串内）切出 JSON，容错：找不到/解析失败返回 None。
        """
        if not html:
            return None
        marker = "window.__INITIAL_STATE__"
        idx = html.find(marker)
        if idx < 0:
            return None
        start = html.find("{", idx)
        if start < 0:
            return None
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(html)):
            c = html[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[start:i + 1])
                    except Exception:  # noqa: BLE001
                        return None
        return None

    # ------------------------------------------------------------------ #
    def _prepend_first_page(
        self, source: SourceConfig, doc, list_cfg: dict,
        detail_url: str, chapters: List[Chapter],
    ) -> None:
        """WordPress 帖子分页 current 页码补全（常规列表与 single_chapter
        播放清單共用）：详情页自身是 <span class="current">（无 href），列表
        只提取后续页 <a> → 缺第 1 集。启用 first_page_is_current 时读 current
        页号，若小于列表最小页号，把详情 URL 作为该页补入头部。就地修改
        chapters，无匹配/异常静默跳过。"""
        if not list_cfg.get("first_page_is_current") or not detail_url:
            return
        try:
            cur_vals = self._parser.extract(
                doc,
                {"css": "div.page-links span.current, div.page-links span.post-page-numbers"},
            )
            cur_num = -1
            for cv in cur_vals:
                cv = (cv or "").strip()
                if cv.isdigit():
                    cur_num = int(cv)
                    break
            if cur_num <= 0:
                return
            nums = []
            for ch in chapters:
                m = _re.match(r"^\s*(\d+)\s*$", ch.title or "")
                if m:
                    nums.append(int(m.group(1)))
            if not nums or cur_num < min(nums):
                chapters.insert(
                    0,
                    Chapter(
                        title=str(cur_num),
                        url=detail_url,
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "[%s] current 页码补全失败，跳过：%s", source.source_id, exc
            )

    def _fetch_bookstack_chapters(
        self, source: SourceConfig, doc, block: dict, detail_url: str = ""
    ) -> List[Chapter]:
        list_cfg = block.get("list") or {}
        root_selector = list_cfg.get("root_selector") or {"css": "ul.none-listyle"}
        if detail_url:
            match = _re.search(r"/books/([^/?#]+)", detail_url)
            if match:
                book_key = match.group(1)
                # SUMMARY.md is the canonical server-rendered reader entry and
                # avoids the redirect used by /read/book/{key}.
                reader_paths = (
                    f"/read/{book_key}/SUMMARY.md",
                    f"/read/{book_key}",
                    f"/read/book/{book_key}",
                )
                for reader_path in reader_paths:
                    reader_url = self._abs_url(source, reader_path)
                    try:
                        reader_html = self._get_detail_html(source, reader_url, reader_url)
                        reader_doc = self._parser.parse(reader_html)
                        reader_roots = []
                        for menu_selector in (
                            {
                                "xpath": (
                                    "//*[contains(concat(' ', normalize-space(@class), ' '), "
                                    "' article-menu-detail ')]/ul"
                                )
                            },
                            {"css": ".m-manual.manual-mode-view .article-menu-detail > ul"},
                            {"css": ".article-menu-detail ul"},
                        ):
                            reader_roots = self._parser._query(
                                reader_doc,
                                menu_selector.get("css"),
                                menu_selector.get("xpath"),
                            )
                            if reader_roots:
                                break
                        if reader_roots:
                            doc = reader_doc
                            root_selector = menu_selector
                            break
                    except Exception:
                        continue
        roots = self._parser._query(
            doc, root_selector.get("css"), root_selector.get("xpath")
        )
        chapters: List[Chapter] = []
        seen = set()

        def walk_li(child, depth):
            link = child.xpath("./a[@href][1]")
            if link:
                href = (link[0].get("href") or "").strip()
                title = link[0].text_content().strip()
                absolute = self._abs_url(source, href)
                # Sidebar links can contain fragment anchors for headings;
                # those are not separate chapters and must deduplicate.
                from urllib.parse import urldefrag

                absolute, _fragment = urldefrag(absolute)
                key = absolute.rstrip("/").lower()
                if href and title and key not in seen:
                    seen.add(key)
                    chapters.append(Chapter(title=title, url=absolute, depth=depth))
            for nested in child.xpath("./ul | ./ol"):
                walk_list(nested, depth + 1)

        def walk_list(list_node, depth=0):
            for child in list_node.xpath("./li"):
                walk_li(child, depth)

        for root in roots:
            if getattr(root, "tag", "") == "li":
                if root.getparent() is not None and root.getparent().get("class") == "none-listyle":
                    walk_li(root, 0)
            else:
                walk_list(root)
        return chapters

    # ------------------------------------------------------------------ #
