"""视频：取流、换源、播放直链、yt-dlp。

B4 拆分自 content.py，方法原样迁移，行为不改变。
"""

from __future__ import annotations

from .shared import (
    Chapter, ContentMissingError, StreamCancelledError, _AD_IMAGE_RE,
    _re, inspect, json, log,
    threading, time, urljoin, urlsplit,
)


def _signed_url_remaining_ttl(values) -> float:
    """从带时效签名的 URL 推断剩余可用时长（秒）；0 = 无法判断。

    渲染结果缓存用它兜住「签名 10 分钟就过期」的地址：缓存 TTL 不能比
    签名寿命还长，否则续播/重播会拿到过期地址。
    """
    from urllib.parse import parse_qs

    expiries = []
    for value in values:
        if not value:
            continue
        try:
            query = parse_qs(urlsplit(str(value)).query)
        except Exception:  # noqa: BLE001
            continue
        for name in ("expire", "expires", "expires_at", "x-amz-expires",
                     "auth_key", "sign", "deadline"):
            raw = (query.get(name) or [""])[0]
            if not raw:
                continue
            # auth_key 形如 "<ts>-<hash>-0-<hash>"，取首段时间戳
            head = str(raw).split("-", 1)[0]
            try:
                stamp = float(head)
            except (TypeError, ValueError):
                continue
            if stamp > 1e12:      # 毫秒
                stamp /= 1000.0
            if stamp > 0:
                expiries.append(stamp)
    if not expiries:
        return 0.0
    return max(0.0, min(expiries) - time.time() - 5.0)


# ---- 播放链路探测的小工具（见 VideoMixin._probe_playback_chain） ----
_PROBE_CHUNK = 8192          # 分块读清单的块大小：一次读满整份大清单会卡到超时


def _probe_wants_manifest(url: str, ctype: str) -> bool:
    text = f"{url or ''} {ctype or ''}".lower()
    return ".m3u8" in text or "mpegurl" in text


def _probe_manifest_ready(text: str) -> bool:
    """够判定了吗：能分类（master/media）+ 已拿到首个 URI。

    读到「首个分片」就够验可达性，不必等整份清单（2500+ 段 = 200KB+）。
    """
    if "#EXTM3U" not in text[:512]:
        return "#EXT-X-ENDLIST" in text
    if "#EXT-X-ENDLIST" in text:
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    has_uri = any(not line.startswith("#") for line in lines)
    if not has_uri:
        return False
    return any(
        line.startswith(("#EXT-X-STREAM-INF", "#EXTINF")) for line in lines
    )


class VideoMixin:
    def fetch_video_streams(self, source: SourceConfig, episode_url: str, quality: str = "",
                            merged: bool = False, deadline=None, context=None,
                            source_lock=False) -> tuple:
        from framework.stream_resolver import (
            StreamResolver, stream_resolver_defaults, stream_resolver_options,
        )

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
        # 源级预算（sources/<id>.json 的 stream_resolver.candidate_timeout /
        # total_budget）：慢站单次取流可能远超默认 10s，不放大就会在候选阶段
        # 被判超时、异常被吞成空地址，表现为播放黑屏（omofuna 实测取流 22s）。
        result = resolver.resolve(
                source, episode_url, quality, merged, deadline=deadline, context=context,
                source_lock=source_lock, **stream_resolver_options(source)

        )
        video = (result[0] if result else "") or ""
        # 只拿到「解析器外壳页」时不要把 URL 丢给播放器：mpv 会把网页当媒体打开，
        # 结果是空窗口＋无进度（实测 omofuna/agedm）。宁可给出明确错误让用户换
        # 线路/查网络，也不给一个看起来在播、其实什么都没有的窗口。
        if video and self._looks_like_parser_shell(video):
            raise ContentMissingError(
                f"只解析到播放器外壳页（{urlsplit(video).hostname or video}），"
                "未取到真实视频流；可换线路重试或检查网络/代理",
                source_id=source.source_id,
            )
        return result

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
        if not api.get("episode"):
            direct_url = episode_url
            if source.source_id == "51cg1":
                # 51cg1 页面内嵌 JSON 常把 URL 写成 `https:\/\/...`，仅该源还原。
                direct_url = str(episode_url or "").replace("\\/", "/").replace('\\"', '"')
            if self._looks_like_direct_media(direct_url):
                return self._apply_media_url_rewrite(source, direct_url)
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

    def _render_skipped(self, source: SourceConfig, play_cfg: dict,
                        switch_cfg: dict, html: str) -> bool:
        """该线路的解析器是否已知不可用（play_url.render_skip_from 命中 from）。

        判定读 player_aaaa 的 `from` 字段（MacCMS 的线路类型：mp4/bfzym3u8/
        hls…）。源把「解析器已失效的线路类型」写进 render_skip_from 后，
        命中即不渲染、直接判失败换线路——不能因为一条坏线路把整次取流
        拖到几十秒。
        """
        skip = play_cfg.get("render_skip_from")
        if not skip:
            return False
        if isinstance(skip, str):
            skip = [skip]
        try:
            wanted = {str(s).strip().lower() for s in skip if str(s).strip()}
        except Exception:  # noqa: BLE001
            return False
        if not wanted or not html:
            return False
        regex = (switch_cfg or {}).get(
            "play_regex", r"var\s+player_aaaa\s*=\s*(\{.*?\})(?:\s*</script>|;)")
        try:
            match = _re.search(regex, html, _re.S)
            if not match:
                return False
            raw = (match.group(1) if match.groups() else match.group(0))
            raw = raw.replace("\\\\/", "/").replace("\\\\\"", '"')
            player = json.loads(raw)
        except Exception:  # noqa: BLE001 —— 解析不了就当没命中
            return False
        return str(player.get("from") or "").strip().lower() in wanted

    def _line_has_no_source(self, switch_cfg: dict, html: str) -> bool:
        """播放页 player_aaaa 存在但 url 为空 → 该线路没有片源。

        omofuna 实测：sid=3/sid=4 两条线路的 `player_aaaa.url` 是空串（站点
        只给了集数导航，没有挂片源），而配置里 default_sid 恰是 3。空 url 时
        候选链拿不到任何地址 → 走 playwright_fallback 渲染 → 播放页没有播放器
        iframe，渲染必然等到超时（实测整次取流 11.5s，其中约 10s 白等），最后
        才换到 sid=2 拿到真链（0.75s）。

        这种线路不是「解析器失效」，而是「根本没有片源」，渲染再多也变不出来
        ——直接判失败交给上层换线路。

        只在 switch_cfg 配了 play_regex（即读 player_aaaa 的源）时生效；
        player_aaaa 缺失 / 解析失败一律返回 False（结构变了不能乱判）。
        """
        if not html or not switch_cfg:
            return False
        regex = switch_cfg.get("play_regex")
        if not regex:
            return False
        url_field = switch_cfg.get("play_url_path", "url")
        try:
            match = _re.search(regex, html, _re.S)
            if not match:
                return False
            raw = (match.group(1) if match.groups() else match.group(0))
            raw = raw.replace("\\\\/", "/").replace("\\\\\"", '"')
            player = json.loads(raw)
        except Exception:  # noqa: BLE001 —— 解析不了就当没命中
            return False
        if not isinstance(player, dict):
            return False
        return not str(player.get(url_field) or "").strip()

    def _play_page_html_cached(self, source: SourceConfig, abs_ep: str) -> str:
        """取播放页 HTML 的缓存命中内容（不发请求）；未命中返回空串。"""
        cache = getattr(self, "_play_page_cache", None) or {}
        hit = cache.get((source.source_id, abs_ep))
        if not hit:
            return ""
        expire, html = hit
        return html if expire > time.monotonic() else ""

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

        # 18AV 的主播放器参数只在浏览器执行 content_mv_protect.js 后写入
        # mvarr；详情页里的 video[data-src] 是 9 秒预览，不能作为主流。
        if source.source_id == "18av":
            return self._fetch_18av_player_stream(
                source, episode_url, deadline=deadline, context=context
            )

        # Playwright 渲染播放页 → iframe 解析站 → video src（agedm 类嵌套 iframe 站）。
        # 渲染分支不消费播放页 HTML：URL 交给 Playwright 即可，跳过此前的纯 HTTP
        # 抓取（同 URL 会抓两次，实测 dm84 每次多耗 1.6-1.9s）。
        #
        # render == "playwright"      ：硬切换，纯渲染（老行为）
        # render == "playwright_fallback"：先走普通 HTML 解析，拿不到「可直接播放」
        #   的直链时才渲染。omofuna 这类站「部分线路直链可播（暴风 m3u8）、部分线路
        #   只有解析器 id（精品 hash→v2player）」时，硬渲染会让本可直接播的线路
        #   退化成解析器 URL；fallback 才能两种线路都拿到可播地址。
        render_mode = play_cfg.get("render")
        if render_mode == "playwright":
            return self._fetch_play_url_rendered(
                source, episode_url, abs_ep, block, play_cfg,
                deadline=deadline, context=context,
            )
        if render_mode == "playwright_fallback":
            try:
                value = self._fetch_play_url_http_once(
                    source, episode_url, abs_ep, block, play_cfg,
                    deadline=deadline, context=context,
                )
            except Exception:  # noqa: BLE001 —— 普通解析失败 → 交渲染兜底
                value = ""
            if self._is_playable_direct(value):
                return value
            # 该线路的解析器已失效（render_skip_from 命中）→ 直接判失败，
            # 让上层换线路。omofuna 精品线路（from=mp4，只有不透明 id）实测
            # 渲染 47s 仍拿不到流（解析站 429/空壳），继续渲染等于把好线路
            # 一起拖死：用户看到的就是「取不到播放流」。
            # 判定要吃**播放页 HTML**（player_aaaa 在里面）：早先这里传的是
            # 播放页 URL，正则永远匹配不到 → skip 从不生效 → 每条坏线路都先
            # 白渲染 20~40s。
            play_html = self._play_page_html_cached(source, abs_ep)
            if self._render_skipped(source, play_cfg, switch_cfg, play_html):
                raise ContentMissingError(
                    f"该线路解析器不可用，已跳过（{abs_ep}）",
                    source_id=source.source_id,
                )
            # 该线路压根没挂片源（player_aaaa.url 为空）→ 渲染同样变不出地址，
            # 立刻判失败换线路，别把 10s 花在必然超时的渲染上。
            if self._line_has_no_source(
                switch_cfg, self._play_page_html_cached(source, abs_ep)
            ):
                raise ContentMissingError(
                    f"该线路无片源（player_aaaa.url 为空），已跳过（{abs_ep}）",
                    source_id=source.source_id,
                )
            return self._fetch_play_url_rendered(
                source, episode_url, abs_ep, block, play_cfg,
                deadline=deadline, context=context,
            )

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
        #   - decode: url_unquote = 纯 URL 解码（MacCMS encrypt=1 的 player.url）
        #   - decode: base64_shift_json = Base64 字节逐位移位后解析 JSON；
        #     shift_regex 从当前文本提取移位值（Rou.video）
        #   - rewrite: [{"regex": "...", "replacement": "..."}] 对提取值
        #     做声明式 URL 重写；replacement 支持 {1} 等捕获组引用
        #   - 最后一步提取出的串即播放地址（http 开头）
        chain_cfg = play_cfg.get("http_chain")
        if chain_cfg:
            # http_chain 失败要能降级到常规候选链（player_aaaa / regex）：站点
            # 播放页结构一旦变化，链条第一步就匹配不上，直接抛错会让整个源不可用。
            # 只有 StreamCancelledError（预算耗尽）必须原样上抛，不降级。
            try:
                return self._fetch_http_chain(
                    source, episode_url, html, chain_cfg,
                    deadline=deadline, context=context
                )
            except StreamCancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 记一笔，继续走常规链路
                log.info(
                    "[%s] 阶段=取流，http_chain 失败，异常类型=%s",
                    source.source_id,
                    self._safe_exception_summary(exc),
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

    def _render_if_parser_shell(self, source: SourceConfig, play: str,
                                context=None, deadline=None,
                                episode_url: str = "") -> str:
        """若 play 是「解析器外壳页」，渲染它并返回真实视频地址；否则返回 ""。

        只在源显式声明 content.episode.play_url.render（playwright /
        playwright_fallback）时展开，其他源一律不动（保持原行为）。

        渲染目标是**原始播放页**（episode_url）而不是外壳 URL：外壳页
        （如 art.v2player.top/player/?url=<id>）脱离宿主页时缺少 dmid/next/
        name/nid 等参数，解析器不会启动（实测渲染外壳恒为空）；从播放页进入时
        站点自己补全这些参数，iframe 才能解出真实视频地址。
        """
        if not self._looks_like_parser_shell(play):
            return ""
        content_cfg = self._content_block(source)
        block = content_cfg.get("episode") or {}
        play_cfg = block.get("play_url") or {}
        mode = play_cfg.get("render")
        if mode not in ("playwright", "playwright_fallback"):
            return ""
        rc = content_cfg.get("render_config") or {}
        from framework.playwright_helper import fetch_rendered_video_sync

        target = episode_url or play
        if target and not str(target).lower().startswith(("http://", "https://")):
            target = self._abs_url(source, target)
        try:
            vurl = fetch_rendered_video_sync(
                target,
                wait_until=rc.get("wait_until") or "domcontentloaded",
                timeout_ms=int(rc.get("timeout_ms") or 45000),
                extra_delay_ms=int(rc.get("extra_delay_ms", 8000)),
                proxy=source.transports().get("proxy"),
                slow_fallback_rounds=int(rc.get("slow_fallback_rounds", 3)),
            )
        except Exception:  # noqa: BLE001 —— 渲染失败保留原候选，不阻断换线路
            return ""
        if vurl and self._is_playable_direct(vurl):
            return vurl
        return ""

    @staticmethod
    def _is_playable_direct(value: str) -> bool:
        """判断取流结果是否**可直接播放**（而不是解析器网页 / 不透明 id）。

        播放器只能吃媒体地址或解析器能展开的网页；`art.v2player.top/player/?url=`
        这类外壳页面本身不可播（VLC 会当普通网页打开而黑屏），源站 hash 更不行。
        """
        text = str(value or "").strip()
        if not text.lower().startswith(("http://", "https://")):
            return False
        # 解析器外壳页即使带 m3u8 参数也仍是一个网页
        if VideoMixin._looks_like_parser_shell(text):
            return False
        low = text.lower()
        if any(hint in low for hint in (".m3u8", ".mpd", ".mp4", ".flv", ".m4v", ".webm")):
            return True
        # CDN 直链常不带扩展名，用 query 标明格式
        # （如 TikTok CDN：.../o0V9uDVEkkFTBqAfDg7BtUqUNzNPwA2TEEfBWA/?mime_type=video_mp4）
        if "mime_type=video" in low or "video/mp4" in low or "video%2fmp4" in low:
            return True
        return False

    @staticmethod
    def _with_relay_candidates(play_cfg: dict, candidates: list) -> list:
        """按源声明的 `play_url.hls_relay` 给清单地址加一条中继候选，排在最前。

        现场（omofuna 实测）：正片分片 `c1.rrcdnbf6.com/.../0000000.ts` 直连
        200 可取，但**清单** `.../第22集/index.m3u8` 直连 404——站点播放器自己
        就是经 `https://m3u8xx.sgzm.net:2087/<原始URL>` 中继取的（playwright
        抓包可见）。拿到裸清单地址却没有中继，播放器就只能一直重试/卡住。

        配置：`"hls_relay": "https://m3u8xx.sgzm.net:2087/{url}"`（`{url}`
        为原始地址占位）。中继版排在最前，原地址保留作兜底——中继挂了还能直取。
        """
        relay = str((play_cfg or {}).get("hls_relay") or "").strip()
        if not relay or not candidates:
            return candidates
        out = []
        for value in candidates:
            text = str(value or "").strip()
            if (text.lower().startswith(("http://", "https://"))
                    and relay.split("{url}")[0].rstrip("/") not in text
                    and ".m3u8" in text.lower()):
                out.append(relay.replace("{url}", text))
            out.append(value)
        return out

    @staticmethod
    def _normalize_js_encoded_url(url: str) -> str:
        """把「JS escape + percent 混编」的地址还原成合法 URL。

        现场（omofuna 实测）：`player_aaaa.url` 解出来是
          `https://c1.rrcdnbf6.com/video/.../%u7B2C%32%32%u96C6/%69%6E%64%65%78%2E%6D%33%75%38`
        其中 `%u7B2C`/`%u96C6` 是 **JS escape**（= 第 / 集），其余是 percent。
        `%u` 在 URL 里非法，播放器拿去请求必然 404（分片目录其实是 UTF-8 的
        `%E7%AC%AC22%E9%9B%86`，实测正片分片 200）——用户看到的就是
        「清单拿到了但一直卡/跳转卡死」。

        顺序必须保持：先解 `%uXXXX`（JS），再 percent 解码，最后按 URL 安全
        字符集重新编码。反过来做会把 `%u` 的 `%` 当成 percent 头解坏。
        """
        import urllib.parse as _up

        text = str(url or "")
        if "%u" not in text and "\\u" not in text:
            return text

        def _js_unescape(value: str) -> str:
            def _sub(match):
                try:
                    return chr(int(match.group(1), 16))
                except (ValueError, OverflowError):
                    return match.group(0)

            value = _re.sub(r"%u([0-9a-fA-F]{4})", _sub, value)
            return _re.sub(r"\\u([0-9a-fA-F]{4})", _sub, value)

        decoded = _js_unescape(text)
        # 只对仍含 percent 转义的段做解码（纯中文/ASCII 部分原样保留）
        if "%" in decoded:
            try:
                decoded = _up.unquote(decoded, errors="strict")
            except Exception:  # noqa: BLE001 —— 解不动就保留 JS 解完的结果
                pass
        # 重新编码成合法 URL：保留 URL 结构字符，中文/空格等转成 UTF-8 percent
        try:
            return _up.quote(decoded, safe=":/?#[]@!$&'()*+,;=%~")
        except Exception:  # noqa: BLE001
            return decoded

    @staticmethod
    def _decode_player_target(value: str) -> str:
        """把被编码塞进外壳页/参数里的媒体直链解出来。

        MacCMS 派生站常见两种形态：
          - `player_aaaa.url` 就是逐字符 percent 编码的地址
            （`%68%74%74%70%73%3A%2F%2Fvip.example.com%2F...m3u8`）；
          - 地址再套一层解析站外壳 `https://解析站/player/?url=<编码值>`。
        外壳页是网页，播放器打不开（黑屏/空白窗口），所以先尝试解码：
        percent 解码（一次/两层）、纯 hex 解码；解出来的必须是 http(s) 地址。

        返回 "" 表示解不出可用地址（调用方保持原行为）。
        """
        import urllib.parse as _up

        text = str(value or "").strip()
        if not text:
            return ""
        raw_items = []
        match = _re.search(r"[?&](?:url|urls|v|vid|src)=([^&]+)", text)
        if match:
            raw_items.append(match.group(1))
        raw_items.append(text)

        seen = set()
        for raw in raw_items:
            if not raw or raw in seen:
                continue
            seen.add(raw)
            variants = []
            try:
                once = _up.unquote(raw)
                variants.extend((once, _up.unquote(once)))
            except Exception:  # noqa: BLE001 —— 解码失败就试下一种形态
                pass
            stripped = raw.strip()
            if len(stripped) >= 16 and len(stripped) % 2 == 0 and _re.fullmatch(
                    r"[0-9a-fA-F]+", stripped):
                try:
                    variants.append(bytes.fromhex(stripped).decode("utf-8", "ignore"))
                except Exception:  # noqa: BLE001
                    pass
            for candidate in variants:
                candidate = candidate.strip()
                if candidate.lower().startswith(("http://", "https://")):
                    return candidate
        return ""

    @staticmethod
    def _wrapper_is_signed(value: str) -> bool:
        """包裹地址是否带站点签名参数（带签名的必须整串用，不能拆成内层裸链）。

        18mh 实测：`/media/m3u8?url=<裸 m3u8>&exp=…&token=…` 一旦丢掉签名参数
        直接请求内层 CDN 一律 403，所以这种包装地址要原样保留、优先级不能动。
        """
        low = str(value or "").lower()
        return any(hint in low for hint in (
            "exp=", "expire=", "token=", "auth_key=", "sign=", "signature=",
            "sig=", "md5=", "deadline=", "wssecret=", "wsse=",
        ))

    @staticmethod
    def _looks_like_parser_shell(url: str) -> bool:
        """是否是「解析器外壳页」（形如 .../player/?url=<id>），而非媒体直链。"""
        low = str(url or "").lower()
        if "/player/" not in low and "/player?" not in low:
            return False
        # 外壳页通常把目标作为 url= 参数传入
        return "url=" in low

    def _fetch_play_url_rendered(
        self, source: SourceConfig, episode_url: str, abs_ep: str,
        block: dict, play_cfg: dict, deadline=None, context=None,
    ) -> str:
        """Playwright 渲染播放页 → iframe 解析站 → 真实 video src。"""
        rc = block.get("render_config") or {}
        from framework.playwright_helper import fetch_rendered_video_sync

        timeout_ms = int(rc.get("timeout_ms") or 45000)
        extra_delay_ms = int(rc.get("extra_delay_ms", 8000))
        render_url = abs_ep
        iframe_selector = play_cfg.get("iframe_selector")
        if iframe_selector:
            html = self._get_play_page_html(source, episode_url, abs_ep,
                                            deadline=deadline, context=context)
            doc = self._parser.parse(html)
            iframe_url = self._parser.extract_first(doc, iframe_selector, abs_ep)
            if not iframe_url or not iframe_url.startswith(("https://", "http://")):
                raise ContentMissingError("未获取到播放 iframe", source_id=source.source_id)
            render_url = iframe_url
        # 已知失效线路（play_url.render_skip_from 命中 player_aaaa.from）：
        # 渲染也是白等（omofuna 精品线路实测 47s 仍然拿不到），直接判失败
        # 换线路，别让一条坏线路把整次取流拖死。
        if self._render_skipped(
                source, play_cfg, block.get("source_switch") or {},
                self._get_play_page_html(source, episode_url, abs_ep,
                                         deadline=deadline, context=context)):
            raise ContentMissingError(
                f"该线路解析器不可用，已跳过（{episode_url}）",
                source_id=source.source_id,
            )
        # 渲染结果短期缓存：同一集重播/来回切集/预取撞点集时，渲染要花
        # 8~15s（浏览器启动 + iframe 解析站），没必要重来一遍。只缓存成功
        # 的非空结果；TTL 取源级 render_config.cache_ttl_s（缺省 600s）。
        cached = self._rendered_play_cache_get(source, abs_ep)
        if cached:
            return cached
        recovery_options = {}
        is_agedm = source.source_id == "agedm"
        if is_agedm and rc.get("recover_navigation_timeout") is True:
            recovery_options["recover_navigation_timeout"] = True
        # context 传进渲染会让 playwright 按「剩余预算」缩短等待：慢站播放页
        # 光是 iframe 解析站加载就 8~12s，剩余预算常被前面的 HTTP 解析吃掉，
        # 于是渲染必然超时（实测带 context 68.9s 失败、不带 3.5s 成功）。
        # 渲染自己的 timeout_ms 已是明确上限，这里不再用外层 context 压缩它。
        vurl = fetch_rendered_video_sync(
            render_url,
            **recovery_options,
            wait_until=rc.get("wait_until") or "networkidle",
            timeout_ms=timeout_ms,
            extra_delay_ms=extra_delay_ms,
            proxy=source.transports().get("proxy"),
            referer=abs_ep if is_agedm else "",
            capture_media_responses=is_agedm,
            slow_fallback_rounds=int(rc.get("slow_fallback_rounds", 3)),
        )
        self._check_stream_context(context, deadline)
        if not vurl:
            raise ContentMissingError(
                f"渲染未获取到视频源（{episode_url}）", source_id=source.source_id
            )
        self._rendered_play_cache_set(source, abs_ep, vurl, rc)
        return vurl

    # 渲染结果缓存（{(source_id, abs_ep): (expire_monotonic, url)}）。
    _RENDERED_CACHE_MAX = 24

    def _rendered_play_cache_get(self, source: SourceConfig, abs_ep: str) -> str:
        cache = getattr(self, "_rendered_play_cache", None)
        if not cache:
            return ""
        key = (source.source_id, abs_ep)
        hit = cache.get(key)
        if not hit:
            return ""
        if hit[0] <= time.monotonic():
            cache.pop(key, None)
            return ""
        return hit[1]

    def _rendered_play_cache_set(self, source: SourceConfig, abs_ep: str,
                                 url: str, rc: dict) -> None:
        if not url:
            return
        # 带时效签名的地址缓存太久会在播放时过期：按 URL 里的 expire/expires
        # 推算剩余时长，剩余不足 30s 就不缓存（下次现场渲染）。
        try:
            ttl = float(rc.get("cache_ttl_s") or 600.0)
        except (TypeError, ValueError):
            ttl = 600.0
        remaining = _signed_url_remaining_ttl((url,))
        if remaining > 0:
            if remaining < 30.0:
                return
            ttl = min(ttl, remaining)
        if ttl <= 0:
            return
        cache = getattr(self, "_rendered_play_cache", None)
        if cache is None:
            cache = self._rendered_play_cache = {}
        cache[(source.source_id, abs_ep)] = (time.monotonic() + ttl, url)
        while len(cache) > int(self._RENDERED_CACHE_MAX):
            cache.pop(next(iter(cache)))

    def _fetch_play_url_http_once(
        self, source: SourceConfig, episode_url: str, abs_ep: str,
        block: dict, play_cfg: dict, deadline=None, context=None,
    ) -> str:
        """纯 HTTP 解析播放页拿播放地址（render fallback 的前半段）。

        复用主流程：_extract_play_url_candidates 逐个候选交给 _resolve 评判。
        失败返回 ""，由调用方决定是否转渲染。
        """
        try:
            # 走播放页缓存：这条路与 _fetch_play_url_candidates_once / 转渲染
            # 兜底抓的是**同一个页面**，不共用缓存就会白白多抓一遍
            # （omofuna 后半段集数 url 为空，每集因此多花 1~3s）。
            html = self._get_play_page_html(source, episode_url, abs_ep,
                                            deadline=deadline, context=context)
        except Exception:  # noqa: BLE001
            return ""
        try:
            candidates = self._extract_play_url_candidates(
                html, source, (block.get("source_switch") or {}),
                play_cfg, abs_ep, episode_url,
                deadline=deadline, context=context,
            )
        except Exception:  # noqa: BLE001
            return ""
        for value in candidates:
            text = str(value or "").strip()
            if not text.lower().startswith(("http://", "https://")):
                continue
            if self._is_playable_direct(text):
                return text
        return ""

    def _fetch_18av_player_stream(
        self, source: SourceConfig, episode_url: str, deadline=None, context=None
    ) -> str:
        """解密 18AV 的受保护播放器并返回真实 HLS 主流。

        18AV 的静态 HTML 不包含 mvarr，content_mv_protect.js 在浏览器运行时
        才把加密参数写入页面。参数按页面当前 radix/分隔符/XOR 还原后，再按
        页面动态 AES-CBC-PKCS7 密钥解出 play.php 的 id，最后从 play.php 的
        videoSources 提取 m3u8。此分支严格按 source_id 隔离，避免影响其他源。
        """
        context = self._stream_context(context, deadline)
        self._check_stream_context(context, deadline)
        from framework.playwright_helper import fetch_rendered_html_sync

        abs_ep = self._abs_url(source, episode_url)
        headers = source.request_headers()
        render_kwargs = {
            "timeout_ms": min(45000, int(self._timeout(source) * 1000)),
            "proxy": source.transports().get("proxy"),
            "referer": source.transports().get("headers", {}).get("Referer", ""),
            "headers": headers,
        }
        try:
            # 18AV 只需要播放器脚本生成 mvarr；等到该状态出现即可，
            # 不必等待页面所有资源完成。
            rendered = fetch_rendered_html_sync(
                abs_ep,
                wait_until="commit",
                wait_for_function=(
                    "() => window.mvarr && window.mvarr['10_1'] "
                    "&& window.mvarr['10_1'].length > 0"
                ),
                extra_delay_ms=50,
                **render_kwargs,
            )
        except Exception:
            rendered = ""
        if not rendered or ("mvarr" not in rendered and "play.php" not in rendered):
            # 页面偶发连接重置或脚本加载慢时保留兼容回退，避免快速路径
            # 把瞬时网络波动变成“无播放地址”。
            rendered = fetch_rendered_html_sync(
                abs_ep,
                wait_until="domcontentloaded",
                extra_delay_ms=250,
                **render_kwargs,
            )
        self._check_stream_context(context, deadline)
        if not rendered:
            raise ContentMissingError("18AV 浏览器渲染详情页为空", source_id="18av")

        import base64 as _base64
        import html as _html
        from urllib.parse import urljoin as _urljoin

        def _fetch_player_stream(player_url):
            timeout = self._timeout(source)
            if context is not None:
                timeout = min(timeout, context.remaining())
            player_html = self._http.get_text(
                player_url,
                headers={**headers, "Referer": abs_ep},
                timeout=timeout,
                retries=0 if context is not None else self._retries(source),
                interval_ms=0 if context is not None else self._interval_ms(source),
                encoding=source.transports().get("charset"),
                proxy_pool=source.proxy_pool(),
            )
            self._check_stream_context(context, deadline)
            streams = _re.findall(
                r"(?:videoSources|sources)\s*=\s*\[.*?['\"]?src['\"]?\s*:\s*"
                r"['\"](https?://[^'\"]+\.(?:m3u8|mp4)(?:\?[^'\"]*)?)",
                player_html,
                _re.IGNORECASE | _re.DOTALL,
            )
            if not streams:
                streams = _re.findall(
                    r"['\"](https?://[^'\"]+\.(?:m3u8|mp4)(?:\?[^'\"]*)?)['\"]",
                    player_html,
                    _re.IGNORECASE,
                )
            stream = next((url for url in streams if ".m3u8" in url.lower()), "")
            if not stream:
                raise ContentMissingError(
                    "18AV play.php 未返回 HLS 主流", source_id="18av"
                )
            return stream

        def _js_scalar(pattern, default=None):
            match = _re.search(pattern, rendered, _re.IGNORECASE)
            return match.group(1) if match else default

        # These values are intentionally read from the rendered detail page.
        # 18AV rotates them and also derives the payload separator from the
        # radix; hard-coding either value eventually produces a false
        # "stream not found" result.
        crypto_key = _js_scalar(
            r"\b(?:argdeqweqweqwe|hdddedd252)\s*=\s*['\"]([0-9a-f]{16})['\"]"
        )
        crypto_iv = _js_scalar(
            r"\b(?:argdeqweqweqww|hdddedg252)\s*=\s*['\"]([0-9a-f]{16})['\"]"
        )
        configured_xor = _js_scalar(r"\bhadeedg252\s*=\s*(\d+)")
        configured_radix = _js_scalar(r"\bhcdeedg252\s*=\s*(\d+)")
        try:
            configured_xor = int(configured_xor) if configured_xor is not None else None
            configured_radix = (
                int(configured_radix) if configured_radix is not None else None
            )
        except ValueError:
            configured_xor = configured_radix = None

        def _read_js_string(text: str, pos: int):
            """读取一个 JS 引号字符串，保留转义序列供后续解码。"""
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if pos >= len(text) or text[pos] not in "'\"":
                return None, pos
            quote = text[pos]
            pos += 1
            chars = []
            while pos < len(text):
                char = text[pos]
                if char == "\\" and pos + 1 < len(text):
                    chars.extend((char, text[pos + 1]))
                    pos += 2
                    continue
                if char == quote:
                    return "".join(chars), pos + 1
                chars.append(char)
                pos += 1
            return None, pos

        # 某些页面变体会在脚本报错前直接留下已生成的播放器 iframe，
        # 此时没有可读的 mvarr，但 iframe 的 play.php id 仍可正常取流。
        direct_player = _re.search(
            r"(?:src|data-src)\s*=\s*['\"]((?:https?:)?//[^'\"]*/js/player/play\.php\?[^'\"]*\bid=[^'\"]+)['\"]",
            rendered,
            _re.IGNORECASE,
        )
        if direct_player:
            player_url = _html.unescape(direct_player.group(1)).replace("\\/", "/")
            if player_url.startswith("//"):
                player_url = "https:" + player_url
            return _fetch_player_stream(_urljoin(abs_ep, player_url))

        marker = _re.search(
            r"mvarr\s*\[\s*['\"]10_1['\"]\s*\]\s*=\s*\[\s*\[",
            rendered,
            _re.IGNORECASE,
        )
        fields = []
        if marker:
            pos = marker.end()
            for index in range(6):
                value, pos = _read_js_string(rendered, pos)
                if value is None:
                    fields = []
                    break
                fields.append(value)
                while pos < len(rendered) and rendered[pos].isspace():
                    pos += 1
                if index < 5:
                    if pos >= len(rendered) or rendered[pos] != ",":
                        fields = []
                        break
                    pos += 1
        if len(fields) < 6:
            raise ContentMissingError(
                "18AV 浏览器页面未生成 mvarr 主播放器参数", source_id="18av"
            )

        def _js_string(value: str) -> str:
            def _replace(match):
                token = match.group(1)
                if token.startswith("u") and len(token) == 5:
                    try:
                        return chr(int(token[1:], 16))
                    except ValueError:
                        return token
                if token.startswith("x") and len(token) == 3:
                    try:
                        return chr(int(token[1:], 16))
                    except ValueError:
                        return token
                return {
                    "b": "\\b", "f": "\\f", "n": "\\n", "r": "\\r",
                    "t": "\\t", "v": "\\v", "0": "\\0",
                }.get(token, token)

            return _re.sub(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", _replace, value)

        # mvarr item: [iframe_id, encrypted, iframe_attrs, player_prefix,
        #              player_suffix, iframe_html_suffix]. The sixth field is
        #              only HTML appended around the iframe, never part of URL.
        encrypted = _js_string(fields[1])
        prefix = _js_string(fields[3])
        suffix = _js_string(fields[4])
        if not encrypted or not prefix:
            raise ContentMissingError("18AV 主播放器参数不完整", source_id="18av")

        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.primitives import padding

            player_id = ""
            # 站点脚本会随版本改变分隔符和 radix。分隔符实际是
            # String.fromCharCode(radix + 97)，例如 radix=21 -> "v"。
            configs = []
            if crypto_key and crypto_iv:
                configs.append((crypto_key.encode(), crypto_iv.encode()))
            configs.append((b"0e4de3a7c8020b9d", b"f86d5f74468c3ab5"))
            unique_configs = []
            for item in configs:
                if item not in unique_configs:
                    unique_configs.append(item)
            configs = unique_configs
            radices = [configured_radix] if configured_radix else []
            radices.extend(range(2, 37))
            radices = list(dict.fromkeys(radices))
            xor_keys = [configured_xor] if configured_xor is not None else []
            xor_keys.extend(range(32))
            xor_keys = list(dict.fromkeys(xor_keys))
            delimiters = []
            for radix in radices:
                if 2 <= radix <= 25:
                    delimiters.append(chr(radix + 97))
            delimiters.extend(("n", "r"))
            delimiters.extend(
                char for char in set(encrypted) if not char.isalnum() and char not in delimiters
            )
            delimiters = list(dict.fromkeys(delimiters))
            for delimiter in delimiters:
                parts = encrypted.split(delimiter)
                if len(parts) < 2 or any(not part for part in parts):
                    continue
                for radix in radices:
                    try:
                        values = [int(part, radix) for part in parts]
                    except ValueError:
                        continue
                    if any(value > 255 for value in values):
                        continue
                    for xor_key in xor_keys:
                        try:
                            decoded = bytes(value ^ xor_key for value in values)
                            for key, iv in configs:
                                cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
                                decryptor = cipher.decryptor()
                                padded = decryptor.update(
                                    _base64.b64decode(decoded, validate=True)
                                )
                                padded += decryptor.finalize()
                                unpadder = padding.PKCS7(128).unpadder()
                                candidate = unpadder.update(padded) + unpadder.finalize()
                                player_id = candidate.decode("utf-8")
                                if player_id:
                                    break
                        except Exception:  # noqa: BLE001
                            continue
                        if player_id:
                            break
                    if player_id:
                        break
                if player_id:
                    break
        except Exception as exc:  # noqa: BLE001
            raise ContentMissingError(
                f"18AV 主播放器 AES 解密失败: {exc}", source_id="18av"
            ) from exc
        if not player_id:
            raise ContentMissingError("18AV 主播放器解密结果为空", source_id="18av")

        player_url = _html.unescape(
            _urljoin(abs_ep, prefix + player_id + suffix)
        )
        return _fetch_player_stream(player_url)

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
            elif decode == "url_unquote":
                # 纯 URL 编码解码（MacCMS encrypt=1：player.url 是整串 percent-encoded，
                # 解码后才是解析器外壳/直链）。
                import urllib.parse as _up

                val = _up.unquote(val)
            elif decode == "base64_shift_json":
                import base64 as _b64

                shift = step.get("shift")
                shift_regex = step.get("shift_regex")
                if shift is None and shift_regex:
                    shift_match = _re.search(
                        str(shift_regex), cur_text, _re.IGNORECASE | _re.DOTALL
                    )
                    if shift_match:
                        shift = shift_match.group(1) if shift_match.groups() else shift_match.group(0)
                try:
                    shift = int(shift)
                    encoded = str(val).replace("-", "+").replace("_", "/")
                    raw = _b64.b64decode(encoded + "=" * (-len(encoded) % 4))
                    decoded = bytes((byte - shift) & 0xFF for byte in raw).decode("utf-8")
                    # Parsing here catches a stale/partial SSR payload before the
                    # next chain step turns it into a misleading relative URL.
                    val = json.dumps(json.loads(decoded), ensure_ascii=False, separators=(",", ":"))
                except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
                    raise ContentMissingError(
                        f"http_chain 第{i}步 Base64 移位 JSON 解码失败: {exc}",
                        source_id=source.source_id,
                    ) from exc

            rewrite_rules = step.get("rewrite") or []
            if rewrite_rules:
                rewritten = val
                matched = False
                for rule in rewrite_rules:
                    if not isinstance(rule, dict) or not rule.get("regex"):
                        continue
                    match = _re.search(str(rule["regex"]), str(rewritten), _re.IGNORECASE)
                    if not match:
                        continue
                    replacement = str(rule.get("replacement") or "")
                    for group in range(1, len(match.groups()) + 1):
                        replacement = replacement.replace(
                            "{%d}" % group, match.group(group) or ""
                        )
                    rewritten = _re.sub(
                        str(rule["regex"]), replacement, str(rewritten), count=1,
                        flags=_re.IGNORECASE,
                    )
                    matched = True
                    break
                if step.get("require_rewrite") and not matched:
                    raise ContentMissingError(
                        f"http_chain 第{i}步 URL 重写未匹配", source_id=source.source_id
                    )
                val = rewritten

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

    def _resolve_external_stream(self, source: SourceConfig, play_cfg: dict,
                                 player: dict, abs_ep: str,
                                 deadline=None, context=None) -> str:
        """第三方播放器线路 → 真实直链（play_url.external_player 配置）。

        未配置、from 不匹配、缺 Node、解密失败都返回空串：调用方按原逻辑
        继续（保留不透明 id，让换线/渲染兜底），不影响其它线路。

        预算感知（2026-11 omofuna 现场）：解析器线路的 `budget_s` 是**上限**，
        不是每次都必须跑满。整次取流只剩几秒时再启动解析，等于把换线机会
        一起烧掉——现场表现是「取流 25s 后报超时」，而同集直链线路只要 1s。
        因此这里按剩余总预算收窄预算，剩余不足 `_EXTERNAL_MIN_BUDGET_S` 直接
        放弃（返回空串 → 上层立刻换线）。
        """
        external = play_cfg.get("external_player") or {}
        if not external:
            return ""
        budget = 0.0
        try:
            budget = float(external.get("budget_s") or 0)
        except (TypeError, ValueError):
            budget = 0.0
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining < self._EXTERNAL_MIN_BUDGET_S:
                log.info(
                    "[%s] 跳过第三方解析器：剩余预算 %.1fs < %.1fs（%s）",
                    source.source_id, max(0.0, remaining),
                    self._EXTERNAL_MIN_BUDGET_S, self._safe_url(abs_ep),
                )
                return ""
            if budget > 0:
                external = dict(external)
                # 留 1s 给解析成功后的收尾（拼地址/写缓存/emit），别把预算全
                # 用在解析站页面上。
                external["budget_s"] = min(budget, max(2.0, remaining - 1.0))
        try:
            from framework.external_player import resolve_external_player

            return resolve_external_player(self._http, external, player, abs_ep) or ""
        except StreamCancelledError:
            raise
        except TimeoutError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 解析失败只降级，不打断取流
            log.info("[%s] 第三方播放器解析失败：%s", source.source_id, str(exc)[:140])
            return ""

    # 启动第三方解析器至少要有的剩余预算（秒）：低于此值直接换线。
    # 实测精品线解析成功约 1s，但解析站抽风时必然吃满预算，不值得赌。
    _EXTERNAL_MIN_BUDGET_S = 3.0

    # 换线至少要有的剩余预算（秒）：一次详情页往返正常 1~2s。低于此值就收尾，
    # 别把用户的时间花在一条注定跑不完的换线请求上。
    _SWITCH_MIN_BUDGET_S = 1.5

    def _extract_play_url_candidates(
        self, html: str, source: SourceConfig, switch_cfg: dict,
        play_cfg: dict, abs_ep: str, episode_url: str,
        deadline=None, context=None,
    ) -> list[str]:
        import html as _html

        values: list[str] = []
        seen: set[str] = set()

        def add(value: str) -> None:
            value = _html.unescape(str(value or "")).strip()
            value = value.replace("\\\\/", "/").replace("\\/", "/")
            value = value.replace('\\"', '"')
            # JS escape（%uXXXX）混编的地址还原成合法 URL（omofuna 实测：
            # `%u7B2C22%u96C6` 是 UTF-8 的 `%E7%AC%AC22%E9%9B%86`，不解就 404）。
            value = VideoMixin._normalize_js_encoded_url(value)
            if not value or value.startswith(("javascript:", "data:")):
                return
            # 外壳页/参数里被编码塞进去的真实媒体直链：先解出来排在前面。
            # mpv/VLC 打不开 `解析站/player/?url=<编码值>` 这种网页（只会得到空白
            # 窗口），而同一串里往往就有真正的 m3u8（omofuna 实测：player_aaaa.url
            # 是逐字符 percent 编码的 vip.dytt-hot.com/.../index.m3u8）。
            # 但带签名的包装（/media/m3u8?url=…&exp=…&token=…）必须整串用：拆出
            # 内层裸链会 403（18mh 实测），所以那种情况保持原顺序。
            inner = VideoMixin._decode_player_target(value)
            if (inner and inner not in seen and VideoMixin._is_playable_direct(inner)
                    and not VideoMixin._wrapper_is_signed(value)):
                seen.add(inner)
                values.append(inner)
            if not value.startswith(("http://", "https://", "//")):
                # 不透明 id（如 MacCMS 外部解析器的 32 位 hex）不能 urljoin：那会
                # 拼出 `play/1/<hash>` 这种不存在的地址，播放器直接黑屏
                # （omofuna 精品源实测）。只有形如相对路径时才拼接。
                looks_like_path = (
                    "/" in value
                    or _re.search(r"\.(?:html?|m3u8|mp4|ts|json|php)(?:\?|$)", value, _re.I)
                )
                if not looks_like_path:
                    # 交给源配置的默认解析器（若有），否则原样保留供调用方判断
                    parse_tpl = str(switch_cfg.get("default_parse") or "").replace("\\/", "/")
                    if parse_tpl:
                        sep = "&" if "?" in parse_tpl else "?"
                        value = f"{parse_tpl}{sep}url={value}"
                    if not value.startswith(("http://", "https://", "//")):
                        return
                else:
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
                # 第三方播放器线路（omofuna 精品：player_aaaa 只给 32 位不透明 id，
                # 真实直链在播放器页面里由 playData() 解密）→ 直接解出直链，
                # 免得把 id 当成播放地址交给播放器（那样只会黑屏/被换线）。
                resolved = self._resolve_external_stream(
                    source, play_cfg, player, abs_ep,
                    deadline=deadline, context=context,
                )
                if resolved:
                    add(resolved)
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
        # 签名包装地址（/media/m3u8?url=…&exp=…&token=…）必须整串带签名取用：
        # 上面 .m3u8(?:\?…)? 的通用候选只会截到 url= 里的裸 m3u8，丢掉 exp/token
        # 后站点侧代理直接 403（18mh 视频实测）。这里先把完整的带参地址收进来，
        # 保证候选表里「带签名」的排在裸地址之前。
        play_host = ""
        try:
            play_host = (urlsplit(abs_ep).hostname or "").lower()
        except Exception:  # noqa: BLE001
            play_host = ""

        def wrapped_url(value: str) -> str:
            """把 /media/m3u8?url=… 这类包装地址落到**站点自己的**代理上。

            站点播放器用的是相对路径（\\/media\\/m3u8?url=…），按页面 host 补全；
            页面里另有 CDN 域的绝对包装地址（xv.dzuxta.cn/media/m3u8），
            实测该条 403/挂起，不能当播放地址用——同路径一律归到站点域。

            页面里同时存在两种形态：干净的 `…&token=…` 与拼坏的
            `…&token=…?auth_key=…&via_m=…`（两个查询串硬接，第二个 `?` 后
            整段成了 token 值的一部分）。后者实测 403，这里把接错的第二段
            查出来归并成合法查询串。
            """
            if not value.startswith(("http://", "https://", "//")):
                value = urljoin(abs_ep, value)
            if value.startswith("//"):
                value = "https:" + value
            try:
                parts = urlsplit(value)
            except Exception:  # noqa: BLE001
                return value
            query = parts.query
            # 接错的第二段：token 值里出现 "?k=v" → 把问号起的内容并成独立参数
            query = _re.sub(r"\?([A-Za-z0-9_]+)=", r"&\1=", query)
            is_wrapper = parts.path.endswith("/media/m3u8")
            if (
                play_host
                and is_wrapper
                and (parts.hostname or "").lower() != play_host
            ):
                return f"https://{play_host}{parts.path}" + (f"?{query}" if query else "")
            if query != parts.query:
                return parts._replace(query=query).geturl()
            return value

        for match in _re.finditer(
            r'((?:https?://[^"\'\s<>]+)?/media/m3u8\?url=[^"\'\s<>]+)', normalized, _re.I
        ):
            add(wrapped_url(match.group(1)))

        def add_bare(value: str) -> None:
            # 裸地址若本身就是 CDN 域包装（…/media/m3u8?url=…），同样归到站点域
            if "/media/m3u8" in value:
                add(wrapped_url(value))
            else:
                add(value)

        for match in _re.finditer(
            r'(https?%3A%2F%2F[^"\'\s<>]+?\.m3u8[^"\'\s<>]*)'
            r'(?:&(?:exp|token|auth_key|expires|signature|sig)=[^"\'\s<>&]+)+',
            normalized, _re.I,
        ):
            from urllib.parse import unquote as _unquote

            add(wrapped_url(_unquote(match.group(0))))
        for match in _re.finditer(
            r'((?:https?://|//)[^"\'\s<>]+?\.m3u8(?:\?[^"\'\s<>]*)?)',
            normalized,
            _re.I,
        ):
            value = match.group(1)
            # 该分支会把同一条串里 url= 后面的裸 m3u8 也抓出来（后面还跟着
            # &exp=&token=），属于签名丢失的重复项，交给上面的包装分支处理
            if "/media/m3u8" in value:
                add_bare(value)
            else:
                add(value)
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
            html = self._get_play_page_html(source, episode_url, abs_ep,
                                           deadline=deadline, context=context)
        self._check_stream_context(context, deadline)
        candidates = self._extract_play_url_candidates(
            html, source, switch_cfg, play_cfg, abs_ep, episode_url,
            deadline=deadline, context=context,
        )
        return self._with_relay_candidates(play_cfg, candidates)

    # 播放页 HTML 短 TTL 缓存：omofuna 实测单页 TTFB 0.4~2.0s（站点本身就慢），
    # 用户来回切同一集/预取与点集撞同一集时没必要再抓一次。只缓存最近几页，
    # TTL 内复用；取流结果本身另有 stream_resolver 缓存，这里只省 HTML。
    _PLAY_PAGE_TTL = 120.0
    _PLAY_PAGE_MAX = 6

    def _get_play_page_html(self, source: SourceConfig, episode_url: str,
                            abs_ep: str, deadline=None, context=None) -> str:
        """取播放页 HTML（带短 TTL 缓存 + 同 URL 并发合并）。"""
        cache = getattr(self, "_play_page_cache", None)
        if cache is None:
            cache = self._play_page_cache = {}
        now = time.monotonic()
        hit = cache.get((source.source_id, abs_ep))
        if hit and hit[0] > now:
            return hit[1]
        lock = getattr(self, "_play_page_lock", None)
        if lock is None:
            lock = self._play_page_lock = threading.Lock()
        # 同一 URL 的并发取流（点集 + 提前预取）只发一次请求：抓取在锁内做，
        # 后到的线程等前者完成直接吃缓存。不同 URL 不互相阻塞。
        with lock:
            hit = cache.get((source.source_id, abs_ep))
            if hit and hit[0] > time.monotonic():
                return hit[1]
            html = self._get(source, episode_url, deadline=deadline, context=context)
            cache[(source.source_id, abs_ep)] = (
                time.monotonic() + float(self._PLAY_PAGE_TTL), html)
            while len(cache) > int(self._PLAY_PAGE_MAX):
                cache.pop(next(iter(cache)))
            return html

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
    def _stream_remaining(context, deadline=None):
        """剩余预算（秒）；无法判断返回 None。

        deadline（绝对单调钟点）与 context.remaining() 都可能存在，取更小的
        那个 —— 只认 deadline 会漏掉「按 StreamBudget 计时」的调用方（换线
        阶段就是这样），于是预算早就用光了还去抓下一条线路。
        """
        remaining = None
        if deadline is not None:
            remaining = deadline - time.monotonic()
        if context is not None:
            try:
                ctx_left = float(context.remaining())
            except Exception:  # noqa: BLE001 —— 上下文不提供剩余时间就只看 deadline
                ctx_left = None
            if ctx_left is not None:
                remaining = ctx_left if remaining is None else min(remaining, ctx_left)
        return remaining

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
        # 需要渲染才能展开的解析器外壳页候选（见下方「挂起」说明）：等所有线路
        # 都试完、仍然没拿到可直接播放的地址时，再回头渲染它们。
        deferred: list = []
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
                    # 候选可达性逐个探测（顺序语义：取消后不再探测下一个）。
                    usable_flags = self._probe_candidates_parallel(
                        source, plays, current, deadline=deadline, context=context
                    )
                    for play, usable in zip(plays, usable_flags):
                        self._check_stream_context(context, deadline)
                        if not usable:
                            continue
                        # 探测只证明「能访问」，不证明「能播」：解析器外壳页
                        # （.../player/?url=<id>）同样 200，播放器打开只会黑屏。
                        #
                        # 外壳页要展开成真地址得靠 playwright 渲染（数秒到数十秒）。
                        # 实测 omofuna：sid=3 无片源 → 换线 → sid=1 是「精品」线
                        # （只有不透明 hash，必须渲染）→ sid=2 是干净 m3u8（0.7s）。
                        # 先渲染 sid=1 会把用户卡十几秒，而 sid=2 本来就在后面。
                        # 所以外壳页先挂起，继续试后面的线路；全都拿不到真直链时
                        # 再回头渲染挂起的那条（保留原有兜底能力，只是排到最后）。
                        if self._looks_like_parser_shell(play):
                            deferred.append(play)
                            continue
                        rendered = self._render_if_parser_shell(
                            source, play, context, deadline,
                            episode_url=current,
                        )
                        return rendered or play
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
                # 先走零请求的坐标替换（omofuna 这类 URL 自带线路坐标的站），
                # 拿不到再退回「逐线路抓详情页」的老路（实测 11.7s）。
                try:
                    candidates = self._call_with_context(
                        self._switch_candidate_episode_urls_fast,
                        (source, current), context, deadline
                    )
                except StreamCancelledError:
                    raise
                except Exception:  # noqa: BLE001 —— 快路径只是优化
                    candidates = []
            if candidates:
                log.info(
                    "[%s] 阶段=换线，快路径候选=%d", source.source_id, len(candidates)
                )
            if not candidates:
                try:
                    # 预算闸门：换线 = 又一次详情页往返（正常 1~2s，站点抽风
                    # 8s 读超时）。剩余预算不够就不再开新战场，把"取流超时"
                    # 提前成明确失败，而不是让用户干等到总预算耗尽。
                    # 现场对照：omofuna 首条线路（精品解析器线）白烧 9~16s 后
                    # 再换线，用户看到的就是「取流很久然后超时」。
                    remaining = self._stream_remaining(context, deadline)
                    if remaining is not None and remaining <= self._SWITCH_MIN_BUDGET_S:
                        log.info(
                            "[%s] 阶段=换线，剩余预算 %.1fs < %.1fs，停止换线",
                            source.source_id, max(0.0, remaining),
                            self._SWITCH_MIN_BUDGET_S,
                        )
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
        # 所有线路都没给出可直接播放的地址：回头把挂起的解析器外壳页渲染展开
        # （最慢的一条路，但总比直接报错强）。
        for shell in deferred:
            self._check_stream_context(context, deadline)
            rendered = self._render_if_parser_shell(
                source, shell, context, deadline, episode_url=episode_url,
            )
            if rendered:
                return rendered
        if deferred:
            last_err = ContentMissingError(
                f"播放器外壳页展开失败（已尝试 {len(deferred)} 个）",
                source_id=source.source_id,
            )
        if last_err is None:
            last_err = ContentMissingError("未取到播放地址", source_id=source.source_id)
        # 把最后一条真实原因带出来：界面只显示顶层消息，而"全部线路取流失败"
        # 本身不说明任何事（是解析站连不上？播放页结构变了？还是超时？）。
        raise ContentMissingError(
            f"全部线路取流失败（{source.source_id}）：{str(last_err)[:140]}",
            source_id=source.source_id,
        ) from last_err

    def _probe_candidates_parallel(
        self, source: SourceConfig, plays: list, episode_url: str,
        deadline=None, context=None, max_workers: int = 4,
    ) -> list:
        """按顺序探测候选地址，返回与 plays 对齐的可用性布尔列表。

        顺序语义必须保留：context 取消（用户停止/预算耗尽）后不得再探测下一个
        候选，否则会多发请求并掩盖取消。命中第一个可用候选即短路返回。

        源可声明 content.episode.play_url.probe = {"mode": "skip"} 关闭探测——
        适用于「直连必然 403、只有本地代理能播」的 CDN（omofuna 实测：直连
        恒定 403，同一地址经代理 200）。这种源上探测纯属白等 3s 再判死，
        还会把好线路写进死线缓存。
        """
        if self._probe_disabled(source):
            return [True] * len(plays)
        results: list = []
        for url in plays:
            if context is not None:
                self._check_stream_context(context, deadline)
            ok = self._play_url_probe_for_episode(
                source, url, episode_url, deadline=deadline, context=context
            )
            results.append(bool(ok))
            if ok:
                results.extend([False] * (len(plays) - len(results)))
                return results
            if context is not None:
                context.check()
        return results

    @staticmethod
    def _probe_disabled(source: SourceConfig) -> bool:
        """源是否声明「跳过播放地址探测」（content.episode.play_url.probe.mode）。"""
        try:
            block = ((source.raw or {}).get("endpoints") or {}).get("content") or {}
            episode = block.get("episode") or {}
            probe = (episode.get("play_url") or {}).get("probe") or {}
            return str(probe.get("mode") or "").lower() == "skip"
        except Exception:  # noqa: BLE001 —— 配置异常按「不跳过」处理
            return False

    def _play_url_probe_for_episode(
        self, source: SourceConfig, url: str, episode_url: str,
        deadline=None, context=None,
    ) -> bool:
        """按「播放页」Referer 探测候选地址（防盗链 CDN 可能只认播放页）。

        把当前播放页作为 Referer 传给 `_play_url_probe`（源级 headers 不变，
        只对本次探测生效）；实现签名不同时自动降级。
        """
        original = getattr(self, "_headers", None)
        abs_ep = self._abs_url(source, episode_url) if episode_url else ""

        def _headers_with_episode(src):
            headers = dict(original(src) if original else {})
            if abs_ep:
                headers["X-Episode-Referer"] = abs_ep
            return headers

        try:
            self._headers = _headers_with_episode
            # 只按「实现签名」挑调用形式，不靠异常猜：子类/测试可能用
            # `_play_url_probe(source, url)` 或 `(source, url, context=None)` 覆盖，
            # 传 deadline 会抛 TypeError 并被误判成「探测失败」。
            try:
                params = inspect.signature(self._play_url_probe).parameters
                accepts = set(params.keys())
                call_kwargs = {}
                if "deadline" in accepts:
                    call_kwargs["deadline"] = deadline
                if "context" in accepts:
                    call_kwargs["context"] = context
                return self._play_url_probe(source, url, **call_kwargs)
            except (TypeError, ValueError):
                return self._play_url_probe(source, url)
        finally:
            if original is not None:
                self._headers = original
            else:
                self.__dict__.pop("_headers", None)

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
        # 探测必须和实际播放走同一个地址：源声明 media.url_rewrite 时（洛雪TV
        # 把被墙的 CDN 镜像主机改成同路径的另一台），拿原地址探测会 403 误判
        # 「这条线路不可用」，而播放时其实是好的。
        url = self._apply_media_url_rewrite(source, url)
        headers = dict(self._headers(source))
        # 探测尽量带播放页 Referer（防盗链 CDN 可能只认播放页）。
        episode_ref = str(headers.pop("X-Episode-Referer", "") or "")
        if episode_ref.startswith(("http://", "https://")):
            headers["Referer"] = episode_ref
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

    # ------------------------------------------------------------------ #
    # 线路可用性探测（「检测播放源」的同源线路分支）
    # ------------------------------------------------------------------ #
    def probe_line_playable(
        self,
        source: SourceConfig,
        detail_url: str,
        episode_title: str = "",
        episode_index: int = -1,
        deadline=None,
        context=None,
    ) -> dict:
        """探测一条「详情 URL 型」线路能不能正常播放。

        搜索分组（`search.group`）与 related_episodes 产出的线路项只有
        `detail_url`：换线路 = 用该线路自己的 source/id 重新取详情。所以
        「这个源能不能播」不能只看详情接口有没有回数据，必须真取一次它的
        分集地址，再走**和播放同一条链路**（本地媒体代理 → 清单 → 变体 →
        首个分片）验一遍——洛雪TV 的 jl 详情有分集，但分集指向的 CDN 镜像
        主机（`jimaoys94.com`）对非浏览器出口统一 403：只看详情、甚至只看
        清单 HTTP 200，都会把一条播不了的线路报成「可播放」（清单 200 但
        分片 403 时，播放器就是黑屏）。

        选集规则与播放一致：优先 `episode_index`（App 换线路后按序号映射
        同一集），`<0` 时按标题匹配，都没有取首集。

        返回 `{"ok", "url", "episodes", "reason", "via", "segment"}`：
        - `ok` 整条链路可播（清单有 `#EXTINF`、首个分片 HTTP<400 且非空非 HTML）
        - `reason` 失败原因：`detail:…` / `no_episodes` / `no_url` /
          `http403` / `html` / `no_variant` / `no_segment` /
          `segment_http403` / `segment_empty` / `timeout` / `probe:…`
        任何异常都收敛成 `ok=False`，探测不能影响播放界面。
        """
        result = {
            "ok": False, "url": "", "episodes": 0, "reason": "",
            "via": "direct", "segment": "",
        }
        if not detail_url:
            result["reason"] = "no_detail_url"
            return result
        try:
            detail = self.fetch_detail(source, detail_url, context=context)
        except StreamCancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 线路详情取不到＝这条线路不可用
            result["reason"] = f"detail:{type(exc).__name__}"
            return result
        chapters = list(getattr(detail, "chapters", None) or [])
        result["episodes"] = len(chapters)
        if not chapters:
            result["reason"] = "no_episodes"
            return result
        target = None
        wanted = (episode_title or "").strip()
        index = int(episode_index) if isinstance(episode_index, (int, float)) else -1
        if 0 <= index < len(chapters):
            target = chapters[index]
        elif wanted:
            for chapter in chapters:
                if (getattr(chapter, "title", "") or "").strip() == wanted:
                    target = chapter
                    break
        if target is None:
            target = chapters[0]
        url = str(getattr(target, "url", "") or "")
        result["url"] = url
        if not url:
            result["reason"] = "no_url"
            return result
        try:
            chain = self._probe_playback_chain(
                source, url, deadline=deadline, context=context
            )
        except StreamCancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            result["reason"] = f"probe:{type(exc).__name__}"
            return result
        result["via"] = str(chain.get("via") or "direct")
        result["segment"] = str(chain.get("segment") or "")
        result["ok"] = bool(chain.get("ok"))
        if not result["ok"]:
            result["reason"] = str(chain.get("reason") or "unreachable")
            return result
        # 严格模式：再用 App 的播放器真解几帧（默认关，见 verify_player_probe）
        try:
            strict = bool(self._settings_opt("verify_player_probe", False))
        except Exception:  # noqa: BLE001
            strict = False
        if not strict:
            return result
        played = self._probe_player_playback(source, url, deadline=deadline, context=context)
        if played.get("ok") is False:
            result["ok"] = False
            result["reason"] = str(played.get("reason") or "player_failed")
        elif played.get("ok") is None:
            result["reason"] = ""       # 没装播放器：不判死，保留链路结论
        return result

    def _probe_player_playback(
        self, source: SourceConfig, url: str, deadline=None, context=None
    ) -> dict:
        """严格模式：拿 App 播的同一个代理地址，用 mpv 真解几帧。

        「显示可播放却播不了」还有一层链路探测看不到的：播放器/解码/代理重写
        在真实会话里的失败。这里直接用 App 的播放器（mpv）跑 8 帧：能出帧就是
        真的能播。没装 mpv 返回 `ok=None`（跳过，不能本机没装就把所有线路判死）。

        代价：每条线路 5~25s（HLS 起播要拉清单+分片+解码），所以默认关闭，
        由 `sources_runtime.verify_player_probe` 打开。
        """
        result = {"ok": None, "reason": ""}
        if not url or not url.startswith(("http://", "https://")):
            result["reason"] = "not_http"
            return result
        try:
            from framework import desktop_player

            _key, exe = desktop_player.resolve("mpv")
        except Exception:  # noqa: BLE001
            exe = ""
        if not exe:
            result["reason"] = "no_player"
            return result
        target = url
        try:
            from framework.media_proxy import proxy_url_for

            raw = getattr(source, "raw", None) or {}
            ad = raw.get("ad_block") if isinstance(raw, dict) else None
            ad_cfg = ad if isinstance(ad, dict) and ad.get("enabled") else None
            proxied = proxy_url_for(
                self._apply_media_url_rewrite(source, url),
                self._probe_headers(source),
                ad_cfg,
                source_id=getattr(source, "source_id", None),
            )
            if proxied:
                target = proxied
        except Exception:  # noqa: BLE001
            target = url
        timeout = self._probe_timeout(source, context, deadline)
        budget = 30.0 if timeout is None else max(15.0, min(60.0, timeout * 6))
        cmd = [
            exe, "--no-config", "--vo=null", "--ao=null", "--frames=8",
            "--msg-level=all=error", "--really-quiet", target,
        ]
        try:
            from framework.subprocess_no_window import run as _run

            done = _run(cmd, capture_output=True, text=True, timeout=budget)
        except Exception as exc:  # noqa: BLE001
            result["ok"] = False
            result["reason"] = (
                "player_timeout"
                if "Timeout" in type(exc).__name__ or "timeout" in str(exc).lower()
                else f"player_error:{type(exc).__name__}"
            )
            return result
        if int(getattr(done, "returncode", 1) or 0) != 0:
            result["ok"] = False
            result["reason"] = "player_failed"
            return result
        result["ok"] = True
        return result

    # ---- 播放链路探测（与播放同路：媒体代理 → 清单 → 变体 → 分片） ----
    def _probe_headers(self, source: SourceConfig) -> dict:
        headers = dict(self._headers(source))
        # 探测尽量带播放页 Referer（防盗链 CDN 可能只认播放页）。
        episode_ref = str(headers.pop("X-Episode-Referer", "") or "")
        if episode_ref.startswith(("http://", "https://")):
            headers["Referer"] = episode_ref
        return headers

    def _probe_timeout(self, source: SourceConfig, context, deadline) -> float | None:
        """单次探测请求的超时；预算耗尽返回 None（＝不发了）。"""
        try:
            probe_cap = float(self._settings_opt("play_url_probe_timeout_cap", 5.0) or 5.0)
        except (TypeError, ValueError):
            probe_cap = 5.0
        probe_cap = max(1.0, min(30.0, probe_cap))
        configured = min(float(self._timeout(source)) or 10.0, probe_cap)
        if context is not None:
            remaining = context.remaining()
            if remaining <= 0:
                return None
            return min(configured, remaining)
        if deadline is None:
            return max(3.0, configured)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        return min(configured, remaining)

    def _probe_fetch(
        self, source, url, headers, context, deadline, read: int = 8192
    ):
        """读一次 URL 的前 `read` 字节；返回 (status, body, content_type)。"""
        timeout = self._probe_timeout(source, context, deadline)
        if timeout is None:
            return None, b"", ""
        import requests as _req

        with _req.get(url, headers=headers, timeout=timeout, stream=True) as resp:
            status = int(resp.status_code)
            ctype = str(resp.headers.get("Content-Type", "") or "")
            if read > _PROBE_CHUNK and _probe_wants_manifest(url, ctype):
                body = self._probe_read_manifest(resp, context, deadline, read)
            else:
                body = resp.raw.read(read) or b""
        self._check_stream_context(context, deadline)
        return status, body, ctype

    def _probe_read_manifest(self, resp, context, deadline, limit: int) -> bytes:
        """小块读清单，够判定/够取出首个分片就停。

        一次性 `raw.read(262144)` 会卡在「等满 256KB 或 EOF」上：聚合站的大清单
        （2500+ 段、200KB+，经媒体代理再转发）经常读满超时 → 把好线路判成
        `error:ReadTimeout`。分块读到「能分类 + 拿到首段 URI」就够，实测单条
        线路从 ~19s 降到 1~3s。
        """
        chunk_size = _PROBE_CHUNK
        chunks: list = []
        total = 0
        while total < limit:
            chunk = resp.raw.read(min(chunk_size, limit - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            self._check_stream_context(context, deadline)
            text = b"".join(chunks).decode("utf-8", "ignore")
            if _probe_manifest_ready(text):
                break
        return b"".join(chunks)

    @staticmethod
    def _probe_abs(base: str, uri: str) -> str:
        from urllib.parse import urljoin

        if not uri:
            return base
        return urljoin(base, uri)

    def _probe_playback_chain(
        self, source: SourceConfig, url: str, deadline=None, context=None
    ) -> dict:
        """按播放链路验证：媒体代理 → 清单 → 变体 → 首个分片。

        为什么不能只看清单 HTTP 200：源站给 HLS 的 200 只说明「清单文本拿到了」，
        播放器真正要的是清单里的分片。分片 403/空/HTML 错误页时播放器就是黑屏，
        而清单依然 200 —— 「显示可播放却不能播」几乎都是这一层。

        为什么走本地媒体代理：App 播的是代理 URL（`proxy_url_for`），清单要经过
        m3u8 重写与广告段过滤。代理这一层把清单改坏（剔除关键段、重写 URI 失效）
        同样是黑屏，直连探测看不到。代理没起来时退回直连，探测不因此失败。
        """
        result = {"ok": False, "reason": "", "url": url, "via": "direct", "segment": ""}
        context = self._stream_context(context, deadline)
        self._check_stream_context(context, deadline)
        if not url or not url.startswith(("http://", "https://")):
            result["reason"] = "not_http"
            return result
        url = self._apply_media_url_rewrite(source, url)
        result["url"] = url
        headers = self._probe_headers(source)
        target = url
        try:
            from framework.media_proxy import proxy_url_for

            raw = getattr(source, "raw", None) or {}
            ad = raw.get("ad_block") if isinstance(raw, dict) else None
            ad_cfg = ad if isinstance(ad, dict) and ad.get("enabled") else None
            proxied = proxy_url_for(
                url, headers, ad_cfg, source_id=getattr(source, "source_id", None)
            )
            if proxied and proxied != url:
                target = proxied
                result["via"] = "proxy"
        except Exception:  # noqa: BLE001 —— 代理不可用不影响探测本身
            target = url

        for _depth in range(3):
            try:
                status, body, _ctype = self._probe_fetch(
                    source, target, headers, context, deadline, read=65536
                )
            except StreamCancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                result["reason"] = f"error:{type(exc).__name__}"
                return result
            if status is None:
                result["reason"] = "timeout"
                return result
            if status >= 400:
                result["reason"] = f"http{status}"
                return result
            text = body.decode("utf-8", "ignore")
            if text.lstrip().startswith("<"):
                result["reason"] = "html"      # 200 的错误页，播放器黑屏
                return result
            if "#EXTM3U" not in text[:256]:
                # 非 HLS 直链媒体（mp4 等）：读到字节即视为可播
                result["ok"] = bool(body)
                if not body:
                    result["reason"] = "empty"
                return result
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            uris = [line for line in lines if not line.startswith("#")]
            if any(line.startswith("#EXT-X-STREAM-INF") for line in lines):
                if not uris:
                    result["reason"] = "no_variant"
                    return result
                target = self._probe_abs(target, uris[0])
                continue
            if not any(line.startswith("#EXTINF") for line in lines):
                result["reason"] = "no_segment"
                return result
            segment = self._probe_abs(target, uris[0])
            result["segment"] = segment
            try:
                seg_status, seg_body, _seg_ctype = self._probe_fetch(
                    source, segment, headers, context, deadline, read=4096
                )
            except StreamCancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                result["reason"] = f"segment_error:{type(exc).__name__}"
                return result
            if seg_status is None:
                result["reason"] = "timeout"
                return result
            if seg_status >= 400:
                result["reason"] = f"segment_http{seg_status}"
                return result
            if not seg_body:
                result["reason"] = "segment_empty"
                return result
            if seg_body[:1] == b"<":
                result["reason"] = "segment_html"
                return result
            result["ok"] = True
            return result
        result["reason"] = "chain_too_deep"
        return result

    def _switch_candidate_episode_urls_fast(
        self, source: SourceConfig, episode_url: str
    ) -> list:
        """零请求算出同集其它线路 URL：线路 tab 来自已缓存的播放页。

        omofuna 的分集 URL 自带线路坐标（`/play/<sid>/<集>.html?sid=<sid>`），
        换线路只是把这两处 sid 换掉。线路集合直接读**当前播放页**的线路 tab
        （页面早已抓过、在短 TTL 缓存里），不必为每条线路各抓一次详情页——
        那正是「换线要先等十几秒」的来源（实测 11.7s，且站点一抽风就超时）。
        """
        switch = self._get_source_switch_cfg(source)
        if not switch:
            return []
        abs_ep = self._abs_url(source, episode_url)
        html = ""
        page_cache = getattr(self, "_play_page_cache", None) or {}
        hit = page_cache.get((source.source_id, abs_ep))
        if hit:
            html = hit[1] or ""
        if not html:
            html = (getattr(self, "_video_html_cache", None) or {}).get(
                (source.source_id, abs_ep), ""
            ) or ""
        if not html:
            return []
        try:
            sids = [
                str(item.get("sid") or "")
                for item in self._parse_source_list(source, html)
            ]
        except Exception:  # noqa: BLE001 —— 解析失败就走老路
            return []
        if not sids:
            return []
        m_cur = _re.search(r"[?&]sid=(\d+)", abs_ep)
        cur_sid = m_cur.group(1) if m_cur else str(switch.get("default_sid") or "1")
        out: list = []
        for sid in sids:
            if not sid or sid == cur_sid:
                continue
            cand = self._swap_line_sid(abs_ep, cur_sid, sid)
            if cand and cand not in out:
                out.append(cand)
        return out

    @staticmethod
    def _swap_line_sid(url: str, cur_sid: str, sid: str) -> str:
        """把分集 URL 的线路坐标从 cur_sid 换成 sid（路径段 + sid 参数）。"""
        if not url or not cur_sid or not sid:
            return ""
        text = _re.sub(
            r"(/play/)" + _re.escape(cur_sid) + r"(/)", r"\g<1>" + sid + r"\g<2>",
            url, count=1,
        )
        text = _re.sub(
            r"([?&]sid=)" + _re.escape(cur_sid) + r"(?=&|\b|$)", r"\g<1>" + sid, text
        )
        return text if text != url else ""

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
        # 其它线路的分集列表**并行**取：每条线路要一次详情页请求（omofuna 实测
        # 单页 TTFB 1~2s），串行三条就是 3~6s，而串行还是**默认线路失败时**才走，
        # 正是用户最急着出画面的时刻。并发上限取源 constraints.max_concurrency
        # （缺省 3），共享线程池，不额外开线程。
        try:
            max_parallel = int((source.raw.get("constraints") or {}).get("max_concurrency") or 3)
        except (TypeError, ValueError, AttributeError):
            max_parallel = 3
        max_parallel = max(1, min(4, max_parallel))
        return self._switch_candidate_episode_urls_parallel(
            source, detail_url, idx, cur_sid,
            [str(line.get("sid") or "") for line in (base_detail.source_list or [])],
            deadline=deadline, context=context, max_parallel=max_parallel,
            episode_url=episode_url,
        )

    def _switch_candidate_episode_urls_parallel(
        self, source: SourceConfig, detail_url: str, idx, cur_sid: str,
        sids: list, deadline=None, context=None, max_parallel: int = 3,
        episode_url: str = "",
    ) -> list:
        """并行取各其它线路分集，按 sid 顺序返回「同集」URL 列表。

        顺序语义保留：结果按 sids 原序排列（换线路优先级不变）；
        失败/空列表的线路直接跳过；context 取消立即停止派新任务。
        """
        pending = [sid for sid in sids if sid and sid != cur_sid]
        if not pending:
            return []
        results: dict = {}
        lock = threading.Lock()

        def worker(sid: str) -> None:
            try:
                if context is not None:
                    self._check_stream_context(context, deadline)
                if deadline is None and context is None:
                    _, chs = self.switch_source(source, detail_url, sid)
                else:
                    _, chs = self.switch_source(
                        source, detail_url, sid, deadline=deadline, context=context)
            except StreamCancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                log.info("[%s] 阶段=换线，sid=%s，异常类型=%s",
                         source.source_id, sid, self._safe_exception_summary(exc))
                return
            if not chs:
                return
            # 优先按「集数序号」在新线路里找同一集：不同线路的分集列表长度/顺序
            # 可能不一致（omofuna sid=3 只有 22 项、sid=2 有 24 项），用位置 idx
            # 会错位甚至越界。序号匹配不上才退回 idx。
            target_ordinal = self._episode_ordinal(episode_url)
            u = ""
            if target_ordinal is not None:
                for ch in chs:
                    if self._episode_ordinal(ch.url) == target_ordinal:
                        u = ch.url
                        break
            if not u and idx is not None and idx < len(chs):
                u = chs[idx].url
            # 都定位不到就不要乱猜：早先兜底 `chs[0]` 会让「点第 22 集」
            # 播成「第 1 集」（omofuna 换线实测）。宁可在本线路失败、让上层
            # 继续试别的线路，也不给用户放错集。
            if not u:
                return
            if u:
                with lock:
                    results[sid] = u

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=max(1, int(max_parallel)),
                                thread_name_prefix="switch-line") as pool:
            futures = [pool.submit(worker, sid) for sid in pending]
            for future in futures:
                try:
                    future.result()
                except Exception:  # noqa: BLE001 —— worker 内已兜底
                    pass
        out: list = []
        seen: set = set()
        for sid in pending:
            u = results.get(sid)
            if u and u not in seen:
                seen.add(u)
                out.append(u)
        # 取消的语义必须如实上报：worker 里捕获取消是为了让各线程干净收尾，
        # 但"跑到一半被取消"的候选表不能当完整结果返回 —— 否则上层会把
        # 取消当成"没有其它线路可用"，最终报成一条含糊的取流失败。
        # 只检查取消（不传 deadline），避免"刚好到点"也抛错。
        if context is not None:
            self._check_stream_context(context)
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
        """URL 路径末段数字（分集号/集索引），用于跨线路定位同一集。

        兼容 `.html` 后缀（omofuna 这类 `/play/3/22.html?sid=3`）：早先只认
        结尾纯数字，`.html` 一律返回 None → 换线时定位不到集号 → 兜底
        `chs[0]`（第 1 集），点第 22 集却播成第 1 集。
        """
        path = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        m = _re.search(r"(\d+)(?:\.(?:html?|shtml|php|htm))?$", path)
        return int(m.group(1)) if m else None

    def _fetch_streams_ytdlp(self, source: SourceConfig, episode_url: str, cfg: dict,
                             quality: str = "", merged: bool = False, deadline=None, context=None) -> tuple:
        """yt-dlp 引擎：拿高清双流（YouTube 需签名，委托 yt-dlp）。

        merged=True（播放端）时：YouTube 等已 DASH 化，所有格式都是「video only」，
        没有含音轨的 progressive 流，`best` 必失败；退而求其次的双流
        bestvideo+bestaudio 返回 (video, audio) 后，VLC 用 :input-slave 喂分离
        音频轨对 DASH/fMP4 支持不可靠（黑屏，设计 §7）。因此播放路径改为产出
        **单一合并 mp4**（视频+音频已 mux 进同一文件），VLC 当普通文件直接
        播——这是唯一稳定出画面+声音的形态（已本地验证 VLC rc=0）。
        合并产物落到本地缓存目录，返回 (本地mp4路径, "")，下游当单流 MRL 处理。

        非 merged（下载端）仍走双流 bestvideo+bestaudio，由 FFmpegMerger 合并。

        缓存：同视频短时间复用，避免重复合并等待。
        """
        context = self._stream_context(context, deadline)
        self._check_stream_context(context, deadline)
        yt = self._get_ytdlp()

        # 分集 URL 可能是相对路径（HTML 搜索结果未绝对化时）→ 补全，
        # yt-dlp 只认完整 URL
        episode_url = self._abs_url(source, episode_url)

        if merged:
            # 播放端：合并成单一 mp4（DASH 站点唯一稳播形态）
            try:
                local = self._merged_play_file(yt, episode_url, quality, deadline, context)
                self._check_stream_context(context, deadline)
                return local, ""
            except StreamCancelledError:
                raise
            except Exception as exc:
                self._check_stream_context(context, deadline)
                raise ContentMissingError(
                    f"yt-dlp 合并取流失败：{exc}",
                    source_id=source.source_id,
                ) from None

        fmt = self._ytdlp_format(cfg, quality)
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
            # 双流主格式失败（网络/签名瞬断）：重试主格式本身
            if "audio" not in fmt:
                try:
                    streams = self._call_with_remaining_timeout(
                        yt.fetch_streams, (episode_url,), {"fmt": fmt},
                        context.deadline if context is not None else deadline
                    )
                    self._check_stream_context(context, deadline)
                    return streams
                except StreamCancelledError:
                    raise
                except Exception:
                    self._check_stream_context(context, deadline)
            raise ContentMissingError(
                f"yt-dlp 取流失败：{exc}",
                source_id=source.source_id,
            ) from None
        self._check_stream_context(context, deadline)
        return streams

    def _merged_play_file(self, yt, episode_url: str, quality: str, deadline, context) -> str:
        """产出一个单一合并 mp4 本地路径（播放端用）。

        落盘到 data/stream_cache/<hash>.mp4，复用避免重复合并。失败抛异常。
        """
        import time
        from pathlib import Path
        cache_dir = self._stream_cache_dir()
        key = self._abs_url_hash(episode_url, quality)
        out_path = cache_dir / f"yt_{key}.mp4"
        # 已有产物且未过期（10 分钟内）直接复用
        try:
            if out_path.exists() and (time.time() - out_path.stat().st_mtime) < 600:
                return str(out_path)
        except OSError:
            pass
        out_path = yt.fetch_merged_stream_file(
            episode_url, str(out_path), quality=quality, deadline=deadline
        )
        return out_path

    def _stream_cache_dir(self) -> "Path":
        from pathlib import Path
        d = getattr(getattr(self, "_cache", None), "_base_dir", None)
        if d is None:
            # 退化：项目根 data/stream_cache
            d = Path(__file__).resolve().parent.parent / "data" / "stream_cache"
        else:
            d = Path(d) / "stream_cache"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return d

    @staticmethod
    def _abs_url_hash(episode_url: str, quality: str) -> str:
        import hashlib
        return hashlib.sha1(f"{episode_url}|{quality}".encode("utf-8", "replace")).hexdigest()[:16]

    def _get_ytdlp(self):
        """懒加载单例 Ytdlp（复用，避免每次子进程重复探测）。"""
        if self._ytdlp is None:
            from framework.ytdlp import Ytdlp

            self._ytdlp = Ytdlp()
        return self._ytdlp

    # ------------------------------------------------------------------ #
    # 线路列表（同作品多线路）：详情分集带 line_switch_url → 播放界面换线路
    # ------------------------------------------------------------------ #
    def _attach_line_sources(self, source: SourceConfig, detail) -> None:
        """把详情里并入的「同作品其它线路」整理成播放界面的线路列表。

        只为现有 source_list **补位**（已有多线路分组的源——ikanpp 搜索分组产出的
        「其它采集源」——保持不变）。播放界面换线路时把该线路的任一集 URI 交给
        player 的线路列表，换线路后详情 URL 也跟着变（见 switch_source_url）。

        仅视频详情生效；无线路切换字段（普通源）时直接返回，零影响。
        """
        if source.content_type != "video" or getattr(detail, "source_list", None):
            return
        lines = self.line_menu_from_chapters(detail.chapters or [])
        if lines:
            detail.source_list = lines

    @staticmethod
    def line_menu_from_chapters(chapters) -> List[dict]:
        """把带 line_switch_* 字段的分集整理成播放界面的线路菜单。

        线路菜单的契约与 `source_switch` 一致：每项 `{source_id, sid, name,
        detail_url, episodes}`，播放界面只认这一份。`episodes` 是该线路的集，
        供切换时按标题/位置定位到同一集。
        """
        lines: List[dict] = []
        seen: set = set()
        for chapter in chapters or []:
            switch_url = getattr(chapter, "line_switch_url", "") or ""
            line_source = getattr(chapter, "line_switch_source", "") or ""
            if not switch_url or not line_source or line_source in seen:
                continue
            seen.add(line_source)
            lines.append({
                "source_id": line_source,
                "sid": line_source,
                "name": getattr(chapter, "line_name", "") or line_source,
                "detail_url": switch_url,
                # 该线路的集（播放界面按集号/标题定位到当前集）
                "episodes": [
                    {
                        "title": getattr(ch, "title", "") or "",
                        "url": getattr(ch, "url", "") or "",
                    }
                    for ch in chapters
                    if (getattr(ch, "line_switch_source", "") or "") == line_source
                ],
            })
        return lines

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
        # 已取消/超预算时直接中止：这么晚才发现取消，说明上层调用链已经
        # 不要这次结果了，继续解析（还可能补发 playerconfig 请求）纯属浪费
        # 预算 —— 而且会把取消吞成「线路表为空」，上层当成"没有别的线路"。
        self._check_stream_context(context)
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
            # 结构变化兜底：线路 tab 的 href 自带线路坐标（omofuna 2026-11：
            # `a.play-channel-item[href=/anime/<id>/play/<sid>/<集>.html]`，
            # 同时还有 data-sid）。站点改版时只要 href 还在就仍能认出线路；
            # 拿不到 sid 的后果不是"少一条线路"，而是整份线路表为空 →
            # 换源菜单消失、失败后永远无法换线（用户看到的就是取流一直超时）。
            if not sid and hasattr(node, "get"):
                href = str(node.get("href") or "")
                m_href = _re.search(r"/play/(\d+)/", href)
                if m_href:
                    sid = m_href.group(1)
                else:
                    # 详情页线路 tab 的形态：`<a href="#playlist1">精品</a>`
                    m_tab = _re.search(r"#playlist(\d+)", href)
                    if m_tab:
                        sid = m_tab.group(1)
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

    def _source_list_from_first_episode(self, source: SourceConfig, detail, context=None) -> List[dict]:
        """详情页没有线路 tab 时，改从首集播放页解析可用线路（omofuna 形态）。

        有些换源站的线路 tab 只渲染在**播放页**（omofuna 详情页 0 个
        .play-channel-item，播放页 3 条：1/2/4），详情解析永远拿不到线路 →
        播放界面「换源」菜单为空、点不动。此处在详情解析为空时补一次首集页
        请求拿线路。仅当 source_switch.probe_from_chapter 显式开启才做，
        避免给所有换源站多加一次请求。
        """
        switch = self._get_source_switch_cfg(source)
        if not (switch.get("probe_from_chapter") or switch.get("probe_from_episode")):
            return []
        chapters = list(getattr(detail, "chapters", None) or [])
        if not chapters:
            return []
        ep_url = self._abs_url(source, chapters[0].url)
        if not ep_url:
            return []
        try:
            headers = dict(self._headers(source))
            headers.setdefault("Referer", getattr(detail, "url", "") or ep_url)
            # 线路菜单是「顺手补」，不值得拖慢详情：单次尝试 + 最长 6s（播放页
            # 正常 TTFB 1~1.3s），站点抽风时立刻放弃，交给播放期间的自动轮换。
            timeout = min(self._timeout(source), 6.0)
            if context is not None:
                timeout = min(timeout, context.remaining())
            html = self._http.get_text(
                ep_url,
                headers=headers,
                timeout=timeout,
                retries=0,
                interval_ms=0,
                encoding=source.transports().get("charset"),
                proxy_pool=source.proxy_pool(),
            )
            self._check_stream_context(context)
        except StreamCancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 线路探测失败不影响观看
            log.info(
                "[%s] 阶段=换源，首集页解析线路失败：%s",
                source.source_id,
                type(exc).__name__,
            )
            return []
        return self._parse_source_list(source, html, context=context)

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
        # JS escape（%uXXXX）混编的地址必须还原成合法 URL，否则播放器 404：
        # omofuna 的 `%u7B2C22%u96C6` 实际是 UTF-8 的 `%E7%AC%AC22%E9%9B%86`。
        raw_url = self._normalize_js_encoded_url(raw_url)
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

    def _episode_link_nodes(self, doc, switch_cfg: dict) -> list:
        """按 ep_href_contains 从详情页 doc 取所有分集链接节点。"""
        needle = str(switch_cfg.get("ep_href_contains") or "/play/")
        sel = f'a[href*="{needle}"]'
        try:
            return list(self._parser._query(doc, sel, None) or [])
        except Exception:  # noqa: BLE001 —— 选择器异常按无链接处理
            return []

    def _first_episode_sid(self, doc, switch_cfg: dict) -> str:
        """详情页里真实出现的第一条线路号（站点可能没有 default_sid 那条线）。"""
        for node in self._episode_link_nodes(doc, switch_cfg):
            href = str(node.get("href") or "") if hasattr(node, "get") else ""
            m_sid = _re.search(r"/play/(\d+)/", href) or _re.search(r"sid=(\d+)", href)
            if m_sid:
                return m_sid.group(1)
        return ""

    def _chapters_from_all_links(self, source: SourceConfig, doc, switch_cfg: dict) -> List[Chapter]:
        """线路号未知时，把详情页所有分集链接按出现顺序收成分集。

        最后的兜底：宁可让用户看到「所有线路混排的分集」也不要空列表——空列表
        等于播放界面没有分集可点，用户看到的就是「加载失败」。
        """
        chapters: List[Chapter] = []
        seen: set = set()
        for node in self._episode_link_nodes(doc, switch_cfg):
            href = str(node.get("href") or "") if hasattr(node, "get") else ""
            if not href or href in seen:
                continue
            seen.add(href)
            title = node.text_content().strip() if hasattr(node, "text_content") else ""
            m_sid = _re.search(r"/play/(\d+)/", href)
            chapters.append(
                Chapter(
                    title=title or f"第{len(chapters) + 1}集",
                    url=self._abs_url(source, href),
                    source_id=m_sid.group(1) if m_sid else "",
                )
            )
        return chapters

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

    def switch_source_url(
        self, source: SourceConfig, target_url: str, deadline=None, context=None
    ) -> tuple:
        """按「目标详情 URL」换线路：重新抓详情，返回 (新 Detail, 新分集列表)。

        换线路后详情 URL 必须跟着目标走：`Detail.url` 是后续自动轮换
        （_switch_candidate_episode_urls 从它推导 id/source）、续读进度和
        「当前在哪条线路」的唯一来源。只改 sid 类的站两者等价，但
        ikanpp 这类「线路坐标在 query（id/source）」的站，沿用旧 URL 会在
        下次取流时又切回原线路。
        """
        context = self._stream_context(context, deadline)
        self._check_stream_context(context, deadline)
        if deadline is None:
            detail = self.fetch_detail(source, target_url)
        else:
            detail = self.fetch_detail(
                source, target_url, deadline=deadline, context=context
            )
        target_abs = self._abs_url(source, target_url)
        if target_abs and detail.url != target_abs:
            detail.url = target_abs
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
        timeout = self._timeout(source)
        retries = self._retries(source)
        if context is not None:
            timeout = min(timeout, context.remaining())
            retries = 0
            self._check_stream_context(context)
        method = (cfg.get("method") or "GET").upper()
        if method == "POST":
            # JSON POST 取流（69tv 类：接口只接受 POST，GET 返回 405）。
            # body 与 params 共用 {id}/{bvid}/{cid}/{quality} 占位符替换，
            # 接口所需请求头仍走端点级覆盖（_endpoint_headers）。
            from framework import utils

            placeholders = {
                "id": ep_id,
                "bvid": bvid,
                "cid": cid,
                "quality": str(q_value),
            }
            if q_param and q_param not in placeholders:
                placeholders[q_param] = str(q_value)
            body_filled = utils.fill_json(cfg.get("body") or {}, **placeholders)
            for k, v in params.items():
                body_filled.setdefault(k, utils.fill_json(v, **placeholders))
            if strategy:
                from framework.signers import get_signer

                signer = get_signer(strategy, self._http)
                body_filled = signer.sign(body_filled)
            resp = self._http.post_json(
                urljoin(source.base_url, api_url),
                json_body=body_filled,
                headers=self._endpoint_headers(source, cfg),
                timeout=timeout,
                retries=retries,
                proxy_pool=source.proxy_pool(),
            )
        else:
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
