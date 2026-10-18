"""内容服务核心：初始化、缓存、HTTP、详情入口。

B4 拆分自 content.py，方法原样迁移，行为不改变。
"""

from __future__ import annotations

from .shared import (
    Chapter, Detail, StreamBudget, _re,
    asdict, json, log, report_bg_check,
    threading, time, utils,
)


class ContentBase:
    def __init__(
        self,
        http: HttpClient,
        parser: Parser,
        checker: StructureChecker,
        decrypter: Optional["Decrypter"] = None,
        health_reporter=None,
        cache=None,
        repository=None,
    ):
        self._http = http
        self._parser = parser
        self._checker = checker
        self._decrypter = decrypter
        self._health_reporter = health_reporter  # 可选：update_health(source_id, state, error)
        # 自动禁用后整源缓存失效：若 health_reporter 是 SourceManager，注册回调，
        # 源被自动禁用时立即清掉该源旧缓存（只清该源前缀键，不碰共享缓存）。
        if health_reporter is not None:
            binder = getattr(health_reporter, "set_cache_invalidator", None)
            if callable(binder):
                try:
                    binder(self.invalidate_source_caches)
                except Exception:  # noqa: BLE001 —— 注册失败不阻塞初始化
                    pass
        # 可选 RedisLikeStore 实例（None=禁用）。键约定：
        #   page:{source_id}:{abs_url}   详情/目录页（永久，shelf 池）
        #   detail:{source_id}:{abs_url} 详情元数据（30 天，shelf 池）
        #   body:{source_id}:{abs_url}   章节正文（7 天，shelf 池）
        #   pages:{source_id}:{abs_url}  漫画页图（7 天，shelf 池）
        #   cover:{source_id}:{abs_url}  封面字节（永久，shelf 池）
        self._cache = cache
        self._repository = repository
        self._ytdlp = None  # 懒加载单例，复用 yt-dlp 子进程
        # 视频详情页 HTML 复用缓存：single_chapter 源（missav/hanime1/18j/18mh-video
        # 等）取流 URL == 详情 URL，fetch_detail 已整页下载，取流时复用免二次下载
        # （慢站反爬延迟下详情+取流各一次，复用省约一半同步等待）。只留最近一部
        # （每次详情加载重新填充，天然不过期）。
        self._video_html_cache: dict = {}
        # 已知失效线路黑名单：source_id -> {episode_key: 判死时刻(monotonic)}。
        # probe 明确失败（403/404/空/超时）的分集线路记入，TTL 内复播/换集
        # 直接跳过（死线路串行轮换是 omofuna 类站取流的最大耗时段）。
        self._dead_episode_lines: dict = {}
        # 详情页 HTML 短 TTL 缓存：重复打开同一本书免二次整页下载（慢站详情慢）。
        # 含目录页（chapters_url 二次抓取也走此缓存，见 _fetch_chapters）。
        # 只缓存最近 _detail_html_max 条，防长期会话无界增长（dict 保持插入序）。
        self._detail_html_cache: dict = {}  # {(source_id, abs_url): (expire_ts, html)}
        self._detail_html_ttl = 300.0  # 5 分钟
        self._detail_html_max = 200  # 多书切换时减少驱逐，详情页/目录页复用
        # 进行中抓取去重表：key -> {"event": Event, "result": ..., "error": ...}。
        # 并发预取同一章节/漫画页时复用同一份在途结果，避免重复网络请求。
        self._inflight_lock = threading.Lock()
        self._inflight: dict = {}
        self._settings = None  # 懒加载：sources_runtime.* 设置（SettingsManager 或 dict）

    def _settings_opt(self, key: str, default):
        """懒读取 sources_runtime.* 设置（SettingsManager 或 dict），缺省返回 default。

        生产接线：app.py 把 SettingsManager 注入 SourceManager(runtime_settings=...)，
        而 Content 持 health_reporter=SourceManager → 经 _runtime 可达同一对象；
        不接线时返回 default（保持旧行为）。每次解析当前 runtime 对象（不缓存），
        以便运行时替换/更新配置后立即生效。
        """
        settings = self._settings
        if settings is None:
            settings = getattr(
                getattr(self, "_health_reporter", None), "_runtime", None
            )
        if isinstance(settings, dict):
            return (settings.get("sources_runtime") or {}).get(key, default)
        try:
            from framework.settings_manager import SettingsManager

            if isinstance(settings, SettingsManager):
                return settings.get("sources_runtime", key, default)
        except Exception:  # noqa: BLE001
            pass
        return default

    def _clean_field(self, value, pairs) -> str:
        """按替换对列表清洗字段值：pattern 以 "re:" 前缀按正则替换。"""
        value = str(value or "").strip()
        for pat, repl in (pairs or []):
            if not pat:
                continue
            if pat.startswith("re:"):
                value = _re.sub(pat[3:], str(repl or ""), value)
            else:
                value = value.replace(pat, str(repl or ""))
        return value.strip()

    def _bg_check(self, source: SourceConfig, abs_url: str) -> None:
        """后台线程执行结构自检，不阻塞抓取（阅读/下载/播放提速）。

        checker.check() 会对同一 URL 额外发一次 GET（最长 timeout×retries），
        同步等待会让每章下载/每页漫画/每次取流慢一倍。自检仅健康监控，
        移后台 daemon 线程（与 discovery 一致）。

        状态映射（与 GUI 诊断一致，避免把活源标死）：
        - 自检通过（True）→ 绿 ok
        - 软失败（False：网络/超时/反爬/未达硬失败阈值）→ 黄 warn，不标 broken
        - 硬失败（StructureChangedError：结构确认变更）→ 红 broken
        """
        # 源配置 diagnostics.selfcheck.strategy="off" → 完全关闭后台自检。
        # 自检会对同一 URL 并发第二 GET（后台线程），fanqie 等已配 off 的源
        # 继续触发会放大反爬尖峰（实测详情慢 ~6s），此处跳过不再发请求。
        if (
            (source.raw.get("diagnostics") or {}).get("selfcheck") or {}
        ).get("strategy") == "off":
            return
        report_bg_check(self._checker, self._health_reporter, source, abs_url)

    # ------------------------------------------------------------------ #
    def _headers(self, source: SourceConfig) -> dict:
        return source.request_headers()

    def _endpoint_headers(self, source: SourceConfig, cfg: dict) -> dict:
        """api_endpoints.<endpoint> 级请求头：源级 headers 基础上做端点覆盖。

        cfg["headers"] 支持：
            "Header": "value"    覆盖/新增该头
            "Header": "" 或 null  移除该头（大小写不敏感）
        用途：B 站 detail 接口带 Referer 会固定 412，但 CDN 取流又必须带
        Referer —— 用端点级覆盖在 detail 上摘掉 Referer，源级保持不动。
        """
        headers = self._headers(source)
        overrides = cfg.get("headers") or {}
        if not overrides:
            return headers
        for k, v in overrides.items():
            if v is None or v == "":
                for hk in list(headers):
                    if hk.lower() == k.lower():
                        headers.pop(hk, None)
            else:
                for hk in list(headers):
                    if hk.lower() == k.lower():
                        headers.pop(hk, None)
                headers[k] = v
        return headers

    def _timeout(self, source: SourceConfig) -> float:
        return float(source.transports().get("timeout") or self._http.defaults.timeout)

    def _retries(self, source: SourceConfig) -> int:
        return int(source.transports().get("retries") or self._http.defaults.retries)

    def _interval_ms(self, source: SourceConfig) -> int:
        return int(source.transports().get("interval_ms") or self._http.defaults.interval_ms)

    @staticmethod
    def _stream_context(context=None, deadline=None):
        if context is not None:
            return context
        return StreamBudget(deadline) if deadline is not None else None

    @staticmethod
    def _check_stream_context(context=None, deadline=None):
        if context is not None:
            context.check()
        elif deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("stream resolution deadline exhausted")

    def _abs_url(self, source: SourceConfig, url: str) -> str:
        return utils.abs_url(source.base_url, url)

    def _detail_cache_key(self, source: SourceConfig, url: str) -> str:
        """Allow a source to invalidate stale detail/TOC HTML after a redesign."""
        absolute = self._abs_url(source, url)
        version = (source.raw.get("detail_cache_version") if source else None) or ""
        if version:
            separator = "&" if "?" in absolute else "?"
            absolute = f"{absolute}{separator}__cache_version={version}"
        return f"detail:{source.source_id}:{absolute}"

    def _page_cache_key(self, source: SourceConfig, abs_url: str) -> str:
        """Build the persistent HTML-page key, including redesign versions."""
        version = (source.raw.get("page_cache_version") if source else None) or ""
        if version:
            separator = "&" if "?" in abs_url else "?"
            abs_url = f"{abs_url}{separator}__cache_version={version}"
        return f"page:{source.source_id}:{abs_url}"

    @staticmethod
    def _repository_cache_parts(key: str, book_key: str | None = None) -> tuple[str, str]:
        parts = key.split(":", 3)
        if len(parts) == 4 and parts[0] == "pages" and parts[1] == "v3":
            url = parts[3]
            if book_key is None:
                return "pages", url
            return url, book_key
        kind, _source_id, url = key.split(":", 2)
        if book_key is None:
            return kind, url
        return url, book_key

    def _cache_get(self, key: str, book_key: str | None = None):
        if self._repository is not None:
            chapter_key, cache_book_key = self._repository_cache_parts(key, book_key)
            value = self._repository.get_content(cache_book_key, chapter_key)
            if value is not None:
                if chapter_key == "pages" and isinstance(value, str):
                    try:
                        return json.loads(value)
                    except json.JSONDecodeError:
                        return None
                return value
        if self._cache is not None:
            return self._cache.get(key)
        return None

    def _cache_set(
        self, key: str, value, ttl=None, book_key: str | None = None
    ) -> None:
        kind, source_id, _url = key.split(":", 2)
        chapter_key, cache_book_key = self._repository_cache_parts(key, book_key)
        if self._repository is not None:
            self._repository.upsert_book(cache_book_key, source_id, cache_book_key, "", {})
            payload = json.dumps(value, ensure_ascii=False) if kind == "pages" else value
            self._repository.put_content(
                cache_book_key,
                chapter_key,
                "application/json" if kind == "pages" else "text/plain",
                payload,
                {},
            )
        if self._cache is not None:
            self._cache.set(key, value, ttl=ttl)

    _DETAIL_TTL = 30 * 86400
    # page: 详情/目录页 HTML（shelf 池）。原永久保留，切为 7 天上限：
    # 站点改版后旧整页 HTML 最长残留一周，超过即回源重抓（明细、漫画目录
    # 页共用 _get_detail_html，改动只落在 page: 单写点，见 _get_detail_html）。
    _PAGE_HTML_TTL = 7 * 86400

    def _cache_get_detail(self, key: str):
        if self._cache is None:
            return None
        raw = self._cache.get(key)
        if raw is None:
            return None
        if isinstance(raw, Detail):
            return raw
        try:
            data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            if not isinstance(data, dict):
                return None
            chapters = [
                chapter if isinstance(chapter, Chapter) else Chapter(**chapter)
                for chapter in data.pop("chapters", [])
            ]
            data["chapters"] = chapters
            return Detail(**data)
        except Exception:  # noqa: BLE001
            return None

    def _cache_set_detail(self, key: str, detail: Detail) -> None:
        if self._cache is None:
            return
        if any(self._looks_like_direct_media(ch.url) for ch in (detail.chapters or [])):
            return
        try:
            self._cache.set(
                key,
                json.dumps(asdict(detail), ensure_ascii=False),
                ttl=self._DETAIL_TTL,
            )
        except Exception:  # noqa: BLE001
            log.debug("[cache] 详情缓存写入失败", exc_info=True)

    @staticmethod
    def _looks_like_direct_media(url: str) -> bool:
        """URL 是否已是可直接播放的媒体文件/流（m3u8/mpd/mp4/flv/webm 等）。

        用在 fetch_video_episode 的直链 passthrough：这类地址交给 HTML 解析
        或 episode API 都没有意义（fetch 会拿到二进制/list 文本而非播放页）。
        """
        if not url:
            return False
        from urllib.parse import urlsplit as _urlsplit

        path = _urlsplit(url).path.lower()
        return path.endswith(
            (".m3u8", ".mpd", ".mp4", ".m4v", ".flv", ".webm", ".mkv", ".mov", ".ts", ".m4a")
        )

    def _get(self, source: SourceConfig, url: str, http=None, deadline=None, context=None) -> str:
        """抓取页面 HTML。http 可传独立 HttpClient（并行翻页时避免共享
        self._http 的 requests.Session 跨线程竞态）；缺省用共享实例。"""
        context = self._stream_context(context, deadline)
        self._check_stream_context(context, deadline)
        client = http or self._http
        abs_url = self._abs_url(source, url)
        timeout = self._timeout(source)
        retries = self._retries(source)
        interval_ms = self._interval_ms(source)
        if context is not None:
            remaining = context.remaining()
            if remaining <= 0:
                context.check()
            timeout = min(timeout, remaining)
            retries = 0
            interval_ms = 0
        elif deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("stream resolution deadline exhausted")
            timeout = min(timeout, remaining)
            retries = 0
            interval_ms = 0
        value = client.get_text(
            abs_url,
            headers=self._headers(source),
            timeout=timeout,
            retries=retries,
            interval_ms=interval_ms,
            encoding=source.transports().get("charset"),
            proxy_pool=source.proxy_pool(),
            direct=bool(source.transports().get("direct")),
        )
        self._check_stream_context(context, deadline)
        return value

    def _get_detail_html(self, source: SourceConfig, url: str, abs_url: str, deadline=None, context=None) -> str:
        """详情页 HTML：短 TTL 缓存命中直接返回，否则抓取并缓存。

        重复打开同一本书（详情抽屉多次/刷新）免二次整页下载，慢站详情慢时
        省约整页等待。缓存键 (source_id, abs_url)，TTL 5 分钟，超上限丢最旧。
        """
        key = (source.source_id, abs_url)
        hit = self._detail_html_cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        # Redis 二级：详情/目录页永久（重启后/预加载后免抓）
        redis = self._cache
        redis_key = self._page_cache_key(source, abs_url)
        if redis is not None:
            cached = redis.get(redis_key)
            if cached is not None:
                self._detail_html_cache[key] = (time.time() + self._detail_html_ttl, cached)
                return cached
        detail_cfg = source.get_detail_config()
        render_mode = detail_cfg.get("render")
        if render_mode == "playwright_fallback":
            render_cfg = detail_cfg.get("render_config") or {}
            try:
                html = self._get(source, url, deadline=deadline, context=context)
                probe = render_cfg.get("http_probe")
                if probe and not self._parser.extract(self._parser.parse(html), {"css": probe}):
                    raise ContentMissingError("HTTP 详情页未命中结构", source_id=source.source_id)
            except Exception:
                html = self._fetch_rendered_html(source, url, render_cfg)
        elif render_mode == "playwright":
            html = self._fetch_rendered_html(
                source, url, detail_cfg.get("render_config") or {}
            )
        else:
            html = self._get(source, url, deadline=deadline, context=context)
        self._detail_html_cache[key] = (time.time() + self._detail_html_ttl, html)
        if len(self._detail_html_cache) > self._detail_html_max:
            # dict 保持插入序：弹出最先插入的一条（近似 LRU）
            self._detail_html_cache.pop(next(iter(self._detail_html_cache)))
        if redis is not None:
            redis.set(redis_key, html, ttl=self._PAGE_HTML_TTL)
        return html

    def _content_block(self, source: SourceConfig) -> dict:
        """endpoints.content 配置块（可能缺失 → {}）。"""
        return source.raw.get("endpoints", {}).get("content") or {}

    def fetch_cover(self, source: SourceConfig, url: str) -> str:
        """按需抓详情页封面（cover_backfill 源：列表页纯文本无封面，详情页补回）。

        轻量：只取 endpoints.detail.fields.cover 选择器，不抓章节列表/正文。
        复用 _detail_html_cache，重复打开/回填同一本书不重复下载。
        异常/解析为空 → 返回 ""（失败静默，调用方跳过该条、不影响其它条目）。
        """
        try:
            abs_url = self._abs_url(source, url)
            html = self._get_detail_html(source, url, abs_url)
            doc = self._parser.parse(html)
            detail_cfg = source.get_detail_config()
            cover_sel = (detail_cfg.get("fields") or {}).get("cover")
            if not cover_sel:
                return ""
            # 与 fetch_detail 同链路：选择器命中但 src 为空/占位（如番茄详情页
            # <img class="book-cover-img"> 的 src 被 SSR 模板清空，真实封面在
            # window.__INITIAL_STATE__ 的 "image":[...] JS0N）→ 走 regex/state 兜底，
            # 否则书架补封面会写回 base_url 垃圾值（表现为「收藏没封面」）。
            cover = self._parser.extract_first(doc, cover_sel, source.base_url)
            return self._extract_cover_fallback(source, html, cover_sel, cover)
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------ #
    def refresh_detail(self, source: SourceConfig, url: str, current_chapter_url: str = "") -> Detail:
        """强制刷新详情和章节正文缓存，保留缓存优先的普通打开路径。"""
        self.invalidate_video_stream_cache(source)
        abs_url = self._abs_url(source, url)
        detail_key = self._detail_cache_key(source, abs_url)
        def _delete(key: str) -> None:
            if self._cache is None:
                return
            deleter = getattr(self._cache, "delete", None)
            if deleter is not None:
                deleter(key)
            else:
                getattr(self._cache, "values", {}).pop(key, None)
                getattr(self._cache, "ttls", {}).pop(key, None)

        if self._cache is not None:
            _delete(detail_key)
            _delete(self._page_cache_key(source, abs_url))
            # Remove the unversioned key left by older source definitions too.
            _delete(f"page:{source.source_id}:{abs_url}")
            if current_chapter_url:
                chapter_key = (
                    self._chapter_cache_key(source, current_chapter_url)
                    if hasattr(self, "_chapter_cache_key")
                    else f"body:{source.source_id}:{self._abs_url(source, current_chapter_url)}"
                )
                _delete(chapter_key)
        getattr(self, "_detail_html_cache", {}).pop((source.source_id, abs_url), None)
        getattr(self, "_video_html_cache", {}).pop((source.source_id, abs_url), None)
        detail = self.fetch_detail(source, url)
        latest = set()
        for chapter in detail.chapters or []:
            chapter_url = chapter.get("url", "") if isinstance(chapter, dict) else getattr(chapter, "url", "")
            if chapter_url:
                latest.add(self._abs_url(source, chapter_url))
        current_abs = self._abs_url(source, current_chapter_url) if current_chapter_url else ""
        keep = sorted(latest - ({current_abs} if current_abs else set()))
        if self._repository is not None:
            book_keys = {
                url,
                abs_url,
                f"{source.source_id}:{url}",
                f"{source.source_id}:{abs_url}",
            }
            for book_key in book_keys:
                self._repository.evict_book_content(book_key, keep)
        return detail

    def fetch_detail(self, source: SourceConfig, url: str, deadline=None, context=None) -> Detail:
        """抓取详情页：元数据 + 章节列表。

        优先 api_endpoints.detail（JSON API，可选 sign 签名）；
        否则走 endpoints.detail HTML 解析。
        """
        context = self._stream_context(context, deadline)
        self._check_stream_context(context, deadline)
        cache_key = self._detail_cache_key(source, url)
        cached = self._cache_get_detail(cache_key)
        if cached is not None and not any(
            self._looks_like_direct_media(ch.url) for ch in (cached.chapters or [])
        ):
            return cached

        api = source.raw.get("api_endpoints") or {}
        detail_api = api.get("detail") or {}
        if detail_api:
            if detail_api.get("engine") == "ytdlp":
                detail = self._fetch_detail_ytdlp(source, url, detail_api, context=context)
            else:
                detail = self._fetch_detail_api(source, url, detail_api, context=context)
            self._check_stream_context(context, deadline)
            self._cache_set_detail(cache_key, detail)
            return detail

        # 详情 URL 规范化：endpoints.detail.url_suffix 配置为 URL 尾缀补全
        # （如 MacCMS 变体列表 href 不带 .html 但详情页必须 .html，否则返回
        # 反爬落地页）。仅路径末尾补，保留 query；URL 已带后缀则跳过。
        detail_cfg0 = source.get_detail_config()
        suffix = (detail_cfg0 or {}).get("url_suffix") or ""
        if suffix and url:
            path, _, query = url.partition("?")
            if not path.endswith(suffix):
                url = path + suffix + ("?" + query if query else "")

        abs_url = self._abs_url(source, url)
        cache_key = self._detail_cache_key(source, abs_url)
        cached = self._cache_get_detail(cache_key)
        if cached is not None:
            return cached
        self._bg_check(source, abs_url)
        html = self._get_detail_html(
            source, url, abs_url, deadline=deadline, context=context
        )
        if source.content_type == "video":
            # 缓存视频详情页 HTML：single_chapter 源取流 URL == 详情 URL，
            # fetch_video_episode 复用，避免二次整页下载（慢站省约一半等待）。
            self._video_html_cache = {
                (source.source_id, abs_url): html
            }
        doc = self._parser.parse(html)

        detail_cfg = source.get_detail_config()
        fields = detail_cfg.get("fields") or {}
        title = self._parser.extract_first(doc, fields.get("title"))
        # 标题清理：fields.title_clean 为 [["在线观看", ""], ...] 替换对列表
        for _pat, _repl in (fields.get("title_clean") or []):
            if _pat:
                title = title.replace(_pat, _repl)
        # 书名修正：若 title 像是站点名（短/非书名），尝试 og:novel:book_name
        book_fields = fields.get("book_name")
        if book_fields:
            book = self._parser.extract_first(doc, book_fields)
            if book:
                title = book
        cover_cfg = fields.get("cover")
        cover = self._parser.extract_first(doc, cover_cfg, source.base_url)
        # 封面兜底：选择器封面为空/命中共享占位时，按 fields.cover 的 regex / state
        # 配置从 JSON-LD / window.__INITIAL_STATE__ 取真实封面（番茄等 SSR 占位图源）。
        cover = self._extract_cover_fallback(source, html, cover_cfg, cover)
        detail = Detail(
            source_id=source.source_id,
            content_type=source.content_type,
            url=abs_url,
            title=title,
            author=self._parser.extract_first(doc, fields.get("author")),
            cover=cover,
            status=self._parser.extract_first(doc, fields.get("status")),
            summary=self._parser.extract_first(doc, fields.get("summary")),
            actor=self._parser.extract_first(doc, fields.get("actor")),
        )
        # 标题/作者文本解密（番茄等：列表/详情书名作者被 PUA 字体混淆，需 translit
        # 还原；解密配置与 discovery 列表同源：decryption.targets.title/author）。
        # 未配置解密策略的源 Decrypter 原样返回，零影响。
        if self._decrypter is not None:
            from urllib.parse import urljoin
            from framework.fanqie_font import extract_font_urls

            font_urls = [
                urljoin(abs_url, font_url) for font_url in extract_font_urls(html)
            ]
            if detail.title:
                detail.title = self._decrypter.decrypt(
                    source, detail.title, target="title", html=html,
                    font_urls=font_urls,
                )
            if detail.author:
                detail.author = self._decrypter.decrypt(
                    source, detail.author, target="author", html=html,
                    font_urls=font_urls,
                )
            # 简介繁简转换（繁体源，如 CZBooks：detail 简介也需随正文转简体）
            if detail.summary:
                detail.summary = self._decrypter.decrypt(
                    source, detail.summary, target="summary", html=html,
                    font_urls=font_urls,
                )
        # 标签（可空）
        tags = self._parser.extract(doc, fields.get("tags"))
        detail.tags = tags
        # 图文集（视频页截图序列等）：detail.fields.gallery 多值选择器 → 绝对 URL
        detail.gallery = self._parser.extract(
            doc, fields.get("gallery"), source.base_url
        )
        # 字段清洗：fields.clean.{field} 为 [pattern, repl] 替换对列表；
        # pattern 以 "re:" 开头按正则替换（如 summary 去"最新章节推荐地址"尾巴）
        for _key, _pairs in (fields.get("clean") or {}).items():
            _val = getattr(detail, _key, None)
            if _key == "tags":
                detail.tags = [
                    self._clean_field(t, _pairs) for t in detail.tags
                ]
            elif _val is not None:
                setattr(detail, _key, self._clean_field(_val, _pairs))

        # 站内相关推荐（detail.related 块，如 hanime1 详情页 #related-tabcontent
        # 的相关影片卡片）：优先直接用详情页内嵌的相关内容，替代同源关键词搜索。
        # 无 related 配置/选择器未命中/任何异常 → 保持空列表（UI 回退关键词搜索）。
        related_cfg = detail_cfg.get("related") or {}
        if related_cfg:
            related: List[dict] = []
            try:
                rel_root = related_cfg.get("root_selector")
                rel_fields = related_cfg.get("fields") or {}
                rel_max = int(related_cfg.get("max") or 8)
                if rel_root and rel_fields:
                    items = self._parser.parse_items(
                        doc, rel_root, rel_fields, source.base_url
                    )
                    own = (detail.url or "").rstrip("/").lower()
                    for it in items:
                        r_url = (it.get("url") or "").strip()
                        if not r_url:
                            continue  # 无 URL 的卡片不可点，跳过
                        if r_url.rstrip("/").lower() == own:
                            continue  # 剔除自身
                        related.append({
                            "title": it.get("title") or "",
                            "url": r_url,
                            "cover": it.get("cover") or "",
                        })
                        if len(related) >= rel_max:
                            break
            except Exception:  # noqa: BLE001
                related = []  # 相关推荐失败不影响主流程
            detail.related = related

        # 章节列表（按类型取 content 配置，传书名用于标题清理；
        # html 供目录页 id 从详情页 HTML 提取，如 dm5 COMIC_MID）
        detail.chapters = self._fetch_chapters(
            source, doc, book_title=detail.title, detail_url=url, html=html
        )
        # 视频系列聚合：内容配置 content.episode.series.enabled 且标题含系列标记
        # （如「第N話」）→ 搜同系列基底，把其他分部并入分集列表（hanime1 类站
        # 每个分部是独立视频，站内无分集列表，靠标题系列名聚合）。
        if source.content_type == "video":
            detail.chapters = self._maybe_expand_video_series(
                source, detail, detail.chapters
            )

        # 播放源列表（换源站）：解析 source_switch 配置的可用源
        detail.source_list = self._parse_source_list(source, html, context=context)

        self._cache_set_detail(cache_key, detail)
        return detail
