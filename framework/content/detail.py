"""详情扩展：系列展开、api/ytdlp 详情、初始状态、漫画书栈章节。

B4 拆分自 content.py，方法原样迁移，行为不改变。
"""

from __future__ import annotations

from .shared import (
    Chapter, ContentMissingError, Detail, StreamCancelledError, _re,
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
        fallback_cfg = cfg.get("fallback") or {}
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
        # 路径末段常带 slug（ikanpp 新版 `/title/ik130109-恐怖收集者`），接口只认
        # 纯 id。cfg.id_regex 从末段里抽出真正的主键；不配则保持原样（老源不受影响）。
        id_regex = cfg.get("id_regex")
        if id_regex:
            m_id = _re.search(str(id_regex), bvid)
            if m_id:
                bvid = m_id.group(1) if m_id.groups() else m_id.group(0)
                _ph["id"] = bvid
                _ph["bvid"] = bvid
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
            try:
                resp = self._http.post_json(
                    urljoin(source.base_url, api_url),
                    json_body=body_filled,
                    headers=self._endpoint_headers(source, cfg),
                    timeout=(min(self._timeout(source), context.remaining()) if context is not None else self._timeout(source)),
                    retries=(0 if context is not None else self._retries(source)),
                    proxy_pool=source.proxy_pool(),
                )
            except StreamCancelledError:
                raise
            except Exception:  # noqa: BLE001 —— 详情接口失效时走搜索回查兜底
                alt = (
                    self._detail_from_search_play(source, url, fallback_cfg, _ph, context)
                    if fallback_cfg.get("strategy")
                    else None
                )
                if alt is not None:
                    return alt
                raise
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
                # 占位符没填上（URL 里没带该参数）就整条丢掉：把字面量
                # `{source}` 发出去只会换来 400，比不发更糟。
                if _re.search(r"\{[^}]*\}", _val):
                    continue
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
            try:
                resp = self._http.get_json(
                    abs_url,
                    headers=self._endpoint_headers(source, cfg),
                    timeout=(min(self._timeout(source), context.remaining()) if context is not None else self._timeout(source)),
                    retries=(0 if context is not None else self._retries(source)),
                    proxy_pool=source.proxy_pool(),
                    direct=bool(source.transports().get("direct")),
                )
            except StreamCancelledError:
                raise
            except Exception:  # noqa: BLE001 —— 详情接口失效时走搜索回查兜底
                alt = (
                    self._detail_from_search_play(source, url, fallback_cfg, _ph, context)
                    if fallback_cfg.get("strategy")
                    else None
                )
                if alt is not None:
                    return alt
                raise
            self._check_stream_context(context)
        if not isinstance(resp, dict):
            return Detail(source_id=source.source_id, content_type=source.content_type, url=url)
        data = resp.get("data") if isinstance(resp.get("data"), dict) else resp

        extractors = cfg.get("field_extractors") or {}
        title = self._jsonpath(data, extractors.get("title")) or ""
        actor = self._extract_names(data, extractors.get("actor"))
        detail = Detail(
            source_id=source.source_id,
            content_type=source.content_type,
            url=self._abs_url(source, url),
            title=str(title),
            author=str(self._jsonpath(data, extractors.get("author")) or ""),
            cover=str(self._jsonpath(data, extractors.get("cover")) or ""),
            status=str(self._jsonpath(data, extractors.get("status")) or ""),
            summary=str(self._jsonpath(data, extractors.get("summary")) or ""),
            actor=" / ".join(actor) or str(self._jsonpath(data, extractors.get("actor")) or ""),
        )
        # 标签（可空，逗号分隔列表；也支持对象列表取指定字段名）
        tags_spec = extractors.get("tags")
        tags = self._extract_names(data, tags_spec)
        if not tags and isinstance(tags_spec, str):
            raw_tags = self._jsonpath(data, tags_spec)
            if isinstance(raw_tags, str) and raw_tags:
                tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
        if tags:
            detail.tags = tags

        # 元数据以页面 head 为准（api_endpoints.detail.meta_html）：接口只给分集/
        # 线路，标题/封面/简介只存在于详情页 og:* 标签（ikanpp 这类「接口拆家」站）。
        if cfg.get("meta_html"):
            self._fill_meta_from_html(source, detail, url)

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
        # 「线路 → 分集」嵌套结构的接口（ikanpp /api/lines：lines[].episodes[]）：
        # chapters.lines_path 指向线路数组，逐条线路把分集摊平成章节，线路名写
        # 进 provider 供播放界面按线路切换。已配置 items 的源不受影响。
        lines_path = chapters_cfg.get("lines_path")
        if lines_path:
            lines = self._jsonpath(data, lines_path)
            if isinstance(lines, list):
                line_name_key = chapters_cfg.get("line_name") or "sourceName"
                line_id_key = chapters_cfg.get("line_id") or "sourceId"
                items_key = chapters_cfg.get("line_items") or "episodes"
                title_key = chapters_cfg.get("title") or "title"
                url_key = chapters_cfg.get("url_key") or "url"
                line_chapters: list = []
                per_line: list = []
                for line in lines:
                    if not isinstance(line, dict):
                        continue
                    line_name = str(line.get(line_name_key) or "")
                    line_id = str(line.get(line_id_key) or "")
                    # 换线路 = 用同一作品 ID 换 source 参数重新打开详情；把目标
                    # 详情的 URL 挂在分集上，播放界面的线路菜单据此直接切换。
                    switch_url = self._line_switch_url(url, line_id)
                    episodes = line.get(items_key)
                    if not isinstance(episodes, list):
                        continue
                    line_eps: list = []
                    for it in episodes:
                        if not isinstance(it, dict):
                            continue
                        c_url = str(it.get(url_key) or "")
                        if not c_url:
                            continue
                        c_title = str(
                            it.get(title_key) or f"第{len(line_chapters)+1}集"
                        )
                        chapter = Chapter(
                            title=c_title, url=c_url, provider=line_name
                        )
                        if switch_url and line_id:
                            chapter.line_switch_url = switch_url
                            chapter.line_switch_source = line_id
                            chapter.line_name = line_name or line_id
                        line_eps.append(chapter)
                        line_chapters.append(chapter)
                    if line_eps:
                        per_line.append((line_id, line_eps))
                if line_chapters:
                    if str(chapters_cfg.get("pick_line") or "") == "direct_media":
                        # 「同一作品多条线路、其中只有一条是直链媒体」的接口
                        # （洛雪TV：jlm3u8 直链 / jlplayer 分享页；官采线路是
                        # 优酷/爱奇艺页面）。取第一条分集就是直链媒体的线路，
                        # 且**不建线路菜单**——线路坐标在同一 URL 里切不过去，
                        # 菜单项点下去只会用错 source 重新请求详情。
                        chosen = next(
                            (
                                eps
                                for _lid, eps in per_line
                                if eps and self._looks_like_direct_media(eps[0].url)
                            ),
                            None,
                        )
                        detail.chapters = [
                            Chapter(title=c.title, url=c.url, provider=c.provider)
                            for c in (chosen or line_chapters)
                        ]
                    else:
                        # 多线路接口（一行一集）不能把所有线路的集都当分集：21 条线
                        # 各 1 集会显示成「21 集」。按 URL 上的线路坐标只留当前线路
                        # 的分集，其它线路全部进线路菜单（source_list）供切换。
                        build_menu = getattr(self, "line_menu_from_chapters", None)
                        menu = build_menu(line_chapters) if build_menu else []
                        param = chapters_cfg.get("current_line_param") or "source"
                        current = self._query_param(url, param)
                        keep = [
                            ch for ch in line_chapters
                            if getattr(ch, "line_switch_source", "") == current
                        ] if current else []
                        if not keep and menu:
                            # URL 里的 source 是发现/搜索的来源坐标，不一定是线路
                            # id（ikanpp 的 lines 用 youxuan/jisu/…）→ 默认取首条线路
                            default_sid = menu[0].get("sid") or ""
                            keep = [
                                ch for ch in line_chapters
                                if getattr(ch, "line_switch_source", "") == default_sid
                            ]
                        detail.chapters = keep or line_chapters
                        if menu:
                            detail.source_list = menu
        # 接口能通但一条线路/分集都没有（第三方 id 在自家库没映射时就是这样）：
        # 同样回查搜索结果，别让用户对着一个「空详情」。
        if not detail.chapters and fallback_cfg.get("strategy") == "search_play_url":
            alt = self._detail_from_search_play(source, url, fallback_cfg, _ph, context)
            if alt is not None and alt.chapters:
                return alt
        # 单章即全片（单集视频 API 源）：详情页自身就是唯一分集，接口若不返回
        # 章节列表则按 content.<type>.single_chapter 补一条，避免播放页「无分集」。
        if not detail.chapters:
            block_key = (
                "chapter" if source.content_type == "novel"
                else "page" if source.content_type == "comic"
                else "episode"
            )
            block = (self._content_block(source) or {}).get(block_key) or {}
            if block.get("single_chapter"):
                detail.chapters.append(
                    Chapter(
                        title=detail.title or "全本",
                        url=self._abs_url(source, url),
                    )
                )
        self._mark_line_menu(source, detail, cfg, url, data)
        return detail

    def _detail_from_search_play(self, source: SourceConfig, url: str,
                                 fb: dict, ph: dict, context=None):
        """搜索结果自带 MacCMS 播放表（vod_play_from / vod_play_url）时直接成型。

        ikanpp 这类「自家库 + 第三方采集」站：`/api/lines` 只认自家库 id，
        第三方 vod_id 一律 400/404，搜索点进详情就「加载失败」。而搜索结果本身
        已经带完整播放表（每线路一串 `分集名$直链`），按标题+来源回查一次即可
        摊出线路与分集，不必依赖详情接口。

        配置（api_endpoints.detail.fallback）：
            strategy        "search_play_url"
            title_param     回查关键词取自 URL 的哪个 query 参数（缺省 title）
            source_param    来源参数名（缺省 source），命中则只查该采集源
            line_param      线路选择参数名（缺省 line），换线路时用它指回本线路
            from_field / url_field / line_names   播放表字段与线路显示名
        """
        from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

        # 兜底是一次额外的搜索请求（最多查 max_sources 个采集源）：剩余预算不够
        # 就直接放弃，别把详情页拖成「转圈十几秒」。
        if context is not None:
            try:
                if context.remaining() <= 5:
                    return None
            except Exception:  # noqa: BLE001 —— 预算读不出来就按可执行处理
                pass

        search_cfg = (source.raw.get("api_endpoints") or {}).get("search") or {}
        api_url = str(search_cfg.get("url") or "")
        body_tpl = search_cfg.get("body") or {}
        if not api_url or not body_tpl:
            return None
        title = str(ph.get(fb.get("title_param") or "title") or "").strip()
        if not title:
            return None
        hint = str(ph.get(fb.get("source_param") or "source") or "")
        line_hint = str(ph.get(fb.get("line_param") or "line") or "")
        body = utils.fill_json(body_tpl, keyword=title, page="1")
        sources = body.get("sources")
        if isinstance(sources, list) and sources:
            picked = [
                s for s in sources
                if isinstance(s, dict) and hint and str(s.get("id")) == hint
            ]
            if not picked:
                limit = max(1, int(fb.get("max_sources") or 25))
                picked = [s for s in sources if isinstance(s, dict)][:limit]
            body["sources"] = picked

        try:
            text = self._http.post_text(
                urljoin(source.base_url, api_url),
                json_body=body,
                headers=self._endpoint_headers(source, search_cfg),
                timeout=self._timeout(source),
                retries=0,
                proxy_pool=source.proxy_pool(),
                encoding=source.transports().get("charset") or "utf-8",
            )
        except Exception as exc:  # noqa: BLE001 —— 回查失败按详情失败处理
            log.info("[%s] 阶段=详情，搜索回查失败：%s", source.source_id, exc)
            return None

        videos: list = []
        for event in self._parse_sse_events(text):
            if event.get("type") == "videos":
                videos.extend(v for v in (event.get("videos") or []) if isinstance(v, dict))
        if not videos:
            return None
        videos.sort(key=lambda v: (
            bool(hint) and str(v.get("source") or "") != hint,
            str(v.get("vod_id") or "") != str(ph.get("id") or ""),
        ))
        video = next(
            (v for v in videos if self._titles_related(str(v.get("vod_name") or ""), title)),
            videos[0],
        )

        froms = [
            p.strip()
            for p in str(video.get(fb.get("from_field") or "vod_play_from") or "").split("$$$")
            if p.strip()
        ]
        blocks = str(video.get(fb.get("url_field") or "vod_play_url") or "").split("$$$")
        names = fb.get("line_names") or {}
        lines: list = []
        for idx, block in enumerate(blocks):
            from_name = froms[idx] if idx < len(froms) else f"line{idx + 1}"
            display = str(names.get(from_name) or from_name)
            chapters: list = []
            for item in block.split("#"):
                item = item.strip()
                if not item:
                    continue
                ep_name, ep_url = (
                    item.split("$", 1) if "$" in item else ("", item)
                )
                ep_url = ep_url.strip()
                if not ep_url.startswith(("http://", "https://")):
                    continue
                chapters.append(
                    Chapter(
                        title=ep_name.strip() or f"第{len(chapters) + 1}集",
                        url=self._abs_url(source, ep_url),
                        provider=display,
                    )
                )
            if chapters:
                lines.append((from_name, display, chapters))
        if not lines:
            return None

        base = urlsplit(self._abs_url(source, url))

        def _line_url(from_name: str) -> str:
            q = {k: v[0] for k, v in parse_qs(base.query, keep_blank_values=True).items()}
            q[fb.get("line_param") or "line"] = from_name
            return urlunsplit(base._replace(query=urlencode(q)))

        chosen = next(
            (ln for ln in lines if line_hint and line_hint in (ln[0], ln[1])),
            lines[0],
        )
        detail = Detail(
            source_id=source.source_id,
            content_type=source.content_type,
            url=self._abs_url(source, url),
            title=str(video.get("vod_name") or title),
            cover=str(video.get("vod_pic") or video.get("vod_pic_original") or ""),
            summary=str(video.get("vod_content") or video.get("vod_blurb") or ""),
            status=str(video.get("vod_remarks") or ""),
        )
        tags = str(video.get("vod_class") or video.get("vod_tag") or "")
        detail.tags = [t.strip() for t in _re.split(r"[,/]", tags) if t.strip()]
        detail.chapters = list(chosen[2])
        for ch in chosen[2]:
            ch.line_switch_url = _line_url(chosen[0])
            ch.line_switch_source = chosen[0]
            ch.line_name = chosen[1]
        detail.source_list = [
            {
                "source_id": from_name,
                "sid": from_name,
                "name": display,
                "detail_url": _line_url(from_name),
                "episodes": [{"title": ch.title, "url": ch.url} for ch in chapters],
            }
            for from_name, display, chapters in lines
        ]
        return detail

    def _fill_meta_from_html(self, source: SourceConfig, detail, url: str) -> None:
        """API 详情补页面元数据：接口只给分集/线路时，从详情页 head 取标题/封面/简介。

        只填空字段（接口有值不覆盖），任何失败静默——元数据缺失不该让详情失败。
        """
        fields = (source.get_detail_config() or {}).get("fields") or {}
        if not fields:
            return
        try:
            abs_url = self._abs_url(source, url)
            html = self._get_detail_html(source, url, abs_url)
            doc = self._parser.parse(html)
        except Exception as exc:  # noqa: BLE001 —— 补全失败保持接口原值
            log.info("[%s] 阶段=详情，页面元数据补全失败：%s", source.source_id, exc)
            return

        def _pick(key: str, absolute: bool = False) -> str:
            spec = fields.get(key)
            if not spec:
                return ""
            try:
                # 只有 URL 类字段（封面）才做 base_url 绝对化：标题/简介按字面取，
                # 否则 Parser 会把纯文本当相对路径拼成 https://站点/书名。
                return str(
                    self._parser.extract_first(
                        doc, spec, source.base_url if absolute else ""
                    ) or ""
                )
            except Exception:  # noqa: BLE001
                return ""

        if not detail.title:
            detail.title = _pick("title")
        if not detail.cover:
            detail.cover = _pick("cover", absolute=True)
        if not detail.summary:
            detail.summary = _pick("summary")
        if not detail.author:
            detail.author = _pick("author")
        if not detail.status:
            detail.status = _pick("status")
        if not detail.tags:
            try:
                detail.tags = list(self._parser.extract(doc, fields.get("tags")) or [])
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _query_param(url: str, key: str) -> str:
        """取 URL query 里某个参数（缺失返回空串）。"""
        if not url or not key:
            return ""
        from urllib.parse import parse_qs, urlsplit

        try:
            return (parse_qs(urlsplit(url).query).get(key) or [""])[0]
        except Exception:  # noqa: BLE001 —— 畸形 URL 当无参数
            return ""

    @staticmethod
    def _line_switch_url(url: str, line_id: str) -> str:
        """把详情 URL 的线路坐标换成 line_id（query 的 source/line/sid）。

        没有线路坐标的 URL 返回空串——此时线路菜单拿不到可切换目标，
        交给播放界面的其它换源路径（source_switch / related_episodes）。
        """
        if not url or not line_id:
            return ""
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        parts = urlsplit(url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        keys = ("source", "line", "sid")
        if not any(k in keys for k, _ in query):
            return ""
        new_query = [(k, line_id if k in keys else v) for k, v in query]
        return urlunsplit(parts._replace(query=urlencode(new_query)))

    # ------------------------------------------------------------------ #
    # API 详情：同作品其它线路（related_episodes）
    # ------------------------------------------------------------------ #
    def _mark_line_menu(self, source, detail: Detail, cfg: dict,
                        url: str, data) -> None:
        """只标记「可换线路」，**不发任何请求**。

        换线路要在其它采集源里按标题检索、再各请求一次详情（实测 ikanpp
        第一集约 3.3s）。这段放在详情主链路里会把打开详情从 1.2s 拖到 4.5s，
        而绝大多数用户根本不换线路。因此这里只置标志位，真正取线路菜单由
        `fetch_line_menu()` 在后台按需触发（播放界面加载完再把线路菜单补上）。
        """
        rel = (cfg.get("chapters") or {}).get("related_episodes") or {}
        if not rel.get("enabled"):
            return
        if self._has_group_menu(source, url):
            # 搜索分组已经给出「其它采集源」线路菜单 → 不需要再拉同作品线路
            return
        if not str(self._jsonpath(data, rel.get("title_path") or "vod_name") or ""):
            return  # 没有标题就无法按标题检索线路，直接放弃
        detail.line_menu_available = True

    def fetch_line_menu(self, source, url: str, title: str = "",
                        current_source: str = "") -> list:
        """取同作品其它线路的分集（供播放界面按需补线路菜单）。

        只在 `detail.line_menu_available` 为真时调用；不改变当前详情、不阻塞
        详情加载。返回的 Chapter 带 line_switch_url / line_name / provider。
        任何异常都返回空列表——线路是锦上添花，不能影响正常播放。
        """
        from urllib.parse import parse_qs, urlsplit

        cfg = source.raw.get("api_endpoints", {}).get("detail", {})
        cfg = cfg if isinstance(cfg, dict) else {}
        rel = {}
        detail_api = (source.raw.get("api_endpoints") or {}).get("detail") or {}
        chapters_api = detail_api.get("chapters") or {}
        rel = chapters_api.get("related_episodes") or {}
        if not rel.get("enabled"):
            return []
        if self._has_group_menu(source, url):
            return []
        if not title:
            title = (parse_qs(urlsplit(url).query).get("title") or [""])[0]
        if not title:
            return []
        if not current_source:
            current_source = (
                parse_qs(urlsplit(url).query).get("source") or [""]
            )[0]
        try:
            return self._line_chapters(source, rel, url, title, current_source)
        except Exception as exc:  # noqa: BLE001 —— 线路失败不影响主流程
            log.info("[%s] 线路菜单获取失败：%s", source.source_id, type(exc).__name__)
            return []

    def _fetch_related_api_episodes(self, source, detail: Detail, cfg: dict,
                                    url: str, data) -> list:
        """旧入口（保留兼容）：把其它线路的分集并入当前集列表。

        详情主链路已改为只标记（见 `_mark_line_menu`），这里仅保留给显式需要
        同步合并的调用方；正常播放界面走 `fetch_line_menu` + 后台任务。
        """
        chapters = list(detail.chapters or [])
        rel = (cfg.get("chapters") or {}).get("related_episodes") or {}
        if not rel.get("enabled") or self._has_group_menu(source, url):
            return chapters
        try:
            return self._merge_related_api_episodes(source, chapters, rel, url, data)
        except Exception as exc:  # noqa: BLE001 —— 多线路失败不影响详情主体
            log.info("[%s] 相关线路获取失败：%s", source.source_id, type(exc).__name__)
            return chapters

    @staticmethod
    def _has_group_menu(source, url: str) -> bool:
        """本作品是否已有搜索分组线路菜单（有则不重复拉同作品其它线路）。"""
        search_cfg = (source.raw.get("api_endpoints") or {}).get("search") or {}
        if not (search_cfg.get("group") or {}).get("enabled"):
            return False
        try:
            from urllib.parse import parse_qs, urlsplit

            from ..search_groups import lookup_group

            title = (parse_qs(urlsplit(url).query).get("title") or [""])[0]
            return len(lookup_group(source.source_id, title, url)) >= 2
        except Exception:  # noqa: BLE001 —— 分组只是优化，查不到就按无分组处理
            return False

    def _merge_related_api_episodes(self, source, chapters: list, rel: dict,
                                    url: str, data) -> list:
        api_url = str(rel.get("url") or "")
        if not api_url:
            return chapters
        cur_source = str(
            self._jsonpath(data, rel.get("source_path") or "source") or ""
        )
        if not cur_source and chapters:
            cur_source = str(getattr(chapters[0], "provider", "") or "")
        # 检索用标题：必须是**当前作品**的标题，不能为空，否则会命中无关作品
        title = str(
            self._jsonpath(data, rel.get("title_path") or "vod_name") or ""
        )
        if not title:
            return chapters

        merged = list(chapters)
        # 当前集标记自身线路来源：换线路后玩家要能看出当前在播哪条线。
        # **就地改 `chapters` 里的对象**（merged 是同一批引用），否则详情缓存
        # 里存下的会是「provider 为空」的版本，换线路回来时当前线路在菜单里消失了。
        for ch in merged:
            if not getattr(ch, "provider", ""):
                ch.provider = cur_source
        merged.extend(
            self._line_chapters(source, rel, url, title, cur_source,
                                existing=merged, data=data)
        )
        return merged

    def _line_chapters(self, source, rel: dict, url: str, title: str,
                       cur_source: str, existing=None, data=None) -> list:
        """取「同作品其它线路」的分集（**只返回其它线路的集**）。

        与当前线路的集完全分离：调用方既可以并入分集列表，也可以只用来构建
        播放界面的线路菜单。`existing` 用于去重（标题+URL），`data` 仅在需要
        回退标题时使用。
        """
        api_url = str(rel.get("url") or "")
        if not api_url or not title:
            return []
        max_lines = max(1, int(rel.get("max_lines") or 2))
        skip = {str(v) for v in (rel.get("skip_sources") or []) if v}
        if cur_source:
            skip.add(cur_source)
        catalog = rel.get("provider_catalog") or {}
        fields_cfg = rel.get("item_fields") or {}
        id_key = fields_cfg.get("play_id") or "vod_id"
        seen = {(ch.title or "", ch.url or "") for ch in (existing or [])}
        out: list = []

        for line_source in self._related_line_sources(rel, skip, max_lines):
            # ★ 关键：不能用当前线路的 id 去请求别的线路。
            #   实测 ikanpp /api/detail：json1080 的 vod_id(31027) + source=jisu
            #   会**静默返回另一部作品**（「窒息的阳光」，单集），且 success=True，
            #   调用方无法从响应判断对错——换过去就是随便一部片子，还带着广告。
            #   必须在该线路自己的搜索接口里按标题查到它自己的 vod_id。
            line_vid = self._resolve_line_vod_id(source, rel, title, line_source)
            if not line_vid:
                log.info("[%s] 相关线路 %s 未检索到该作品，跳过",
                         source.source_id, line_source)
                continue
            query = {"id": line_vid, "source": line_source, "title": title}
            line_data = self._fetch_line_detail(source, api_url, query, rel)
            if not isinstance(line_data, dict):
                continue
            # 二次校验：返回作品必须与当前作品同名（允许「国语版」这类后缀差异），
            # 否则宁可不并这条线，也不能把无关作品混进分集列表。
            got_name = str(self._jsonpath(line_data, rel.get("title_path") or "vod_name") or "")
            if not self._titles_related(title, got_name):
                log.info("[%s] 相关线路 %s 返回《%s》，与《%s》不匹配，跳过",
                         source.source_id, line_source, got_name[:20], title[:20])
                continue
            items = self._jsonpath(line_data, (rel.get("items") or "episodes"))
            if not isinstance(items, list) or not items:
                continue
            line_id = str(self._jsonpath(line_data, id_key) or line_vid)
            line_name = str(catalog.get(line_source) or line_source)
            target_url = self._related_line_url(
                rel, url, line_id, line_source, data if data is not None else {}
            )
            added = 0
            for it in items:
                if not isinstance(it, dict):
                    continue
                c_title = str(it.get(rel.get("title") or "name") or "")
                c_url = str(it.get(rel.get("url_key") or "url") or "")
                if not c_url or (c_title, c_url) in seen:
                    continue
                seen.add((c_title, c_url))
                chapter = Chapter(title=c_title, url=c_url, provider=line_source)
                chapter.line_name = line_name
                chapter.line_switch_url = target_url
                chapter.line_switch_source = line_source
                out.append(chapter)
                added += 1
            if added:
                log.info("[%s] 相关线路 %s 并入 %d 集",
                         source.source_id, line_source, added)
        return out

    # ------------------------------------------------------------------ #
    # 跨线路定位作品：必须用目标线路自己的 id
    # ------------------------------------------------------------------ #
    def _resolve_line_vod_id(self, source, rel: dict, title: str,
                             line_source: str) -> str:
        """在目标线路自己的搜索接口里按标题查该作品的 vod_id。

        结果按 (源, 线路, 标题) 缓存，避免每次打开详情都重新搜索。
        找不到/失败返回 ""（调用方跳过该线路，绝不回退到别的线路 id）。
        """
        cache_key = (getattr(source, "source_id", ""), line_source, title)
        cache = getattr(self, "_line_vod_id_cache", None)
        if cache is None:
            cache = self._line_vod_id_cache = {}
        if cache_key in cache:
            return cache[cache_key]
        vid = ""
        try:
            vid = self._search_line_vod_id(source, title, line_source)
        except Exception as exc:  # noqa: BLE001 —— 单条线路搜索失败只跳过该线路
            log.info("[%s] 线路 %s 搜索失败：%s", source.source_id,
                     line_source, type(exc).__name__)
        cache[cache_key] = vid
        # 缓存有界：只留最近 256 条（详情多开时不无界增长）
        while len(cache) > 256:
            cache.pop(next(iter(cache)))
        return vid

    def _search_line_vod_id(self, source, title: str, line_source: str) -> str:
        """调聚合站搜索接口（只带目标线路一个源），返回匹配作品的 vod_id。"""
        from urllib.parse import urljoin

        search_cfg = (source.raw.get("api_endpoints") or {}).get("search") or {}
        search_url = str(search_cfg.get("url") or "")
        template = search_cfg.get("body") or {}
        if not search_url or not template:
            return ""
        entries = [
            dict(item) for item in (template.get("sources") or [])
            if isinstance(item, dict) and str(item.get("id")) == line_source
        ]
        if not entries:
            return ""
        body = dict(template)
        body["query"] = title
        body["sources"] = entries
        body["page"] = "1"
        charset = (source.transports() or {}).get("charset") or "utf-8"
        text = self._http.post_text(
            urljoin(source.base_url, search_url),
            json_body=body,
            headers=self._endpoint_headers(source, search_cfg),
            timeout=self._timeout(source),
            retries=max(0, self._retries(source) - 1),
            proxy_pool=source.proxy_pool(),
            encoding=charset,
        )
        for event in self._parse_sse_events(text):
            videos = event.get("videos")
            if not isinstance(videos, list):
                continue
            for video in videos:
                if not isinstance(video, dict):
                    continue
                name = str(video.get("vod_name") or "")
                vid = str(video.get("vod_id") or "")
                if not vid or not self._titles_related(title, name):
                    continue
                return vid
        return ""

    @staticmethod
    def _parse_sse_events(text: str) -> list:
        """宽松解析 SSE 文本为事件对象列表。

        ikanpp 的超大 videos 事件会被服务器拆成多物理行：只有首行带 `data:`
        前缀，续行是裸 JSON 片段（可能在多字节字符中间被截断）。因此按**空行**
        切事件，事件内所有行拼接后再解析，而不是逐行解析。
        """
        events = []
        for block in _re.split(r"\n\s*\n", text or ""):
            chunk = []
            for line in block.splitlines():
                if line.startswith("data:"):
                    chunk.append(line[5:].lstrip())
                elif chunk:
                    chunk.append(line)
            payload = "".join(chunk).strip()
            if not payload:
                continue
            try:
                parsed = json.loads(payload)
            except Exception:  # noqa: BLE001 —— 残缺事件直接丢弃
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
        return events

    @staticmethod
    def _titles_related(a: str, b: str) -> bool:
        """标题是否指向同一作品（容忍「国语版/粤语版」等后缀差异）。"""
        from ..search_groups import normalize_title

        na, nb = normalize_title(a), normalize_title(b)
        if not na or not nb:
            return False
        if na == nb:
            return True
        short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
        # 短标题太短时不做包含判断（「我和僵尸」这类会产生大量误配）
        return len(short) >= 4 and short in long


    def _related_line_sources(self, rel: dict, skip: set, max_lines: int) -> list:
        """可选线路标识（按 provider_catalog 或线路候选列表顺序，去掉当前/跳过项）。"""
        candidates = [str(v) for v in (rel.get("line_sources") or []) if v]
        if not candidates:
            candidates = [str(k) for k in (rel.get("provider_catalog") or {})]
        out = []
        for cand in candidates:
            # 先去重/去跳过再计数：跳过项不能占用 max_lines 名额
            if not cand or cand in skip or cand in out:
                continue
            out.append(cand)
            if len(out) >= max_lines:
                break
        return out

    def _fetch_line_detail(self, source, api_url: str, body: dict, rel: dict):
        """按线路请求一次详情接口，返回 data 节点（失败返回 None）。"""
        from urllib.parse import urljoin

        try:
            resp = self._http.post_json(
                urljoin(source.base_url, api_url),
                json_body=body,
                headers=self._endpoint_headers(source, rel),
                timeout=self._timeout(source),
                retries=max(0, int(rel.get("retries") or 0)),
                proxy_pool=source.proxy_pool(),
            )
        except Exception as exc:  # noqa: BLE001 —— 单条线路失败只跳过该线路
            log.info("[%s] 线路详情失败（%s）：%s",
                     source.source_id, body.get("source"), type(exc).__name__)
            return None
        if not isinstance(resp, dict):
            return None
        return resp.get("data") if isinstance(resp.get("data"), dict) else resp

    def _related_line_url(self, rel: dict, url: str, line_id: str,
                          line_source: str, data) -> str:
        """构造线路切换 URL（替换当前详情 URL 的 id/source，保留 title 等参数）。

        直接拼接而不是 urlencode：站点内 URL 的 title 参数要求**原样中文**
        （与 detail_url 模板产出的形态一致），编码后的窗口里服务端匹配逻辑
        与源站模板并不完全等价，改用原样可省掉这一层不确定性。
        """
        from urllib.parse import parse_qs, urlsplit, urlunsplit

        parts = urlsplit(url)
        parsed = parse_qs(parts.query, keep_blank_values=True)
        order = list(parsed)
        values = {k: v[0] for k, v in parsed.items()}
        # 线路 id 写在路径里（/title/{id}），query 里的 id 只是详情接口 body 的
        # 占位来源——保留旧 id 会与路径打架，这里同步成目标线路的 id
        values.pop("id", None)
        path = parts.path
        tpl = rel.get("resolve_url")
        if tpl:
            # 模板给出时按线路标识重建路径（如 /title/{play_id}），未给出的占位
            # 原样保留，由调用方 URL 的 path 兜底
            path = (
                tpl.replace("{play_id}", line_id)
                .replace("{play_source}", line_source)
                .replace("{source}", line_source)
            )
        if "source" in values or tpl:
            if "source" not in order:
                order.append("source")
            values["source"] = line_source
        if not values.get("title"):
            title = str(self._jsonpath(data, rel.get("title_path") or "vod_name") or "")
            if title:
                if "title" not in order:
                    order.append("title")
                values["title"] = title
        query = "&".join(
            f"{k}={values[k]}" for k in order if k != "id" and values.get(k, "") != ""
        )
        return urlunsplit((parts.scheme, parts.netloc, path, query, ""))

    @staticmethod
    def _extract_names(node, spec):
        """列表字段（标签/演员）取值：支持字符串 JSONPath 或 {path, key} 对象列表。

        API 详情通常返回对象数组（如 labels=[{id,labelName}]），纯 JSONPath
        无法投影字段名；配置 ``{"path": "labels", "key": "labelName"}`` 即可
        取出名字列表。字符串 spec 或数据不是列表时返回 []，由调用方回退。
        """
        if not spec:
            return []
        if isinstance(spec, dict):
            items = DetailMixin._jsonpath(node, spec.get("path"))
            key = spec.get("key") or ""
        else:
            items = DetailMixin._jsonpath(node, spec)
            key = ""
        if not isinstance(items, list):
            return []
        names = []
        for it in items:
            if isinstance(it, dict):
                val = it.get(key) if key else next(
                    (it[k] for k in ("name", "title") if it.get(k)), ""
                )
            else:
                val = it
            if val not in (None, ""):
                names.append(str(val))
        return names

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
