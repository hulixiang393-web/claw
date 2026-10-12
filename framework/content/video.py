"""视频：取流、换源、播放直链、yt-dlp。

B4 拆分自 content.py，方法原样迁移，行为不改变。
"""

from __future__ import annotations

from .shared import (
    Chapter, ContentMissingError, StreamCancelledError, _AD_IMAGE_RE,
    _re, inspect, json, log,
    time, urljoin, urlsplit,
)


class VideoMixin:
    def fetch_video_streams(self, source: SourceConfig, episode_url: str, quality: str = "",
                            merged: bool = False, deadline=None, context=None,
                            source_lock=False) -> tuple:
        from framework.stream_resolver import StreamResolver, stream_resolver_defaults

        resolver = getattr(self, "_stream_resolver", None)
        if resolver is None:
            # D4：TTL/签名名单默认值从 settings（sources_runtime.*）读取，
            # 缺失/非法/读取失败回退内置硬编码默认（安全默认，不阻断取流）。
            try:
                _settings = getattr(
                    getattr(self, "_health_reporter", None), "_runtime", None
                )
                options = stream_resolver_defaults(_settings)
            except Exception:  # noqa: BLE001
                options = {}
            resolver = self._stream_resolver = StreamResolver(self, **options)
        return resolver.resolve(
                source, episode_url, quality, merged, deadline=deadline, context=context,
                source_lock=source_lock

        )

    def invalidate_video_stream_cache(
        self, source: SourceConfig, episode_url: str | None = None,
        quality: str | None = None, merged: bool | None = None,
    ) -> None:
        resolver = getattr(self, "_stream_resolver", None)
        if resolver is not None:
            resolver.invalidate(source, episode_url, quality, merged)
        source_id = getattr(source, "source_id", "")
        if episode_url is None:
            self._video_html_cache = {
                key: value for key, value in self._video_html_cache.items()
                if key[0] != source_id
            }
            self._detail_html_cache = {
                key: value for key, value in self._detail_html_cache.items()
                if key[0] != source_id
            }
        else:
            abs_url = self._abs_url(source, episode_url)
            self._video_html_cache.pop((source_id, abs_url), None)
            self._detail_html_cache.pop((source_id, abs_url), None)
        cache = self._cache
        if cache is None:
            return
        delete = getattr(cache, "delete", None)
        if not callable(delete):
            return
        keys = []
        if episode_url is None:
            scan = getattr(cache, "scan", None)
            if callable(scan):
                keys.extend(scan(f"page:{source_id}:*"))
        else:
            keys.append(f"page:{source_id}:{self._abs_url(source, episode_url)}")
        for key in keys:
            delete(key)

    def invalidate_source_caches(self, source_id: str) -> None:
        """整源缓存失效（源被自动禁用时由 SourceManager 回调调用）。

        只清该源前缀的业务键（detail/body/pages/page/cover/search），不动
        共享/无前缀键（避免误删其它源或全局缓存），模式同
        invalidate_video_stream_cache。访问量小的源通常无盘缓存，扫描即空。
        """
        resolver = getattr(self, "_stream_resolver", None)
        if resolver is not None:
            try:
                from types import SimpleNamespace

                resolver.invalidate(SimpleNamespace(source_id=source_id))
            except Exception:  # noqa: BLE001
                pass
        # 内存缓存：同源条目整清（video 详情 HTML + 详情/目录页 HTML）
        self._video_html_cache = {
            key: value for key, value in self._video_html_cache.items()
            if key[0] != source_id
        }
        self._detail_html_cache = {
            key: value for key, value in self._detail_html_cache.items()
            if key[0] != source_id
        }
        cache = self._cache
        if cache is None:
            return
        delete = getattr(cache, "delete", None)
        if not callable(delete):
            return
        keys: list = []
        scan = getattr(cache, "scan", None)
        if callable(scan):
            # 注意 pages:v3: 前缀与 pages: 前缀不重叠，需分别扫描
            for prefix in (
                f"detail:{source_id}:",
                f"body:{source_id}:",
                f"pages:{source_id}:",
                f"pages:v3:{source_id}:",
                f"page:{source_id}:",
                f"cover:{source_id}:",
                f"search:{source_id}:",
            ):
                keys.extend(scan(f"{prefix}*"))
        for key in keys:
            try:
                delete(key)
            except Exception:  # noqa: BLE001 —— 单键删除失败不中断
                continue

    def resolve_video_stream_candidates(self, source: SourceConfig, episode_url: str,
                                         quality: str = "", merged: bool = False,
                                         deadline=None, context=None,
                                         source_lock=False) -> list:

        context = self._stream_context(context, deadline)
        candidate = {
            "resolver": lambda context=context: self._fetch_video_streams_legacy(
                source, episode_url, quality, merged, deadline=deadline, context=context,
                source_lock=source_lock,
            ),
        }
        if source.source_id == "agedm":
            block = self._content_block(source).get("episode") or {}
            if block.get("stream_budget") == "pipeline":
                candidate["budget"] = "pipeline"
        return [candidate]

    def _fetch_video_streams_legacy(self, source: SourceConfig, episode_url: str, quality: str = "",
                                     merged: bool = False, deadline=None, context=None,
                                     source_lock=False) -> tuple:

        context = self._stream_context(context, deadline)
        self._check_stream_context(context, deadline)
        """视频：抓取单集 dash 音视频双流（播放/下载用）。

        返回 (video_url, audio_url)；非 dash/无音频时 audio_url 为 ""。
        quality: 画质名（"best"/"1080p"/...），空=源配置默认。经
        api_endpoints.episode 的 quality 映射为请求参数（如 B 站 qn）。
        merged: True 时 yt-dlp 引擎强制合并单流（含音视频的 `best`）——
        VLC 内嵌播放器喂双流 input-slave 对 DASH/fMP4 支持不可靠（黑屏），
        播放端应传 merged=True（单流稳定出画面+声音）；下载端留 False 走
        高清双流再 ffmpeg 合并。
        播放器（VLC）播放 dash 需要同时喂视频轨+音频轨（B 站音视频分离）。

        返回前经 adblock 广告过滤：URL 命中广告特征 → 置空（下载/播放均跳过）。
        """
        from framework.adblock import adblock_for

        ad = adblock_for(source)
        api = source.raw.get("api_endpoints") or {}
        episode_api = api.get("episode") or {}
        video, audio = "", ""
        if episode_api:
            # yt-dlp 引擎（YouTube 高清双流，需签名）
            if episode_api.get("engine") == "ytdlp":
                video, audio = self._fetch_streams_ytdlp(
                    source, episode_url, episode_api, quality, merged=merged,
                    deadline=deadline, context=context
                )
            else:
                streams = self._fetch_episode_api(
                    source, episode_url, episode_api, want_streams=True, quality=quality,
                    deadline=deadline, context=context,
                )
                if isinstance(streams, dict):
                    video, audio = streams.get("video", ""), streams.get("audio", "")
                elif streams:
                    video, audio = streams, ""
        else:
            # HTML 兜底：单 URL
            video, audio = self.fetch_video_episode(
                source, episode_url, deadline=deadline, context=context,
                source_lock=source_lock
            ), ""
        # 广告过滤（URL 命中 → 置空）
        if ad.enabled:
            if video and ad.is_ad_url(video):
                video = ""
            if audio and ad.is_ad_url(audio):
                audio = ""
        return video, audio

    def fetch_video_episode(self, source: SourceConfig, episode_url: str, deadline=None,
                            context=None, source_lock=False) -> str:
        """视频：抓取单集播放地址（解密后返回真实地址）。

        优先 api_endpoints.episode（JSON API，可选 sign 签名）；
        否则 endpoints.content.episode HTML 解析（play_url.selector）。
        换源站（content.episode.source_switch）：默认线路取流失败
        （空 / ContentMissingError / 网络异常 / 播放 URL 403/404/超时）时
        自动尝试其他线路，全部失败才抛错。
        """
        context = self._stream_context(context, deadline)
        self._check_stream_context(context, deadline)
        # 直链媒体 passthrough：分集 URL 已是可直接播放的媒体文件
        # （m3u8/mpd/mp4/flv/webm 等，如 ikanpp 详情 chapters url_template
        # 直接给出 CDN m3u8）。此时无需再走 episode API / HTML 播放页解析，
        # 且此类 URL 被当前置 HTML 解析会误抓二进制/列表文本。仅无
        # api_endpoints.episode 的源生效（episode API 源的分集 URL 是内容页）。
        api = source.raw.get("api_endpoints") or {}
        if not api.get("episode") and self._looks_like_direct_media(episode_url):
            return episode_url
        # JSON API 播放地址（api_endpoints.episode）
        episode_api = api.get("episode") or {}
        if episode_api:
            # yt-dlp 引擎：单流播放地址（合并单流，含音视频，VLC 可直接播）
            if episode_api.get("engine") == "ytdlp":
                v, _ = self._fetch_streams_ytdlp(
                    source, episode_url, episode_api, merged=True,
                    deadline=deadline, context=context
                )
                return v
            play = self._fetch_episode_api(
                source, episode_url, episode_api, deadline=deadline, context=context
            )
            if play:
                return play
            if episode_api.get("url"):
                # API 端点返回空（如 avgood 搜索链接大批过期 → {"zt":"视频不存在"}）
                err_url = episode_api["url"].replace(
                    "{id}", episode_url.split("/")[-1].rsplit(".", 1)[0]
                )
                raise ContentMissingError(
                    f"视频播放地址已失效（API 返回空），{err_url}",
                    source_id=source.source_id,
                )

        # 换源站：默认线路失败 → 自动轮换其他线路
        switch_cfg = self._get_source_switch_cfg(source)
        if switch_cfg and switch_cfg.get("auto_switch", True) and not source_lock:
            return self._fetch_play_url_auto_switch(
                source, episode_url, deadline=deadline, context=context
            )

        return self._call_with_context(
            self._fetch_play_url_once, (source, episode_url), context, deadline
        )

    def _fetch_play_url_once(
        self, source: SourceConfig, episode_url: str, deadline=None, context=None
    ) -> str:
        """按单条分集 URL 提取播放地址（fetch_video_episode 的 HTML 解析部分）。

        独立成方法供线路自动轮换按 URL 复用：每条线路各抓一次播放页再提取。
        """
        context = self._stream_context(context, deadline)
        self._check_stream_context(context, deadline)
        abs_ep = self._abs_url(source, episode_url)
        self._bg_check(source, abs_ep)
        self._check_stream_context(context, deadline)

        content_cfg = self._content_block(source)
        block = content_cfg.get("episode") or {}
        play_cfg = block.get("play_url") or {}
        switch_cfg = block.get("source_switch") or {}

        # Playwright 渲染播放页 → iframe 解析站 → video src（agedm 类嵌套 iframe 站）。
        # 渲染分支不消费播放页 HTML：URL 交给 Playwright 即可，跳过此前的纯 HTTP
        # 抓取（同 URL 会抓两次，实测 dm84 每次多耗 1.6-1.9s）。
        if play_cfg.get("render") == "playwright":
            rc = block.get("render_config") or {}
            from framework.playwright_helper import fetch_rendered_video_sync

            timeout_ms = int(rc.get("timeout_ms") or 45000)
            extra_delay_ms = int(rc.get("extra_delay_ms", 8000))
            render_url = abs_ep
            iframe_selector = play_cfg.get("iframe_selector")
            if iframe_selector:
                html = self._get(source, episode_url, deadline=deadline, context=context)
                doc = self._parser.parse(html)
                iframe_url = self._parser.extract_first(doc, iframe_selector, abs_ep)
                if not iframe_url or not iframe_url.startswith(("https://", "http://")):
                    raise ContentMissingError("未获取到播放 iframe", source_id=source.source_id)
                render_url = iframe_url
            recovery_options = {}
            if source.source_id == "agedm" and rc.get("recover_navigation_timeout") is True:
                recovery_options["recover_navigation_timeout"] = True
            vurl = fetch_rendered_video_sync(
                render_url,
                **recovery_options,
                wait_until=rc.get("wait_until") or "networkidle",
                timeout_ms=timeout_ms,
                extra_delay_ms=extra_delay_ms,
                proxy=source.transports().get("proxy"),
                slow_fallback_rounds=int(rc.get("slow_fallback_rounds", 3)),
                context=context,
            )
            self._check_stream_context(context, deadline)
            if not vurl:
                raise ContentMissingError(
                    f"渲染未获取到视频源（{episode_url}）", source_id=source.source_id
                )
            return vurl

        # single_chapter 视频源：取流 URL == 详情 URL，复用 fetch_detail 已取回的
        # HTML，免对慢站（如 missav 反爬延迟）二次整页下载（详情+取流省约一半等待）。
        # 非单章源（episode URL 与详情不同）key 不命中 → 正常请求。
        html = self._video_html_cache.get((source.source_id, abs_ep))
        if html is None:
            html = self._get(source, episode_url, deadline=deadline, context=context)
        doc = self._parser.parse(html)

        # 链式 HTTP 提取（play_url.http_chain）：多跳播放地址链，纯 HTTP 秒级完成、
        # 不依赖 Playwright。每步从前一步响应（或详情页）提取 URL/数据，逐步收敛
        # 到最终视频地址。适用 iframe→二级播放器→解密 类站（如 5238 的
        # player5238G.php → get3G.php → Caesar 解码 → m3u8）。
        #
        # 配置结构（play_url.http_chain，数组，按序执行）：
        #   [ {"regex": "<从当前文本提取的正则>", "name": "<结果名，供后续步引用>",
        #      "referer": "episode|detail|<上一步的name>", "fetch": true|false,
        #      "force_mp4_0": true, "decode": "caesar_unquote"} ]
        #   - 第 1 步默认从详情页 HTML 提取（可用 html 源："detail" 同上）
        #   - referer: 请求该步 URL 时用的 Referer（防防盗链）。缺省用源 Referer
        #   - fetch=true 或出现 referer 时：把提取到的串当 URL 请求其页面
        #   - force_mp4_0: URL 里 mp4= 值强制为 0（取 HLS 变体）
        #   - decode: caesar_unquote = Caesar 移位(自动测3~5)+URL 解码
        #   - 最后一步提取出的串即播放地址（http 开头）
        chain_cfg = play_cfg.get("http_chain")
        if chain_cfg:
            return self._fetch_http_chain(
                source, episode_url, html, chain_cfg,
                deadline=deadline, context=context
            )

        # 换源站：从 player_aaaa JS 配置提取真实播放地址（ps=0 直接用 / ps=1 走 parse 转码）。
        # 播放页无 player_aaaa（MacCMS 变体：var now 直链等）→ 回退下方 play_url.regex 规则
        if switch_cfg:
            try:
                return self._fetch_play_url_from_player(
                    html, switch_cfg, episode_url, source=source
                )
            except ContentMissingError:
                pass  # player_aaaa 缺失 → 走 play_url.regex

        generic_candidates = self._extract_play_url_candidates(
            html, source, switch_cfg, play_cfg, abs_ep, episode_url
        )
        if generic_candidates:
            return generic_candidates[0]

        # 正则提取（JS 里的转义 URL，如 _detail_.url m3u8）
        play_regex = play_cfg.get("regex")
        if play_regex:
            m = _re.search(play_regex, html)
            if m:
                play = m.group(1) if m.groups() else m.group(0)
                # unescape JS 转义（\/ → /）
                play = play.replace("\\/", "/")
                # 相对地址绝对化（如 18mh /media/m3u8?url=... 是相对路径，
                # 不补全 VLC/下载器拿不到完整 URL，表现为"无法爬取"）
                if play and not play.startswith(("http://", "https://", "//", "data:", "javascript:")):
                    play = urljoin(abs_ep, play)

                # 通用后缀：正则只提取到 CDN base 时补全（如 missav → /playlist.m3u8）
                suffix = play_cfg.get("suffix", "")
                if suffix:
                    play = play.rstrip("/") + "/" + suffix.lstrip("/")
                return play
            raise ContentMissingError(
                f"未匹配到播放地址（{episode_url}）", source_id=source.source_id
            )

        play_sel = play_cfg.get("selector")
        if not play_sel:
            raise ContentMissingError(
                "源未配置播放地址规则（content.episode.play_url）",
                source_id=source.source_id,
            )
        play = self._parser.extract_first(doc, play_sel, source.base_url)
        if not play:
            raise ContentMissingError(
                f"未解析到播放地址（{episode_url}）", source_id=source.source_id
            )
        if play.startswith("//"):
            play = "https:" + play
        elif not play.startswith(("http://", "https://")):
            play = urljoin(abs_ep, play)
        # 解密（如 wbi 签名、加密地址）
        if self._decrypter is not None:
            return self._decrypter.decrypt(source, play, "video_url")
        return play

    # ------------------------------------------------------------------ #
    def _fetch_http_chain(
        self, source: SourceConfig, episode_url: str,
        detail_html: str, chain: list, deadline=None, context=None
    ) -> str:
        """链式 HTTP 取流：多跳播放地址链，逐步收敛到视频地址。

        每步从前一步响应文本（第 1 步从详情页 HTML）按 regex 提取，得到的串
        作为下一步的 URL 继续请求，最终一步提取出 http 开头的播放地址。
        referer 用于防盗链（"episode"/"detail" = 详情页 URL；上一步 name）。
        返回最终播放 URL；失败抛 ContentMissingError。
        """
        context = self._stream_context(context, deadline)
        self._check_stream_context(context, deadline)
        cur_text = detail_html  # 当前文本（初始为详情页 HTML）

        results: dict = {"detail": detail_html}
        cur_url = self._abs_url(source, episode_url)
        step_urls: dict = {"episode": cur_url, "detail": cur_url}

        for i, step in enumerate(chain):
            self._check_stream_context(context, deadline)
            packer = step.get("packer")
            # url 模板：按 {前步name} 引用此前提取值、{t}=秒级时间戳 构造请求 URL
            url_tpl = step.get("url") or ""
            regex = step.get("regex") or ""
            if url_tpl and not regex:
                val = url_tpl
                for _k, _v in results.items():
                    val = val.replace("{" + _k + "}", _v)
                if "{t}" in val:
                    import time as _time
                    val = val.replace("{t}", str(int(_time.time())))
            elif regex:
                m = _re.search(regex, cur_text, _re.IGNORECASE | _re.DOTALL)
                if not m:
                    raise ContentMissingError(
                        f"http_chain 第{i}步未匹配（{regex[:40]}）", source_id=source.source_id
                    )
                val = m.group(1) if m.groups() else m.group(0)
                val = val.replace("\\/", "/")
            else:
                raise ContentMissingError(
                    f"http_chain 第{i}步缺 regex/url", source_id=source.source_id
                )

            # 解码（Caesar 移位 + URL 解码）
            decode = step.get("decode")
            if decode == "caesar_unquote":
                import urllib.parse as _up

                # 站点对 get3G 响应做 Caesar 移位时 shift 会随机轮换（实测
                # 5238/520cc 的 ccsJsCmds 在 shift=1/2/3/4 间随机返回），故遍历
                # 1~8 自动探测：哪档位移解码后含视频标记即采用。
                # 先做 JS 字符串 unescape（\' → '，站点在值内用转义单引号防
                # 单引号闭合，如 Caesar+2 格式里 `\'5F` → `'5F`），再逐档位移。
                val = val.replace("\\'", "'")
                for sh in range(1, 9):
                    dec = _up.unquote("".join(chr(ord(c) - sh) for c in val))
                    if any(k in dec for k in ("<source", ".m3u8", ".mp4", "<video")):
                        # 解码后 JS 内 `\"` 还原为 `"`（供后续 <source src="..."> 匹配）
                        val = dec.replace('\\"', '"')
                        break

            name = step.get("name") or f"s{i}"
            results[name] = val
            step_urls[name] = val

            # 是否继续请求该 URL 拿下一页
            fetch = step.get("fetch")
            if fetch is None:
                # 未显式指定：url 模板（构造请求地址）默认要请求。
                # decode 步骤是解密出文本（供后续步提取），不请求。
                # 其余：referer 非空 且 提取的不是视频文件（m3u8/mp4/mpd 等）
                # → 视为中间跳转页需请求（iframe → get3G 这类相对路径 .php 也请求）。
                # 视频文件后缀不请求，作为最终播放地址返回。
                if url_tpl and not regex:
                    fetch = True
                elif step.get("decode"):
                    fetch = False
                elif not step.get("referer"):
                    fetch = False
                else:
                    _low = val.lower()
                    fetch = not any(_low.endswith(s) for s in
                                    (".m3u8", ".mp4", ".mpd", ".ts", ".m4a", ".m4s"))
            if not fetch:
                cur_text = val  # 后续步骤在提取结果上继续（如解码后的 JS）
                continue

            ref_key = step.get("referer") or "episode"
            referer = step_urls.get(ref_key, step_urls.get("episode"))
            headers = self._headers(source)
            if referer and not referer.startswith(("http", "/")):
                referer = urljoin(self._abs_url(source, episode_url), referer)
            headers["Referer"] = referer or self._abs_url(source, episode_url)

            abs_step_url = val if val.startswith(("http://", "https://")) else urljoin(cur_url, val)
            if step.get("force_mp4_0"):
                import re as _r
                abs_step_url = (_r.sub(r"mp4=[^&]*", "mp4=0", abs_step_url)
                                if "mp4=" in abs_step_url
                                else abs_step_url + ("&" if "?" in abs_step_url else "?") + "mp4=0")
            try:
                timeout = self._timeout(source)
                retries = self._retries(source)
                interval_ms = self._interval_ms(source)
                if context is not None:
                    remaining = context.remaining()
                    if remaining <= 0:
                        context.check()
                        raise StreamCancelledError("stream resolution deadline exhausted")
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
                cur_text = self._http.get_text(
                    abs_step_url, headers=headers,
                    timeout=timeout, retries=retries,
                    interval_ms=interval_ms,
                    encoding=source.transports().get("charset"),
                    proxy_pool=source.proxy_pool(),
                )
                self._check_stream_context(context, deadline)
            except StreamCancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._check_stream_context(context, deadline)
                raise ContentMissingError(
                    f"http_chain 第{i}步请求失败 {abs_step_url}: {exc}",
                    source_id=source.source_id,
                ) from exc
            cur_url = abs_step_url
            step_urls[name] = abs_step_url
            if packer:
                # JS Packer（Dean Edwards）混淆的中间页响应，解包后还原明文
                # 供后续步骤（一般为最终视频地址的 regex）继续提取。
                cur_text = self._unpack_js_packer(cur_text)

        # 最后一步提取的 val 即播放地址
        final = val
        if final and not final.startswith(("http://", "https://")):
            final = urljoin(cur_url, final)
        # 解包文本中 URL 常带 JS 转义反斜杠（如 ...&via_bm=dx\\），还原为真实字符
        if final:
            final = final.replace("\\\\", "\\").rstrip("\\")
        if not final or not final.startswith("http"):
            raise ContentMissingError(
                f"http_chain 未取到播放地址（{episode_url}）", source_id=source.source_id
            )
        return final

    def _extract_play_url_candidates(
        self, html: str, source: SourceConfig, switch_cfg: dict,
        play_cfg: dict, abs_ep: str, episode_url: str
    ) -> list[str]:
        import html as _html

        values: list[str] = []
        seen: set[str] = set()

        def add(value: str) -> None:
            value = _html.unescape(str(value or "")).strip()
            value = value.replace("\\\\/", "/").replace("\\/", "/")
            value = value.replace('\\"', '"')
            if not value or value.startswith(("javascript:", "data:")):
                return
            if not value.startswith(("http://", "https://", "//")):
                value = urljoin(abs_ep, value)
            if value.startswith("//"):
                value = "https:" + value
            if value not in seen:
                seen.add(value)
                values.append(value)

        player_regex = switch_cfg.get(
            "play_regex", r'var\s+player_aaaa\s*=\s*(\{.*?\})(?:\s*</script>|;)'
        )
        for match in _re.finditer(player_regex, html, _re.S):
            raw = match.group(1) if match.groups() else match.group(0)
            raw = raw.replace("\\\\/", "/").replace("\\\\\"", '\"')
            try:
                player = json.loads(raw)
            except Exception:
                player = {}
            raw_url = player.get(switch_cfg.get("play_url_path", "url"), "") if player else ""
            if not raw_url:
                url_match = _re.search(
                    r'["\'](?:url|playurl|file)["\']\s*:\s*["\']([^"\']+)',
                    raw,
                    _re.I,
                )
                raw_url = url_match.group(1) if url_match else ""
            if raw_url:
                raw_url = str(raw_url).replace("\\\\/", "/").replace("\\/", "/")
                ps = str(player.get("ps", "0") or "0") if player else "0"
                parse_url = str(
                    (player.get("parse") if player else "")
                    or switch_cfg.get("default_parse")
                    or ""
                ).replace("\\/", "/")
                if ps == "1" and parse_url:
                    sep = "&" if "?" in parse_url else "?"
                    add(f"{parse_url}{sep}url={raw_url}")
                else:
                    if self._decrypter is not None:
                        try:
                            add(self._decrypter.decrypt(source, raw_url, "video_url"))
                        except StreamCancelledError:
                            raise
                        except TimeoutError:
                            raise
                        except Exception:
                            pass
                    else:
                        add(raw_url)

        for pattern in (
            r'var\s+now\s*=\s*["\']([^"\']+)',
            r'<(?:video|source)\b[^>]*\b(?:src|data-src)\s*=\s*["\']([^"\']+)',
            r'(?<![A-Za-z0-9])((?:https?:)?\/\/[^"\'\s<>]+?\.m3u8(?:\?[^"\'\s<>]*)?)',
        ):
            for match in _re.finditer(pattern, html, _re.I | _re.S):
                add(match.group(1))

        normalized = html.replace("\\/", "/").replace('\\"', '"')
        for match in _re.finditer(
            r'((?:https?://|//)[^"\'\s<>]+?\.m3u8(?:\?[^"\'\s<>]*)?)',
            normalized,
            _re.I,
        ):
            add(match.group(1))
        return values

    def _fetch_play_url_candidates_once(
        self, source: SourceConfig, episode_url: str, deadline=None, context=None
    ) -> list[str]:
        context = self._stream_context(context, deadline)
        self._check_stream_context(context, deadline)
        abs_ep = self._abs_url(source, episode_url)
        self._bg_check(source, abs_ep)
        content_cfg = self._content_block(source)
        block = content_cfg.get("episode") or {}
        play_cfg = block.get("play_url") or {}
        switch_cfg = block.get("source_switch") or {}
        if play_cfg.get("render") == "playwright" or play_cfg.get("http_chain"):
            return [
                self._call_with_context(
                    self._fetch_play_url_once, (source, episode_url), context, deadline
                )
            ]
        html = self._video_html_cache.get((source.source_id, abs_ep))
        if html is None:
            html = self._get(source, episode_url, deadline=deadline, context=context)
        self._check_stream_context(context, deadline)
        return self._extract_play_url_candidates(
            html, source, switch_cfg, play_cfg, abs_ep, episode_url
        )

    @staticmethod
    def _safe_url(url: str) -> str:
        parts = urlsplit(str(url or ""))
        return f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.netloc else "<redacted>"

    @staticmethod
    def _safe_exception_summary(exc: Exception) -> str:
        return type(exc).__name__

    @staticmethod
    def _call_with_remaining_timeout(func, args, kwargs, deadline):
        if deadline is None:
            return func(*args, **kwargs)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("stream resolution deadline exhausted")
        try:
            parameters = inspect.signature(func).parameters
            supports_deadline = "deadline" in parameters
            supports_timeout = (
                "timeout" in parameters
                or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD
                    for p in parameters.values()
                )
            )
        except (TypeError, ValueError):
            supports_deadline = False
            supports_timeout = True
        kwargs = dict(kwargs)
        if supports_deadline:
            kwargs["deadline"] = deadline
        elif supports_timeout:
            kwargs["timeout"] = remaining
        return func(*args, **kwargs)

    @staticmethod
    def _call_with_context(func, args, context, deadline=None):
        try:
            parameters = inspect.signature(func).parameters
            accepts_context = (
                "context" in parameters
                or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())
            )
            accepts_deadline = "deadline" in parameters
        except (TypeError, ValueError):
            accepts_context = False
            accepts_deadline = False
        kwargs = {}
        if accepts_context:
            kwargs["context"] = context
        elif accepts_deadline:
            kwargs["deadline"] = deadline
        return func(*args, **kwargs)

    @staticmethod
    def _call_with_deadline(func, args, deadline):
        if deadline is None:
            return func(*args)
        try:
            parameters = inspect.signature(func).parameters
            supports_deadline = (
                "deadline" in parameters
                or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD
                    for p in parameters.values()
                )
            )
        except (TypeError, ValueError):
            supports_deadline = True
        if supports_deadline:
            return func(*args, deadline=deadline)
        return func(*args)

    # ------------------------------------------------------------------ #
    # 换源站线路自动轮换（source_switch）
    # ------------------------------------------------------------------ #
    def _fetch_play_url_auto_switch(
        self, source: SourceConfig, episode_url: str, deadline=None, context=None
    ) -> str:
        """换源站取流：默认线路失败时自动尝试其他线路，全部失败才抛错。

        失败判定：取流抛错 / 返回空 / 播放 URL 不可访问（HTTP>=400 / 空内容 /
        超时，见 _play_url_probe）。换线路复用 switch_source 重取详情拿新线路
        分集 URL（如 play-{sid}-x.html）后重新取流。
        max_switch_attempts：最多尝试线路数（含默认线路），缺省 3，避免太慢。
        """
        context = self._stream_context(context, deadline)
        self._check_stream_context(context, deadline)
        switch_cfg = self._get_source_switch_cfg(source)
        max_attempts = max(1, int(switch_cfg.get("max_switch_attempts") or 3))
        # 已知死线路黑名单 TTL：可配（sources_runtime.dead_line_ttl_seconds，秒），
        # 缺省 600s 惰性过期（站点修复后自动恢复尝试）；限制在 [30, 3600]。
        try:
            dead_line_ttl = float(
                self._settings_opt("dead_line_ttl_seconds", 600.0) or 600.0
            )
        except (TypeError, ValueError):
            dead_line_ttl = 600.0
        dead_line_ttl = max(30.0, min(3600.0, dead_line_ttl))
        # 已知死线路黑名单：TTL 惰性过期（站点修复后自动恢复尝试）。
        now = time.monotonic()
        dead: dict = {
            k: ts
            for k, ts in (self._dead_episode_lines.get(source.source_id) or {}).items()
            if now - ts < dead_line_ttl
        }
        if dead:
            self._dead_episode_lines[source.source_id] = dead
        else:
            self._dead_episode_lines.pop(source.source_id, None)
        attempted: set = set()
        candidates: list = []
        current = episode_url
        last_err: Exception | None = None
        for _ in range(max_attempts):
            self._check_stream_context(context, deadline)
            key = self._episode_key(current)
            if key in attempted:
                break
            attempted.add(key)
            line_index = len(attempted)
            if key in dead:
                # probe 曾明确失败（403/404/空/超时）的线路：跳过抓取与 probe，
                # 直接进入候选轮换（省一次播放页 HTTP + 一次死链 probe）。
                log.info(
                    "[%s] 阶段=跳过，线路索引=%d，异常类型=KnownDeadLine",
                    source.source_id,
                    line_index,
                )
            else:
                try:
                    plays = self._call_with_context(
                        self._fetch_play_url_candidates_once,
                        (source, current), context, deadline
                    )
                    for play in plays:
                        self._check_stream_context(context, deadline)
                        usable = self._call_with_context(
                            self._play_url_probe, (source, play), context, deadline
                        )
                        if usable:
                            return play
                    if plays:
                        dead[key] = now
                        self._dead_episode_lines[source.source_id] = dead
                    last_err = ContentMissingError(
                        f"播放地址不可访问（已尝试 {len(plays)} 个候选，"
                        f"共 {len(attempted)} 条线路）",
                        source_id=source.source_id,
                    )
                except StreamCancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    last_err = ContentMissingError(
                        "线路取流失败",
                        source_id=source.source_id,
                    )
                    log.info(
                        "[%s] 阶段=取流，线路索引=%d，异常类型=%s",
                        source.source_id,
                        line_index,
                        self._safe_exception_summary(exc),
                    )
            if not candidates:
                try:
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    candidates = self._call_with_context(
                        self._switch_candidate_episode_urls,
                        (source, current), context, deadline
                    )
                except StreamCancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.info(
                        "[%s] 阶段=换线，线路索引=%d，异常类型=%s",
                        source.source_id,
                        line_index,
                        self._safe_exception_summary(exc),
                    )
                    candidates = []
            if not candidates:
                break
            current = candidates.pop(0)
        if last_err is None:
            last_err = ContentMissingError("未取到播放地址", source_id=source.source_id)
        raise ContentMissingError(
            f"全部线路取流失败（{source.source_id}）",
            source_id=source.source_id,
        )

    def _play_url_probe(self, source: SourceConfig, url: str, deadline=None, context=None) -> bool:
        """轻量探测播放 URL 是否可访问：HTTP<400 且响应非空即视为可用。

        403/404/空内容/超时 → False（触发换线路）。非 http(s) URL（如 data:）
        无法探测，直接视为可用（保持原行为，不误换线路）。

        用 requests（同播放/代理路径）而非 urllib：urllib 用系统/OpenSSL 证书
        库，对证书不规范的 CDN 会 SSL 校验失败 → probe 误判地址不可用，触发
        不必要的换线路（重抓详情页拖慢加载）。requests 走 certifi CA + 源
        headers，与播放一致。超时取源配置但上限 5s（探测只是可达性判断，
        不必等满 10s；慢 CDN 尽快失败换线路比白等更流畅）。
        """
        context = self._stream_context(context, deadline)
        try:
            self._check_stream_context(context, deadline)
        except StreamCancelledError:
            raise
        if not url or not url.startswith(("http://", "https://")):
            return False
        headers = dict(self._headers(source))
        # 探测只做可达性判断，不白等：超时取源配置但上限可配
        # （sources_runtime.play_url_probe_timeout_cap，秒），缺省 5s；
        # 限制在 [1, 30]。慢 CDN 尽快失败换线路比白等更流畅。
        try:
            probe_cap = float(
                self._settings_opt("play_url_probe_timeout_cap", 5.0) or 5.0
            )
        except (TypeError, ValueError):
            probe_cap = 5.0
        probe_cap = max(1.0, min(30.0, probe_cap))
        configured_timeout = min(float(self._timeout(source)) or 10.0, probe_cap)
        if context is not None:
            remaining = context.remaining()
            if remaining <= 0:
                context.check()
                raise StreamCancelledError("stream resolution deadline exhausted")
            timeout = min(configured_timeout, remaining)
        elif deadline is None:
            timeout = max(3.0, configured_timeout)
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            timeout = min(configured_timeout, remaining)
            if timeout <= 0:
                return False
        try:
            import requests as _req

            with _req.get(url, headers=headers, timeout=timeout, stream=True) as resp:
                if resp.status_code >= 400:
                    return False
                value = bool(resp.raw.read(4096))
                self._check_stream_context(context, deadline)
                return value
        except StreamCancelledError:
            raise
        except Exception:  # noqa: BLE001
            self._check_stream_context(context, deadline)
            return False

    def _switch_candidate_episode_urls(
        self, source: SourceConfig, episode_url: str, deadline=None, context=None
    ) -> list:
        """构造同集其他线路的分集 URL 列表（供失败自动换线路）。

        依赖 source_switch 配置 detail_url_regex（+可选 detail_url_template）
        从分集 URL 推导详情页 URL，再走 switch_source 取各线路分集列表，
        按当前集的位置/集数号匹配出同集在新线路上的 URL。
        """
        context = self._stream_context(context, deadline)
        self._check_stream_context(context, deadline)
        switch = self._get_source_switch_cfg(source)
        detail_url = self._detail_url_from_episode(source, episode_url)
        if not detail_url:
            return []
        m_sid = _re.search(r"sid=(\d+)", episode_url)
        cur_sid = m_sid.group(1) if m_sid else str(switch.get("default_sid") or "1")
        try:
            if deadline is None and context is None:
                base_detail, base_chs = self.switch_source(source, detail_url, cur_sid)
            else:
                base_detail, base_chs = self.switch_source(
                    source, detail_url, cur_sid, deadline=deadline, context=context
                )
            self._check_stream_context(context, deadline)
        except StreamCancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.info(
                "[%s] 阶段=换线，线路序号=0，异常类型=%s",
                source.source_id,
                self._safe_exception_summary(exc),
            )
            return []

        # 当前集在新线路分集列表中的位置（精确 URL 匹配，回退 URL 末段集数号）
        idx = self._episode_index_in_chapters(episode_url, base_chs)
        out: list = []
        seen: set = set()
        for switch_index, line in enumerate(base_detail.source_list or [], 1):
            cand_sid = str(line.get("sid") or "")
            if not cand_sid or cand_sid == cur_sid:
                continue
            try:
                self._check_stream_context(context, deadline)
                if deadline is None and context is None:
                    _, chs = self.switch_source(source, detail_url, cand_sid)
                else:
                    _, chs = self.switch_source(
                        source, detail_url, cand_sid, deadline=deadline, context=context
                    )
                self._check_stream_context(context, deadline)
            except StreamCancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.info(
                    "[%s] 阶段=换线，线路序号=%d，异常类型=%s",
                    source.source_id,
                    switch_index,
                    self._safe_exception_summary(exc),
                )
                continue
            if not chs:
                continue
            u = chs[idx].url if idx is not None and idx < len(chs) else chs[0].url
            if u and u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _detail_url_from_episode(self, source: SourceConfig, episode_url: str) -> str:
        """从分集 URL 推导详情页 URL（source_switch 配置驱动）。

        detail_url_regex：正则作用于分集 URL；detail_url_template：可选，
        用 {1}/{2} 引用捕获组重建详情 URL，缺省直接用捕获组 1。
        无法推导返回 ""。
        """
        switch = self._get_source_switch_cfg(source)
        regex = switch.get("detail_url_regex") or ""
        if not regex:
            return ""
        m = _re.search(regex, episode_url)
        if not m:
            return ""
        tpl = switch.get("detail_url_template")
        if tpl:
            out = tpl
            for i in range(len(m.groups())):
                out = out.replace("{%d}" % (i + 1), m.group(i + 1) or "")
            return out
        return m.group(1) or ""

    @staticmethod
    def _episode_key(url: str) -> str:
        """归一化分集 URL（去 fragment、去尾斜杠），用于去重/定位。"""
        return _re.sub(r"#.*$", "", url).rstrip("/")

    def _episode_index_in_chapters(self, episode_url: str, chapters: List[Chapter]):
        """在分集列表中定位当前集的位置：先精确 URL 匹配，回退按 URL 末段数字。"""
        target = self._episode_key(episode_url)
        for i, ch in enumerate(chapters):
            if self._episode_key(ch.url) == target:
                return i
        cur_n = self._episode_ordinal(episode_url)
        if cur_n is not None:
            for i, ch in enumerate(chapters):
                if self._episode_ordinal(ch.url) == cur_n:
                    return i
        return None

    @staticmethod
    def _episode_ordinal(url: str):
        """URL 路径末段数字（分集号/集索引），用于跨线路定位同一集。"""
        path = url.split("?", 1)[0].rstrip("/")
        m = _re.search(r"(\d+)$", path)
        return int(m.group(1)) if m else None

    def _fetch_streams_ytdlp(self, source: SourceConfig, episode_url: str, cfg: dict,
                             quality: str = "", merged: bool = False, deadline=None, context=None) -> tuple:
        """yt-dlp 引擎：拿高清双流（YouTube 需签名，委托 yt-dlp）。

        merged=True 时强制 `best` 合并单流（含音视频）：VLC input-slave
        对双流 DASH/fMP4 支持不可靠（黑屏），播放端走单流最稳；且少一次
        yt-dlp 子进程调用（bestvideo+bestaudio 是两次 --get-url，合并一次）。

        缓存：yt-dlp 取流 ~10s（签名+网络），同视频短时间复用，避免重复等待。
        """
        context = self._stream_context(context, deadline)
        self._check_stream_context(context, deadline)
        yt = self._get_ytdlp()

        # 分集 URL 可能是相对路径（HTML 搜索结果未绝对化时）→ 补全，
        # yt-dlp 只认完整 URL
        episode_url = self._abs_url(source, episode_url)

        fmt = "best" if merged else self._ytdlp_format(cfg, quality)
        try:
            streams = self._call_with_remaining_timeout(
                yt.fetch_streams, (episode_url,), {"fmt": fmt},
                context.deadline if context is not None else deadline
            )
            self._check_stream_context(context, deadline)
        except StreamCancelledError:
            raise
        except Exception as exc:
            self._check_stream_context(context, deadline)
            # 高清失败回退单流
            try:
                streams = (
                    self._call_with_remaining_timeout(
                        yt.fetch_streams, (episode_url,), {"fmt": "best"},
                        context.deadline if context is not None else deadline
                    )[0],
                    "",
                )
                self._check_stream_context(context, deadline)
            except StreamCancelledError:
                raise
            except Exception:
                self._check_stream_context(context, deadline)
                raise ContentMissingError(
                    "yt-dlp 取流失败",
                    source_id=source.source_id,
                ) from None
        self._check_stream_context(context, deadline)
        return streams

    def _get_ytdlp(self):
        """懒加载单例 Ytdlp（复用，避免每次子进程重复探测）。"""
        if self._ytdlp is None:
            from framework.ytdlp import Ytdlp

            self._ytdlp = Ytdlp()
        return self._ytdlp

    # ------------------------------------------------------------------ #
    # 换源支持（source_switch）
    # ------------------------------------------------------------------ #
    def _get_source_switch_cfg(self, source: SourceConfig) -> dict:
        """取源配置 content.episode.source_switch（换源配置），无则 {}。"""
        content_cfg = self._content_block(source)
        block = content_cfg.get("episode") or {}
        return block.get("source_switch") or {}

    def _parse_source_list(self, source: SourceConfig, html: str, context=None) -> List[dict]:
        """从详情页 HTML 提取所有可用播放源。

        source_switch 配置：
            list_selector  提取源 tab 的根选择器（如 .player_name[data-sid]）
            list_attr      源 ID 属性名（data-sid）
            name_selector  源名选择器（缺省用根节点的文本）
            playerconfig_url  可选：加载 playerconfig.js 拿各源 ps/parse
        返回 [{sid, name, from_, ps, parse}]；无配置返回 []。
        """
        switch = self._get_source_switch_cfg(source)
        if not switch:
            return []
        doc = self._parser.parse(html)
        list_sel = switch.get("list_selector")
        attr = switch.get("list_attr", "data-sid")
        name_sel = switch.get("name_selector") or list_sel
        if not list_sel:
            return []
        roots = self._parser._query(doc, list_sel, None)
        name_nodes = self._parser._query(doc, name_sel, None) if name_sel else roots
        out: List[dict] = []
        seen: set = set()
        for i, node in enumerate(roots):
            sid = node.get(attr) if hasattr(node, "get") else None
            # 无现成 sid 属性时：sid_regex 从属性值提取（如 li id="tab83" → sid=3）
            if sid and switch.get("sid_regex"):
                m_sid = _re.search(switch["sid_regex"], str(sid))
                if m_sid:
                    sid = m_sid.group(1) if m_sid.groups() else m_sid.group(0)
            # 属性无 sid 时：sid_regex 从节点 html 提取
            # （如 div.video_list_li 内含 /v/56673-2-1/ 的线路分集块）
            if not sid and switch.get("sid_regex"):
                try:
                    from lxml import etree as _etree

                    html_repr = _etree.tostring(node, encoding="unicode")
                    m_sid = _re.search(switch["sid_regex"], html_repr)
                    if m_sid:
                        sid = m_sid.group(1) if m_sid.groups() else m_sid.group(0)
                except Exception:
                    pass
            if not sid or sid in seen:
                continue
            seen.add(sid)
            name = ""
            if i < len(name_nodes) and hasattr(name_nodes[i], "text_content"):
                name = (name_nodes[i].text_content() or "").strip()
            if not name:
                name = node.text_content().strip() if hasattr(node, "text_content") else ""
            # data-from 可能在节点或其子元素上（如 span[data-from]）
            from_ = ""
            if hasattr(node, "get"):
                from_ = node.get("data-from") or ""
            if not from_ and hasattr(node, "iter"):
                for sub in node.iter():
                    if hasattr(sub, "get") and sub.get("data-from"):
                        from_ = sub.get("data-from")
                        break
            out.append({
                "sid": sid,
                "name": name,
                "from_": from_,
                "ps": "0",
                "parse": "",
            })
        # 加载 playerconfig.js 补全各源 ps/parse（可选；失败用默认 ps=0 直接用 url）
        pc_url = switch.get("playerconfig_url")
        if pc_url and out:
            try:
                self._check_stream_context(context)
                pc_html = self._get(source, self._abs_url(source, pc_url), context=context)
                self._check_stream_context(context)
                self._merge_parse_config(out, pc_html)
            except StreamCancelledError:
                raise
            except Exception:
                pass  # playerconfig 加载失败 → 所有源 ps=0 直接用 url
        return out

    @staticmethod
    def _merge_parse_config(source_list: List[dict], pc_html: str) -> None:
        """解析 playerconfig.js 的 player_list，把各 from 的 ps/parse 合入源列表。"""
        import json as _json

        m = _re.search(
            r'MacPlayerConfig\.player_list=(\{.*?\}),MacPlayerConfig', pc_html, _re.S
        )
        if not m:
            return
        try:
            raw = m.group(1)
            # JS 对象 → 尽量解析（字符串键无引号也能被 json 容错处理）
            raw = _re.sub(r"([{\s,])(\w+)\s*:", r'\1"\2":', raw)
            players = _json.loads(raw)
        except Exception:
            return
        for item in source_list:
            cfg = players.get(item.get("from_")) or {}
            item["ps"] = str(cfg.get("ps", "0") or "0")
            item["parse"] = (cfg.get("parse") or "").replace("\\/", "/")
            if not item["name"] and cfg.get("show"):
                item["name"] = cfg["show"]

    def _fetch_play_url_from_player(
        self, html: str, switch_cfg: dict, episode_url: str, source: SourceConfig = None
    ) -> str:
        """从 player_aaaa JS 配置提取真实播放地址。

        player_aaaa 结构：
            {url: 播放地址, from: 源类型, sid: 源ID, nid: 集数, ps: 0/1}
        ps=0：url 直接用；ps=1：url 需经 parse 接口转码（{parse}?url={url}）
        """
        regex = switch_cfg.get("play_regex", r'var player_aaaa=(\{.*?\})\s*</script>')
        url_field = switch_cfg.get("play_url_path", "url")
        m = _re.search(regex, html)
        if not m:
            raise ContentMissingError(
                f"未匹配到播放配置（{episode_url}）", source_id=(source.source_id if source else "")
            )
        try:
            obj = json.loads(m.group(1))
        except Exception as exc:
            raise ContentMissingError(
                f"播放配置解析失败（{episode_url}）：{exc}",
                source_id=(source.source_id if source else ""),
            ) from exc
        raw_url = obj.get(url_field) or ""
        if not raw_url:
            raise ContentMissingError(
                f"播放配置无 {url_field}（{episode_url}）",
                source_id=(source.source_id if source else ""),
            )
        raw_url = raw_url.replace("\\/", "/")
        # ps=1 → 走 parse 接口转码（iframe 外链源 → m3u8；parse 端自解加密）
        ps = str(obj.get("ps", "0") or "0")
        parse_url = (obj.get("parse") or switch_cfg.get("default_parse") or "")
        parse_url = parse_url.replace("\\/", "/")
        if ps == "1" and parse_url:
            sep = "&" if "?" in parse_url else "?"
            return f"{parse_url}{sep}url={raw_url}"
        # ps=0 → url 直用（MacCMS 密文时按 decryption.targets.video_url 解密）
        if source is not None and self._decrypter is not None:
            return self._decrypter.decrypt(source, raw_url, "video_url")
        return raw_url

    def _fetch_chapters_for_source(
        self, source: SourceConfig, doc, detail_url: str, sid: str
    ) -> List[Chapter]:
        """按指定播放源 sid 从详情页 doc 提取分集列表。

        source_switch.ep_list_selector 含 {sid} 占位 → 替换为当前 sid。
        返回 Chapter 列表，每条带 source_id=sid。
        """
        switch = self._get_source_switch_cfg(source)
        ep_sel_tpl = switch.get("ep_list_selector")
        if not ep_sel_tpl:
            return []
        ep_sid_re = switch.get("ep_sid_regex")
        href_needle = switch.get("ep_href_contains", "/tv/")
        # 分集列表选择器：{sid} → 当前源
        ep_sel = ep_sel_tpl.replace("{sid}", str(sid))
        nodes = self._parser._query(doc, ep_sel, None)
        chapters: List[Chapter] = []
        seen: set = set()
        for node in nodes:
            href = node.get("href") if hasattr(node, "get") else None
            if not href or href_needle not in href:
                continue
            # 多线路同页：ep_sid_regex 从 href 提取线路号，非当前 sid 跳过
            if ep_sid_re:
                m_es = _re.search(ep_sid_re, href)
                link_sid = (
                    m_es.group(1)
                    if m_es and m_es.groups()
                    else (m_es.group(0) if m_es else "")
                )
                if str(link_sid) != str(sid):
                    continue
            if href in seen:
                continue
            seen.add(href)
            title = node.text_content().strip() if hasattr(node, "text_content") else ""
            # 确保 URL 带 sid（无则补）
            if not switch.get("ep_sid_in_path") and f"sid={sid}" not in href:
                sep = "&" if "?" in href else "?"
                href = f"{href}{sep}sid={sid}"
            abs_url = self._abs_url(source, href)
            chapters.append(Chapter(title=title or f"第{len(chapters)+1}集", url=abs_url, source_id=str(sid)))
        # 去重标题（同标题不同 URL 的只留第一个）
        seen_t = set()
        deduped = []
        for ch in chapters:
            t = ch.title or ""
            if t in seen_t:
                continue
            seen_t.add(t)
            deduped.append(ch)
        return deduped

    def switch_source(
        self, source: SourceConfig, detail_url: str, new_sid: str, deadline=None, context=None
    ) -> tuple:
        """切换播放源：重新抓取该源详情页，返回 (更新后的 Detail, 新分集列表)。

        detail_url：当前详情 URL（含当前 sid）；new_sid：目标播放源 sid。
        返回的 Detail 保留元数据，chapters 换成新源分集，source_list 保留。
        """
        context = self._stream_context(context, deadline)
        self._check_stream_context(context, deadline)
        # 构造新 URL（换 sid 参数）
        new_url = _re.sub(r"sid=\d+", f"sid={new_sid}", detail_url)
        if f"sid={new_sid}" not in new_url:
            sep = "&" if "?" in new_url else "?"
            new_url = f"{new_url}{sep}sid={new_sid}"
        # 重新抓详情（fetch_detail 内部会按新 sid 用 _fetch_chapters 提取新分集）
        if deadline is None:
            detail = self.fetch_detail(source, new_url)
        else:
            detail = self.fetch_detail(source, new_url, deadline=deadline, context=context)
        return detail, detail.chapters

    @staticmethod
    def _ytdlp_format(cfg: dict, quality: str = "") -> str:
        """quality → yt-dlp 格式串（下载端双流路径）。

        下载用双流（bestvideo+bestaudio，ffmpeg 合并出高清）；播放端
        由 _fetch_streams_ytdlp(merged=True) 强制 `best` 合并单流，不经本方法。
        """
        fmt = str(cfg.get("format") or "best")
        if not quality or quality == "best":
            return fmt
        # 精确画质：高度限制（bestvideo[height<=X]+bestaudio，ffmpeg 可合并）
        m = __import__("re").match(r"^(\d{3,4})p$", quality or "")
        if m:
            h = m.group(1)
            return f"bestvideo[height<={h}]+bestaudio/best[height<={h}]"
        return fmt

    def _fetch_episode_api(self, source: SourceConfig, episode_url: str, cfg: dict, want_streams: bool = False, quality: str = "", deadline=None, context=None) -> str | dict:
        """api_endpoints.episode：JSON API 取播放地址（支持 sign 签名）。

        want_streams=True 时返回 {"video": ..., "audio": ...}（dash 音视频分离）。
        quality: 画质名（"best"/"1080p"/...），经 cfg["quality"]["map"] 映射为
        请求参数值（如 B 站 qn）；缺省用 quality.map 的默认值。
        """
        from urllib.parse import urlencode, urljoin

        context = self._stream_context(context, deadline)
        self._check_stream_context(context, deadline)
        api_url = str(cfg.get("url") or "")
        params = cfg.get("params") or {}
        # 画质映射：{quality} 占位 → 具体请求参数值（如 B 站 qn）
        quality_map = (cfg.get("quality") or {}).get("map") or {}
        q_param = (cfg.get("quality") or {}).get("param") or "quality"
        q_default = (cfg.get("quality") or {}).get("default") or "best"
        q_value = quality_map.get(quality, quality_map.get(q_default, quality or q_default))
        # {id} 从 episode_url 末段提取，去掉 .html/.htm 扩展名（avgood 分集 URL 带扩展名，
        # 直接拼接会成 xxx.html.html → 接口返回"视频不存在"）
        ep_id = episode_url.split("/")[-1]
        if ep_id.endswith((".html", ".htm")):
            ep_id = ep_id.rsplit(".", 1)[0]
        # 占位符同样替换进 URL 路径（如 avgood /play/ajax/{id}.html）。
        # 原实现只在 params 值里替换，URL 路径里的 {id} 会原样发出 → 接口返回"不存在"
        api_url = api_url.replace("{id}", ep_id)
        # 从 episode_url 提取一次 bvid/cid，路径与 params 值共用（避免每参数重复 re.search）
        bvid = ""
        m_bv = _re.search(r"(BV[0-9A-Za-z]+)", episode_url)
        if m_bv:
            bvid = m_bv.group(1)
            api_url = api_url.replace("{bvid}", bvid)
        cid = ""
        m_cid = _re.search(r"(?:cid|p)=(\d+)", episode_url)
        if m_cid:
            cid = m_cid.group(1)
            api_url = api_url.replace("{cid}", cid)
        api_url = api_url.replace("{" + q_param + "}", str(q_value))
        api_url = api_url.replace("{quality}", str(q_value))
        filled = {}
        # 占位符：{bvid}/{cid}/{id} 从 episode_url 提取；{quality} 从画质映射；{keyword} 不适用
        for k, v in params.items():
            val = str(v)
            val = val.replace("{id}", ep_id)
            # 从 episode_url 提取 bvid / cid（复用上面已提取的局部变量）
            if bvid:
                val = val.replace("{bvid}", bvid)
            if cid:
                val = val.replace("{cid}", cid)
            val = val.replace("{" + q_param + "}", str(q_value))
            # 兼容通用占位符 {quality}（不论 param 名是什么都替换）
            val = val.replace("{quality}", str(q_value))
            filled[k] = val
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
        timeout = self._timeout(source)
        retries = self._retries(source)
        if context is not None:
            timeout = min(timeout, context.remaining())
            retries = 0
            self._check_stream_context(context)
        resp = self._http.get_json(
            abs_url,
            headers=self._endpoint_headers(source, cfg),
            timeout=timeout,
            retries=retries,
            proxy_pool=source.proxy_pool(),
        )
        self._check_stream_context(context, deadline)
        rpath = cfg.get("response_path") or ""
        node = resp
        if rpath:
            node = self._jsonpath(resp, rpath)
            if node is None:
                return ""
        # 取播放地址：优先 extractors（目标字段→JSONPath），否则字符串/第一值
        extractors = cfg.get("field_extractors") or {}
        url_path = extractors.get("play_url") or ""
        if url_path:
            play = self._jsonpath(resp if not url_path.startswith(".") else node, url_path) if url_path else node
        else:
            play = node
        # 若是列表，取首个非空元素；若是带 baseUrl 的对象，取 baseUrl
        if isinstance(play, list):
            play = next((it for it in play if it), "")
            if isinstance(play, dict):
                play = next((play[k] for k in ("baseUrl", "url") if play.get(k)), "")
        elif isinstance(play, dict):
            play = next((play[k] for k in ("baseUrl", "base_url", "url") if play.get(k)), "")
        play_url = str(play) if play else ""
        # 相对播放地址补全（如 avgood playlink 是 /remote_m3u8/...，缺绝对地址无法播）
        if play_url and not play_url.startswith(("http://", "https://", "data:")):
            play_url = urljoin(source.base_url, play_url)

        # dash 双流：额外取音频轨 URL（B 站音视频分离）
        if want_streams:
            audio_url = ""
            audio_path = extractors.get("audio_url") or ""
            if audio_path:
                au = self._jsonpath(resp, audio_path)
                if isinstance(au, list):
                    au = next((it for it in au if it), "")
                if isinstance(au, dict):
                    au = next((au[k] for k in ("baseUrl", "url") if au.get(k)), "")
                audio_url = str(au) if au else ""
                if audio_url and not audio_url.startswith(("http://", "https://", "data:")):
                    audio_url = urljoin(source.base_url, audio_url)
            return {"video": play_url, "audio": audio_url}
        return play_url

    @staticmethod
    def _filter_ad_images(images: List[str], source: SourceConfig) -> List[str]:
        """剔除广告图（URL 路径含广告标记）。

        优先走 ad_block 引擎的 is_ad_image_url（含域名黑名单/query 特征/
        源级配置），        再保留旧版路径段特征与 .gif 广告动图剔除作为兜底。
        """
        from urllib.parse import urlparse

        gif_marker = ".gif"
        # ad_block 引擎（含源级配置；无配置时用内置规则）
        try:
            from framework.adblock import adblock_for

            ad = adblock_for(source)
        except Exception:
            ad = None
        filtered = []
        for url in images:
            low = url.lower()
            path = urlparse(low).path
            # gif 广告动图（旧版规则，保留）
            if gif_marker in path:
                continue
            # ad_block 引擎判定（域名黑名单/路径/query/源级）
            if ad is not None and ad.enabled and ad.is_ad_image_url(url):
                continue
            # 旧版路径段特征（引擎未命中时兜底）
            if _AD_IMAGE_RE.search(path):
                continue
            filtered.append(url)
        return filtered
