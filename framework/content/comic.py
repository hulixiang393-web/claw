"""漫画：分页图片、解密、批量抓取。

B4 拆分自 content.py，方法原样迁移，行为不改变。
"""

from __future__ import annotations

from .shared import (
    AntiScrapeError, ContentMissingError, _re, log,
    utils,
)


class ComicMixin:
    def fetch_comic_pages(
        self,
        source: SourceConfig,
        chapter_url: str,
        on_page=None,
        cancel_evt=None,
        use_cache: bool = True,
    ) -> List[str]:
        """漫画：抓取一话的全部分页图片 URL（带缓存包装）。

        pages:{source_id}:{abs_url} 键命中（7 天）直接返回；否则内部抓取后写缓存。
        对应 endpoints.content.page：
        - render: playwright → 用 Playwright 渲染（分片加密站）
        - 普通源 → HTML 提取图片 URL

        on_page：可选分批回调 on_page(已就绪的前缀列表)。解密型源在
        _decrypt_image_urls 解密过程中分批回调（连续前缀），GUI 边收边渲染。
        cancel_evt：可选 threading.Event 取消令牌。换书/切页时置位，分页抓取
        循环里检查并提前返回（已抓到的部分），旧书取流立即让路给新书，不再
        白跑完一整话（dm5 一话 39 页 ≈74s）。None 表示不取消（下载器等同步调用）。
        """
        abs_url = self._abs_url(source, chapter_url)
        # v2 skips entries written by the old implementation, which could
        # persist a two-image HTML fallback for a week.
        cache_key = f"pages:v3:{source.source_id}:{abs_url}"
        cached = self._cache_get(cache_key) if use_cache else None
        if cached is not None:

            if on_page and cached:
                on_page(list(cached))
            return cached

        def _fetch() -> List[str]:
            degraded: list = []
            imgs = self._fetch_comic_pages_impl(
                source, chapter_url, on_page=on_page, cancel_evt=cancel_evt,
                degraded_out=degraded,
            )
            if cancel_evt and cancel_evt.is_set():
                degraded.append("cancelled partial comic page fetch")
            if use_cache and imgs and not degraded:
                self._cache_set(
                    cache_key,
                    list(imgs),
                    ttl=7 * 86400,
                )
            return imgs

        imgs = self._inflight_call(
            ("comic", source.source_id, abs_url, bool(use_cache)), _fetch
        )
        if on_page and imgs:
            try:
                on_page(list(imgs))
            except Exception:  # noqa: BLE001 — 回调异常不影响抓取结果
                pass
        return imgs

    def _fetch_comic_pages_impl(
        self,
        source: SourceConfig,
        chapter_url: str,
        on_page=None,
        cancel_evt=None,
        degraded_out=None,
    ) -> List[str]:
        """抓取一话分页图片 URL（无缓存包装）。

        degraded_out：可选 list。Playwright 渲染失败降级到 HTML 提取时置位
        （该结果不完整，调用方据此不入 pages: 缓存）。
        """
        content_cfg = self._content_block(source)
        block = content_cfg.get("page") or {}
        abs_url = self._abs_url(source, chapter_url)

        # 渲染策略：render 可能配置在 page.body（comicbox 结构）
        body_cfg = block.get("body") or {}
        render_mode = block.get("render") or body_cfg.get("render")

        # Playwright 渲染策略（分片加密图片）
        if render_mode == "playwright":
            # 具体用法由源配置 render_config 提供（框架只执行指令，不写死）：
            #   wait_for          渲染目标选择器（默认 canvas）
            #   wait_until        页面就绪策略（domcontentloaded / networkidle）
            #   extra_delay_ms    渲染后额外等待
            #   timeout_ms        页面加载超时
            #   click_selector    可选：渲染后点击该元素（展开弹层）
            #   scroll_to_bottom  可选：滚动到页底触发懒加载
            #   extract_mode      提取方式：canvas(默认) / img / text
            #   proxy             是否走代理（默认自动探测系统代理）
            rc = block.get("render_config") or body_cfg.get("render_config") or {}
            try:
                from framework.playwright_helper import fetch_rendered_images_sync

                imgs = fetch_rendered_images_sync(
                    abs_url,
                    wait_for=rc.get("wait_for", "canvas"),
                    wait_until=rc.get("wait_until", "domcontentloaded"),
                    timeout_ms=int(rc.get("timeout_ms") or 30000),
                    extra_delay_ms=int(rc.get("extra_delay_ms") or 2500),
                    click_selector=rc.get("click_selector"),
                    scroll_to_bottom=rc.get("scroll_to_bottom", False),
                    extract_mode=rc.get("extract_mode", "canvas"),
                    proxy=source.transports().get("proxy"),
                    page_container_selector=rc.get("page_container_selector"),
                    scroll_step_px=int(rc.get("scroll_step_px", 600)),
                    scroll_stale_rounds=int(rc.get("scroll_stale_rounds", 6)),
                    wheel_scroll=bool(rc.get("wheel_scroll", False)),
                    img_selector=rc.get("img_selector"),
                    img_js_path=rc.get("img_js_path"),
                    # 边滚边分批回调：连续前缀提前给 GUI 渲染（首图秒出，后续边滚边补）
                    on_batch=(lambda prefix: on_page(list(prefix))) if on_page else None,
                )
                minimum = int(rc.get("min_images") or 0)
                if minimum and len(imgs) < minimum and degraded_out is not None:
                    degraded_out.append("incomplete rendered comic page list")
                if on_page and imgs:
                    on_page(list(imgs))
                return imgs
            except Exception as exc:
                # Playwright 渲染失败 → 降级到普通 HTML 提取（站点改版/选择器不匹配时
                # 不整话失败，尝试 HTML 兜底；若 HTML 也提取不到，下方会抛 ContentMissingError）
                log.warning("[%s] Playwright 渲染失败，降级 HTML 提取：%s", source.source_id, exc)
                # canvas/分片加密源的页面是 JS 应用，静态 HTML 里只有首屏那几个占位
                # 元素（如 comicbox 只能提到 2 个 .cropped）——兜底结果不完整，若当
                # 整话缓存会让该章 7 天内永远只出这 2 张（其余页面再也渲染不出来）。
                if degraded_out is not None:
                    degraded_out.append(exc)

        self._bg_check(source, abs_url)
        # 图片列表优先 body，兼容旧 list
        list_cfg = body_cfg or block.get("list") or {}
        urls = self._fetch_comic_page_imgs(
            source, list_cfg, chapter_url, cancel_evt=cancel_evt, on_page=on_page,
            degraded_out=degraded_out,
        )
        if cancel_evt and cancel_evt.is_set() and degraded_out is not None:
            degraded_out.append("cancelled partial comic page fetch")
        # 图片解密源（如 18mh AES-CBC 加密图）：下载并把每张解密成 data URI，
        # 使阅读器/下载器无需改动即可显示/保存解密图。
        if urls and source.raw.get("decryption", {}).get("targets", {}).get("image"):
            return self._decrypt_image_urls(source, urls, on_page)
        if on_page and urls:
            try:
                on_page(list(urls))
            except Exception:  # noqa: BLE001 —— 回调异常不影响抓取结果
                pass
        return urls

    def _decrypt_image_urls(self, source: SourceConfig, urls: List[str], on_page=None) -> List[str]:
        """把加密图片 URL 并发下载并 AES 解密成 data URI（供阅读器/下载器直接用）。

        一话常几十张图，串行下载+解密要 15-30s 卡半天；改为 8 并发，
        并发把耗时压到 2-4s。单张失败 → 保留原 URL（可能封面等非加密图混入）。

        on_page：可选分批回调 on_page(已解密的前缀列表)。每次「从 0 起的连续
        前缀」增长时回调一次（保持顺序），GUI 可边解密边渲染，不等整话。

        注意：HttpClient 非线程安全，每 worker 各建独立实例（复用默认值）。
        """
        if len(urls) <= 1:
            # 单张直接走原逻辑（无并发开销）
            result = [self._decrypt_one_image(source, u) for u in urls]
            if on_page and result:
                on_page(list(result))
            return result

        from concurrent.futures import ThreadPoolExecutor, as_completed

        result: List = [None] * len(urls)
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(self._decrypt_one_image, source, u): i for i, u in enumerate(urls)}
            last_emit = 0
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    result[i] = fut.result()
                except Exception:  # noqa: BLE001
                    result[i] = urls[i]
                # 连续前缀完成 → 分批回调（边解密边显示，保持顺序）
                done = 0
                while done < len(result) and result[done] is not None:
                    done += 1
                if on_page and done > last_emit:
                    on_page(list(result[:done]))
                    last_emit = done
        return list(result)

    def _decrypt_one_image(self, source: SourceConfig, u: str) -> str:
        """下载单张并 AES 解密 → data URI；失败保留原 URL。

        并发下载用 urllib 独立连接（复用 HttpClient 的 Session 线程不安全），
        headers 带 Referer/UA 防图床防盗链。
        """
        try:
            import urllib.request

            abs_url = self._abs_url(source, u)
            req = urllib.request.Request(abs_url, headers=self._headers(source))
            timeout = self._timeout(source)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            plain = self._decrypter.decrypt_bytes(source, raw, target="image")
            if plain and plain is not raw:
                import base64 as _b64

                mime = self._guess_image_mime(plain)
                return f"data:{mime};base64,{_b64.b64encode(plain).decode()}"
            return u
        except Exception:  # noqa: BLE001
            return u  # 解密失败保底原 URL

    @staticmethod
    def _guess_image_mime(data: bytes) -> str:
        """按魔数猜图片 mime（JPEG/PNG/GIF/WEBP/BMP），默认 image/jpeg。"""
        return utils.guess_image_mime(data)

    def _fetch_comic_page_imgs(
        self, source: SourceConfig, list_cfg: dict, chapter_url: str,
        cancel_evt=None, on_page=None, degraded_out=None,
    ) -> List[str]:
        """从单话 HTML 提取全部图片 URL，支持图片列表翻页（含并行翻页加速）。

        对应 endpoints.content.page：
        - list.root_selector / list.fields.url   单图项与图片 URL 提取
        - list.paginator.next_link.selector     （可选）「下一页」链接，跨页拼全
          多页图片站（如每页固定 N 张、30P 需翻 3 页）。循环抓取直到：
            下一页链接缺失 / 已访问过（URL 去重防死循环）/ 达 max_pages 上限。
        - list.paginator.parallel               （可选）并行翻页：站点分页 URL
          可预测（如 {base}-2.html、{base}-3.html…）时按 wave 并发抓取，把每页
          interval_ms 礼貌延迟从顺序累积压成每 wave 并行平摊。模板验证失败 /
          并行异常 → 回退下方顺序循环（保证正确性）。
        - 图片 URL 用 data-src / data-original 懒加载属性时框架自动兜底
        - image_api（可选）：图片 URL 来自 AJAX 文本接口 + JS Packer 解包的源
          （如 dm5 chapterfun.ashx）。配置见 _fetch_comic_image_api。命中即
          走该路径，不解析页面 HTML 图片。
        """
        image_api = list_cfg.get("image_api") or {}
        if image_api:
            return self._fetch_comic_image_api(
                source, image_api, chapter_url, cancel_evt=cancel_evt, on_page=on_page
            )
        root_sel = list_cfg.get("root_selector")
        fields = list_cfg.get("fields") or {}
        if not root_sel or not fields.get("url"):
            raise ContentMissingError(
                "源未配置漫画图片规则（content.page）",
                source_id=source.source_id,
            )
        paginator = list_cfg.get("paginator") or {}
        max_pages = int(paginator.get("max_pages") or 0)
        next_sel = (paginator.get("next_link") or {}).get("selector")

        url_sel = fields.get("url")
        # 图片 URL 重写（缩略图 → 原图）：url_replace = [regex, repl]。
        # repl 支持 {n} 引用正则捕获组、{seq} 表示按收集顺序自动编号
        # （从 1 起，如 wnacg 缩略图时间戳 → img5 原图序号 0001..）。无匹配
        # 时保留原始 URL，避免误伤；仅当有 url_replace 才启用顺序编号。
        url_replace_re = ""
        url_replace_tpl = ""
        _ur = (url_sel or {}).get("url_replace") if isinstance(url_sel, dict) else None
        if isinstance(_ur, (list, tuple)) and len(_ur) == 2 and _ur[0]:
            url_replace_re = str(_ur[0])
            url_replace_tpl = str(_ur[1])
        urls: List[str] = []
        seen_url: set = set()   # 已访问的页面 URL（防死循环）
        seen_img: set = set()   # 已收集的图片 URL（跨页去重）
        _seq_counter = [0]  # {seq} 顺序编号器（被闭包共享）

        # 提取阶段的 fields 剥离 url_replace：parser.extract 的 url_replace 只
        # 支持 {1}..{n} 捕获组，不支持 {seq} —— 若在此阶段重写会把字面 {seq}
        # 残留进 URL，导致 _add_imgs 的重写正则（面向缩略图 URL）不再命中而
        # 原样输出 404（wnacg img5.qy0.ru {seq}.webp 即此坑）。故统一交由下方
        # _add_imgs（支持 {seq} 顺序编号）做唯一一次重写。
        fields_pt = fields
        if isinstance(fields.get("url"), dict) and fields["url"].get("url_replace"):
            fields_pt = dict(fields)
            f_url = dict(fields["url"])
            f_url.pop("url_replace", None)
            fields_pt["url"] = f_url

        # 共用小函数：抓单页 → (本页图片列表, 下一页链接)。顺序与并行翻页都复用。
        def _fetch_page(page_url: str, http=None) -> tuple:
            html = self._get(source, page_url, http=http)
            doc = self._parser.parse(html)
            # 在 root_selector 限定范围内提取图片 URL（复用 parse_items：
            # 每个 root 项内按 fields.url 取属性，自动 data-src 懒加载兜底）。
            # 无 root_selector 时回退整页提取。
            items = self._parser.parse_items(
                doc, root_sel, fields_pt, source.base_url
            )
            page_imgs = [it.get("url") or "" for it in items]
            page_imgs = [u for u in page_imgs if u]
            nxt = ""
            if next_sel:
                nxt = self._parser.extract_first(doc, next_sel, source.base_url) or ""
            return page_imgs, nxt

        def _add_imgs(page_imgs: List[str]) -> None:
            """广告过滤 + url_replace 重写 + 去重后并入最终列表（顺序/并行共用）。"""
            for u in self._filter_ad_images(page_imgs, source):
                final = u
                if url_replace_re:
                    m = _re.search(url_replace_re, u)
                    if m:
                        _seq_counter[0] += 1
                        repl = url_replace_tpl
                        for gi in range(1, len(m.groups()) + 1):
                            repl = repl.replace("{%d}" % gi, m.group(gi) or "")
                        repl = repl.replace("{seq}", "%04d" % _seq_counter[0])
                        final = repl
                if final and final not in seen_img:
                    seen_img.add(final)
                    urls.append(final)

        # 并行翻页（config-driven）：URL 可预测时按 wave 并发抓取，提速明显
        # （acgxmh 实测顺序 55 页 ≈71s → 并行 ~20s，window 过大触发站点 429
        # 限流丢页，window=6 为完整且最快档）。
        parallel = paginator.get("parallel") or {}
        if parallel and next_sel:
            try:
                # base：章节 URL 去 query 再 rsplit('.',1)[0] 去扩展名
                base = chapter_url.split("?", 1)[0].rsplit(".", 1)[0]
                tpl = str(parallel.get("url_template") or "")
                first_page = int(parallel.get("first_page") or 2)
                window = int(parallel.get("window") or 8)
                p_max = int(parallel.get("max_pages") or 100)
                if not base or not tpl or first_page < 2 or window < 1:
                    raise ValueError("parallel 配置无效")
                from concurrent.futures import ThreadPoolExecutor, as_completed

                def pred(n: int) -> str:
                    """第 n 页 URL（第 1 页即章节 URL 本身，从 first_page 起预测）。"""
                    return self._abs_url(source, tpl.format(base=base, page=n))

                # 第 1 页：抓章节 URL，收集图片 + 取 next 链接 nxt1。
                # 第 1 页也走限流退避重试（并发翻页偶发 429 时，首页失败会触发
                # 整个并行路径回退顺序 → 更慢，务必补抓而非直接放弃）。
                p1_imgs, nxt1 = ([], "")
                for _attempt in range(3):
                    try:
                        p1_imgs, nxt1 = _fetch_page(chapter_url)
                        break
                    except AntiScrapeError:
                        if _attempt < 2:
                            self._http._sleeper(1.0 * (2 ** _attempt))
                    except Exception:  # noqa: BLE001 —— 非限流异常交给外层回退顺序
                        raise
                # 模板验证（安全网）：nxt1 必须等于 pred(2)，否则模板错 → 回退顺序
                if not nxt1 or self._abs_url(source, nxt1) != pred(2):
                    raise ValueError("parallel URL 模板与站点分页不符")
                _add_imgs(p1_imgs)
                if on_page and urls:
                    try:
                        on_page(list(urls))  # 首图就绪即回调，阅读器秒出首屏
                    except Exception:  # noqa: BLE001
                        pass

                def _fetch_page_retry(p: int) -> tuple:
                    """抓第 p 页；独立 HttpClient（requests.Session 非线程安全，
                    每页一个实例避免并发竞态）。

                    429/5xx 限流响应退避重试（1s/2s，最多 3 次）——并发翻页偶发
                    限流时补抓该页，而非直接丢页（丢页会导致话内容不全）。4xx 等
                    确定性失败不重试（末页探测靠 next 判定，无需重试）。
                    """
                    page_http = self._http.__class__(
                        sleeper=getattr(self._http, "_sleeper", None),
                        defaults=self._http.defaults,
                    )
                    try:
                        last = ([], "")
                        for attempt in range(3):
                            try:
                                imgs, nxt = _fetch_page(pred(p), http=page_http)
                            except AntiScrapeError as exc:
                                last = ([], "")
                                if attempt < 2:
                                    self._http._sleeper(1.0 * (2 ** attempt))
                                    continue
                                log.debug("[%s] 分页 %d 限流重试耗尽：%s", source.source_id, p, exc)
                            except Exception:  # noqa: BLE001
                                last = ([], "")
                                if attempt < 2:
                                    self._http._sleeper(0.5 * (attempt + 1))
                                    continue
                            else:
                                last = (imgs, nxt)
                                if imgs:
                                    break  # 拿到图即成功
                                # 图片为空：可能是瞬时失败（并发下偶发丢响应），再试
                                if attempt < 2:
                                    self._http._sleeper(0.5 * (attempt + 1))
                        return last
                    finally:
                        try:
                            page_http.close()
                        except Exception:  # noqa: BLE001
                            pass

                n = first_page
                while n <= p_max:
                    if cancel_evt and cancel_evt.is_set():
                        if degraded_out is not None:
                            degraded_out.append("cancelled partial comic page fetch")
                        break  # 换书取消：停止后续 wave 抓取，返回已收集部分
                    wave = list(range(n, min(n + window, p_max + 1)))
                    # 每 wave 并发抓取（max_workers=min(window, 页数)）
                    results: dict = {}
                    with ThreadPoolExecutor(
                        max_workers=min(window, len(wave))
                    ) as pool:
                        futs = {pool.submit(_fetch_page_retry, p): p for p in wave}
                        for fut in as_completed(futs):
                            results[futs[fut]] = fut.result()
                    # 图片按页码顺序收集，与第 1 页拼接
                    populated = [p for p in wave if results.get(p, ([], ""))[0]]
                    # Empty trailing probes are the normal way a predictable
                    # paginator discovers its end. Only a hole before a later
                    # populated page proves the result is incomplete.
                    if populated:
                        last_populated = populated[-1]
                        if any(
                            not results.get(p, ([], ""))[0]
                            for p in wave if p < last_populated
                        ) and degraded_out is not None:
                            degraded_out.append("incomplete parallel comic page wave")
                        last_populated_nxt = results.get(last_populated, ([], ""))[1]
                        if (
                            last_populated < wave[-1]
                            and last_populated_nxt
                            and self._abs_url(source, last_populated_nxt)
                            == pred(last_populated + 1)
                            and not results.get(last_populated + 1, ([], ""))[0]
                            and degraded_out is not None
                        ):
                            degraded_out.append("missing expected parallel comic page")
                    elif degraded_out is not None:
                        # Entering this wave means the previous page explicitly
                        # linked to its first predicted page. An all-empty wave
                        # is therefore a fetch failure, not a natural ending.
                        degraded_out.append("missing expected parallel comic page")
                    for p in wave:
                        _add_imgs(results.get(p, ([], ""))[0])
                    # 边抓边回调（连续前缀）：阅读器首批图就绪即可渲染首屏，
                    # 无需等整话分页抓完（此前 HTML 路径全程无回调 → 21 页 20s 才出首图）。
                    if on_page and urls:
                        try:
                            on_page(list(urls))
                        except Exception:  # noqa: BLE001 —— 回调异常不影响抓取
                            pass
                    # 停止条件：wave 最后一页的 next 自环（== 自身）/为空/
                    # ≠ pred(最后一页+1) → 章节结束停止；否则继续下一 wave。
                    last_p = wave[-1]
                    last_nxt = results.get(last_p, ([], ""))[1]
                    if (
                        not last_nxt
                        or self._abs_url(source, last_nxt) == pred(last_p)
                        or self._abs_url(source, last_nxt) != pred(last_p + 1)
                    ):
                        break
                    n = last_p + 1
                return urls
            except Exception as exc:  # noqa: BLE001
                # 模板验证失败 / 并行异常 → 清空并行阶段结果，回退顺序循环（保证正确性）
                log.warning("[%s] 并行翻页回退顺序：%s", source.source_id, exc)
                urls = []
                seen_url = set()
                seen_img = set()

        # 顺序循环（fallback，未配置 parallel / 并行回退时走这里）
        page_url = chapter_url
        for _ in range(max_pages if max_pages else 1000):
            if cancel_evt and cancel_evt.is_set():
                if degraded_out is not None:
                    degraded_out.append("cancelled partial comic page fetch")
                break  # 换书取消：停止后续翻页抓取
            if page_url in seen_url:
                break
            seen_url.add(page_url)
            page_imgs, nxt = _fetch_page(page_url)
            _add_imgs(page_imgs)
            if nxt and not page_imgs and degraded_out is not None:
                degraded_out.append("incomplete sequential comic page")
            # 无下一页配置 → 单页即止
            if not next_sel or not nxt:
                break
            page_url = nxt

        return urls

    # ------------------------------------------------------------------ #
    def _fetch_comic_image_api(
        self, source: SourceConfig, cfg: dict, chapter_url: str,
        cancel_evt=None, on_page=None,
    ) -> List[str]:
        """漫画图片来自 AJAX 文本接口的源（配置驱动，如 dm5 chapterfun.ashx）。

        站点阅读页的图片 URL 不在 HTML 里，而是前端 JS 按页调用文本接口拿
        （接口返回 JS Packer 混淆脚本，内含图片地址）。完整流程（全部规则
        外置到源配置，本方法只做通用执行）：

        1. 抓阅读页（chapter_url）HTML，按 cfg.vars 的 {名: 正则} 提取 JS 变量
           （如 dm5 的 DM5_CID / DM5_MID / DM5_VIEWSIGN / DM5_VIEWSIGN_DT /
           DM5_IMAGE_COUNT），其中 count 为总页数；
        2. 对第 1..count 页：把 cfg.url 里的 {变量}/{page} 占位替换成提取值，
           逐个 GET（间隔走 transports.interval_ms）；
        3. cfg.packer=true 时对响应做 JS Packer（Dean Edwards packer）解包，
           否则原文使用；
        4. 按 cfg.pix_regex / cfg.paths_regex / cfg.key_regex 从解包文本提取
           图片 CDN 基址 / 路径数组 / 签名 key；
        5. 按 cfg.url_template（{pix} {path} {key} 及 JS 变量占位）拼出图片 URL，
           去重合并。

        配置示例（dm5）：
        ```json
        "body": {
          "image_api": {
            "url": "/chapterfun.ashx",
            "params": {
              "cid": "{cid}", "page": "{page}", "key": "",
              "language": 1, "gtk": 6,
              "_cid": "{cid}", "_mid": "{mid}", "_dt": "{sign_dt}", "_sign": "{sign}"
            },
            "vars": {
              "cid": "DM5_CID\\\\s*=\\\\s*(\\\\d+)",
              "mid": "DM5_MID\\\\s*=\\\\s*(\\\\d+)",
              "sign": "DM5_VIEWSIGN\\\\s*=\\\\s*\"([^\"]+)\"",
              "sign_dt": "DM5_VIEWSIGN_DT\\\\s*=\\\\s*\"([^\"]+)\"",
              "count": "DM5_IMAGE_COUNT\\\\s*=\\\\s*(\\\\d+)"
            },
            "packer": true,
            "pix_regex": "pix=\\\"([^\\\"]+)\\\"",
            "paths_regex": "pvalue=\\\\[([^\\\\]]*)\\\\]",
            "key_regex": "key='([^']+)'",
            "url_template": "{pix}{path}?cid={cid}&key={key}",
            "max_pages": 500
          }
        }
        ```

        注意：接口 query 参数用 `params` 对象声明（自动 urlencode，空格→`+`）比
        手拼 URL 更稳——手拼 URL 里含空格的变量（如 `_dt` 时间戳）经 requests
        会变 `%20`，dm5 这类对签名串敏感的接口会据此生成失效 key（图片 404）。
        """
        import time as _time
        from urllib.parse import urlencode

        # 1) 阅读页 JS 变量
        html = self._get(source, chapter_url)
        v: dict = {}
        for name, pattern in (cfg.get("vars") or {}).items():
            m = _re.search(str(pattern), html)
            if m:
                v[name] = m.group(1) if m.groups() else m.group(0)
        try:
            count = int(v.get("count") or cfg.get("count") or 1)
        except Exception:  # noqa: BLE001
            count = 1
        count = min(count, int(cfg.get("max_pages") or 500))
        if count <= 0:
            return []

        params_cfg = cfg.get("params") or {}
        api_url = str(cfg.get("url") or "")
        url_tpl = str(cfg.get("url_template") or "")
        pix_re = str(cfg.get("pix_regex") or r'pix="([^"]+)"')
        paths_re = str(cfg.get("paths_regex") or r"pvalue=\[([^\]]*)\]")
        key_re = str(cfg.get("key_regex") or "")
        # 渲染提取配置（如 dm5）：页面 JS 运行时动态计算图片 URL 的 key 参数，
        # 静态接口提取到的 key 可能是固定常量/诱饵（全 403）。配置存在时先
        # Playwright 渲染章节页，从 DOM 的 img.src 提取真实签名 key 覆盖接口
        # 结果；渲染失败/未拿到 → 保留原接口 key（不破坏其他源）。
        render_cfg = cfg.get("render_config") or {}
        render_key = ""
        if render_cfg:
            try:
                from framework.playwright_helper import fetch_rendered_images_sync

                rendered = fetch_rendered_images_sync(
                    self._abs_url(source, chapter_url),
                    wait_for=render_cfg.get("wait_for") or "img",
                    wait_until=render_cfg.get("wait_until") or "domcontentloaded",
                    timeout_ms=int(render_cfg.get("timeout_ms") or 30000),
                    extra_delay_ms=int(render_cfg.get("extra_delay_ms") or 1500),
                    extract_mode=render_cfg.get("extract_mode") or "img",
                    img_selector=render_cfg.get("img_selector"),
                    proxy=(None if source.transports().get("direct")
                           else source.transports().get("proxy")),
                )
                for u in rendered or []:
                    mk = _re.search(r"[?&]key=([^&#]+)", str(u))
                    if mk:
                        render_key = mk.group(1)
                        break
            except Exception as exc:
                log.warning(
                    "[%s] 渲染提取 key 失败，回退接口 key：%s",
                    source.source_id, self._safe_exception_summary(exc),
                )
        use_packer = bool(cfg.get("packer"))
        # 接口专用请求头（合并覆盖源头）——部分站点接口要求特定 Referer/Cookie
        api_headers = dict(self._headers(source))
        for k, val in (cfg.get("headers") or {}).items():
            api_headers[k] = val
        interval = (
            float(source.transports().get("interval_ms") or self._http.defaults.interval_ms)
            / 1000.0
        )

        def _fill(tpl: str, page: int) -> str:
            for name, val in v.items():
                tpl = tpl.replace("{" + name + "}", str(val))
            return tpl.replace("{page}", str(page))

        def _api_url(page: int) -> str:
            if params_cfg:
                filled = {}
                for k, val in params_cfg.items():
                    filled[k] = _fill(str(val), page)
                sep = "&" if "?" in api_url else "?"
                return api_url + sep + urlencode(filled)
            return _fill(api_url, page)

        def _fetch_api(page: int, http=None, interval_ms=None) -> str:
            """接口请求：走独立 headers，遇失败抛异常由调用方跳过该页。"""
            abs_url = self._abs_url(source, _api_url(page))
            http = http or self._http
            return http.get_text(
                abs_url,
                headers=api_headers,
                timeout=self._timeout(source),
                retries=self._retries(source),
                interval_ms=self._interval_ms(source)
                if interval_ms is None
                else interval_ms,
                encoding=source.transports().get("charset"),
                direct=bool(source.transports().get("direct")),
            )

        # 单页接口响应 → 图片 URL 列表（解析逻辑抽出，供顺序/并行共用）
        def _parse_page(resp_text: str, page: int, _seen: set) -> List[str]:
            plain = self._unpack_js_packer(resp_text) if use_packer else resp_text
            m = _re.search(pix_re, plain)
            pix = m.group(1) if m else ""
            mp = _re.search(paths_re, plain)
            paths = _re.findall(r'"([^"]+)"', mp.group(1)) if mp else []
            key = render_key or ""
            if not key and key_re:
                mk = _re.search(key_re, plain)
                if mk:
                    key = (mk.group(1) if mk.groups() else mk.group(0)).rstrip("\\")
            out: List[str] = []
            for p in paths:
                full = url_tpl.replace("{pix}", pix).replace("{path}", p)
                full = _fill(full, page).replace("{key}", key)
                if full and full not in _seen:
                    _seen.add(full)
                    out.append(full)
            return out

        # 并行抓取配置（config-driven）：接口每页独立请求时按 wave 并发，
        # 把 interval_ms 礼貌延迟从「逐页累积」压成「每 wave 平摊」——dm5 一话
        # 数十页，顺序 ≈74s，wave 并发可压到 2-5s。
        parallel = cfg.get("parallel") or {}
        window = int(parallel.get("window") or 8)
        wave_gap = (
            float(parallel.get("interval_ms") or interval * 1000.0) / 1000.0
        )
        urls: List[str] = []
        seen: set = set()
        if count > 1 and window > 1:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _fetch_page_retry(p: int, http) -> List[str]:
                last: List[str] = []
                for attempt in range(2):
                    try:
                        # 并行分支：逐页 interval 延迟由 wave 间 wave_gap 统一承担，
                        # 请求本身不再 sleep（否则 8 并发各自 sleep 900ms → wave 内 8s）
                        resp_text = _fetch_api(p, http=http, interval_ms=0)
                    except Exception:  # noqa: BLE001
                        continue
                    last = _parse_page(resp_text, p, seen)
                    if last:
                        break  # 拿到图即成功
                return last

            try:
                n = 1
                while n <= count:
                    if cancel_evt and cancel_evt.is_set():
                        break  # 换书取消：停止后续 wave，返回已收集部分
                    wave = list(range(n, min(n + window, count + 1)))
                    results: dict = {}
                    with ThreadPoolExecutor(
                        max_workers=min(window, len(wave))
                    ) as pool:
                        futs = {}
                        for p in wave:
                            page_http = self._http.__class__(
                                sleeper=getattr(self._http, "_sleeper", None),
                                defaults=self._http.defaults,
                            )
                            futs[pool.submit(_fetch_page_retry, p, page_http)] = (p, page_http)
                        for fut in as_completed(futs):
                            p, page_http = futs[fut]
                            try:
                                results[p] = fut.result()
                            except Exception:  # noqa: BLE001
                                results[p] = []
                            finally:
                                try:
                                    page_http.close()
                                except Exception:  # noqa: BLE001
                                    pass
                    for p in wave:
                        urls.extend(results.get(p, []))
                    # 每 wave 完成 → 回调已就绪前缀（边抓边显示：首批到达即可
                    # 渲染首屏，无需等整话抓完；顺序不变，后续批次续接）。
                    if on_page and urls:
                        on_page(list(urls))
                    if wave_gap > 0 and n + window <= count:
                        _time.sleep(wave_gap)
                    n += window
                return urls
            except Exception as exc:  # noqa: BLE001
                # 并行异常 → 清空重来，回退顺序循环（保证正确性）
                log.warning("[%s] image_api 并行抓取回退顺序：%s", source.source_id, exc)
                urls = []
                seen = set()

        # 顺序循环（单页 / 未配置 parallel / 并行回退）
        for page in range(1, count + 1):
            if cancel_evt and cancel_evt.is_set():
                break  # 换书取消：停止逐页接口抓取（dm5 一话几十页，立即让路）
            try:
                resp_text = _fetch_api(page)
            except Exception:  # noqa: BLE001
                continue  # 单页接口失败跳过（不整话失败）
            urls.extend(_parse_page(resp_text, page, seen))
            if on_page and urls:
                on_page(list(urls))
            if interval > 0 and page < count:
                _time.sleep(interval)
        return urls

    @staticmethod
    def _unpack_js_packer(code: str) -> str:
        """解 JS Packer（Dean Edwards packer）混淆脚本，返回解包后的 JS 文本。

        形态：eval(function(p,a,c,k,e,d){...}('密文',N,N,'词表'.split('|'),0,{}))
        解不开（非 packer / 已变形）→ 原样返回。解包后把 JS 字符串转义
        （\\' → '、\\" → "）还原，方便下游正则直接提取值。
        """
        import string as _string

        m = _re.search(
            r"eval\(function\(p,a,c,k,e,d\)\{.*?\}\('(.*?)',(\d+),(\d+),'(.*?)'\.split\('\|'\),0,\{\}\)\)",
            code,
            _re.S,
        )
        if not m:
            return code
        s, a, c, k = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).split("|")

        def _b36(n: int) -> str:
            return str(n) if n < 10 else _string.ascii_lowercase[n - 10]

        def _enc(n: int, base: int) -> str:
            if n < base:
                x = n % base
                return chr(x + 29) if x > 35 else _b36(x)
            return _enc(n // base, base) + _enc(n % base, base)

        out = s
        for i in range(c - 1, -1, -1):
            if i < len(k) and k[i]:
                out = _re.sub(
                    r"\b" + _enc(i, a) + r"\b",
                    k[i].replace("\\", "\\\\").replace("$", "\\$"),
                    out,
                )
        return out.replace("\\'", "'").replace('\\"', '"')

    def comic_page_urls(self, source: SourceConfig, chapter_url: str) -> List[str]:
        """纯 HTTP 提取一话的图片 URL 列表（不渲染，供预加载计数）。

        与 fetch_comic_pages 的 HTML 分支同逻辑，但始终走普通解析、
        不走 Playwright。返回原始图片 URL 列表（可能为混淆地址，
        仅用于统计页数与占位，正文显示仍走 fetch_comic_pages 渲染）。
        """
        content_cfg = self._content_block(source)
        block = content_cfg.get("page") or {}
        body_cfg = block.get("body") or {}
        abs_url = self._abs_url(source, chapter_url)
        self._bg_check(source, abs_url)
        list_cfg = body_cfg or block.get("list") or {}
        root_sel = list_cfg.get("root_selector")
        fields = list_cfg.get("fields") or {}
        if not root_sel or not fields.get("url"):
            return []
        return self._fetch_comic_page_imgs(source, list_cfg, chapter_url)

    def fetch_comic_pages_batch(
        self,
        source: SourceConfig,
        chapter_urls: List[str],
        render_cfg: Optional[dict] = None,
    ) -> dict:
        """批量抓取多话漫画图片（**复用同一个 Chromium 实例**）。

        仅用于 render: playwright 的加密分片源（如 comicbox）。
        把 N 话的 Playwright 渲染收敛到 1 次浏览器启动，大幅降低下载耗时。

        返回 {chapter_url: [base64 data URI 列表]}；单话渲染失败 → 值为 None。
        render_cfg 可显式传入；缺省时从源配置 content.page 解析。
        """
        if not chapter_urls:
            return {}
        content_cfg = self._content_block(source)
        block = content_cfg.get("page") or {}
        body_cfg = block.get("body") or {}
        abs_urls = [self._abs_url(source, u) for u in chapter_urls]
        if render_cfg is None:
            rc = block.get("render_config") or body_cfg.get("render_config") or {}
            render_cfg = {
                "wait_for": rc.get("wait_for", "canvas"),
                "wait_until": rc.get("wait_until", "domcontentloaded"),
                "timeout_ms": rc.get("timeout_ms", 30000),
                "extra_delay_ms": rc.get("extra_delay_ms", 2500),
                "scroll_to_bottom": rc.get("scroll_to_bottom", False),
                "extract_mode": rc.get("extract_mode", "canvas"),
                "proxy": source.transports().get("proxy"),
                "wheel_scroll": bool(rc.get("wheel_scroll", False)),
                "img_selector": rc.get("img_selector"),
                "img_js_path": rc.get("img_js_path"),
            }
        from framework.playwright_helper import fetch_rendered_pages_batch_sync

        raw = fetch_rendered_pages_batch_sync(abs_urls, render_cfg)
        # 以原始 chapter_url 为 key 返回（调用方用 ch.url 直接查）
        return {u: raw.get(a) for u, a in zip(chapter_urls, abs_urls)}
