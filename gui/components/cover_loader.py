"""封面/正文图异步加载器（CoverLoader）。

共享单个 QNetworkAccessManager + 并发限流（同时最多 MAX_CONCURRENT 个请求），
避免每个 WorkCard 独立发请求导致网络拥塞卡顿。图片下载优先走框架
HttpClient.get_bytes（CF cookie 注入 / impersonate / 代理池换 IP / 重试 / 反爬识别），
失败自动回退 QNetworkAccessManager 原路径，保留既有重试/代理/缓存/AES 解密语义。

- 支持系统代理（Clash 等，与 Playwright 一致）
- 按图片域名推导 Referer（部分图床要求 Referer 同源）；显式章节 URL 优先
- 可选注入共享 HttpClient（App 层按 settings 构建），未注入用模块级惰性单例

用法：
    loader = CoverLoader.instance()
    loader.load(url, callback)   # callback(pixmap) 加载完成（可能为 None）
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Callable, List, Optional
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QThreadPool, QTimer, QUrl, QRunnable, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkProxy,
    QNetworkReply,
    QNetworkRequest,
)

from framework.playwright_helper import _system_proxy

log = logging.getLogger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
MAX_CONCURRENT = 16  # 同时最多 16 个封面请求（提升一页 20 张封面加载速度）
REQUEST_TIMEOUT_MS = 20000  # 请求超时 20s：图床(file2.acgnngca.com)延迟抖动大(中位数~2.5s,尾部>8s)，8s 会把个别慢请求误判失败
MAX_RETRIES = 2  # 单图失败最多再重试 2 次（保守，避免放大图床压力）
RETRY_DELAY_MS = 300  # 失败重试短退避，避开瞬时抖动

# 图片 CDN → 所属主站（用于 Referer）。命中后按主站域名回填 Referer。
_REFERER_RULES = (
    # comicbox 图床 → 主站
    ("ccavbox.com", "https://www.comicbox.xyz/"),
    # B 站封面 CDN → B 站（防风控，部分环境需 Referer 才放行）
    ("hdslb.com", "https://www.bilibili.com/"),
    # manben 漫画图床 → 满本网站点根（发现页/详情封面）。
    # 正文图（manhua*.cdndm5.com）校验精确章节页 Referer，由 ComicView
    # 显式传入章节 URL 覆盖此处（_infer_referer 仅是兜底）。
    ("cdndm5.com", "https://www.manben.com/"),
    # ho5ho 图床（Bunny CDN，ho5hocdn1/hhmg2.b-cdn.net）→ 站点根，
    # 封面与正文图均校验 Referer，无 Referer 时 403。
    ("b-cdn.net", "https://www.ho5ho.com/"),
)


def _infer_referer(url: str) -> Optional[str]:
    """按图片 URL 推导 Referer。外链图床需要同源 Referer 才放行。"""
    host = (urlsplit(url).hostname or "").lower()
    for img_domain, referer in _REFERER_RULES:
        if img_domain in host:
            return referer
    return None


def _decode_pixmap(data: bytes, source=None):
    """字节解码为 QPixmap；加密站封面（源配置 decryption.image）二次解密再试。

    普通图（JPEG/PNG 等）直接解码成功；失败且源的 decryption.image 声明了
    AES 解密时，按缓存密钥解密后再解码（91pornacomic 类私图床封面字节加密）。
    返回 (像位图, 用于持久化的字节)；无 source / 解密失败时原字节透传，
    像位图为 None（调用方按失败处理，不阻塞封面展示）。
    """
    p = QPixmap()
    if p.loadFromData(data) and not p.isNull():
        return p, data
    if source is None:
        return None, data
    try:
        from framework.decrypter import Decrypter

        plain = Decrypter(None).decrypt_bytes(source, data, target="image")
        if plain and plain is not data:
            p2 = QPixmap()
            if p2.loadFromData(plain) and not p2.isNull():
                return p2, plain
    except Exception:  # noqa: BLE001
        pass
    return None, data


def fade_in(widget: "QWidget") -> None:
    """封面淡入动画：从透明到不透明，让异步加载过程可见。

    加载完成后图片从 0 透明度淡入到 255，视觉上明确"刚加载出来"。
    动画对象绑定在 widget 属性上防 GC；失败静默（不影响显示）。
    """
    try:
        from PySide6.QtCore import QPropertyAnimation, QEasingCurve
        from PySide6.QtWidgets import QGraphicsOpacityEffect

        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
        effect.setOpacity(0.0)
        widget.setProperty("_fade_effect", effect)  # 持引用防 GC
        anim = QPropertyAnimation(effect, b"opacity", widget)
        anim.setDuration(260)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        widget.setProperty("_fade_anim", anim)
        anim.finished.connect(lambda: widget.setGraphicsEffect(None))
        anim.start()
    except Exception:  # noqa: BLE001
        pass  # 动画失败不影响封面显示


# ------------------------------------------------------------------ #
# 模块级共享 HttpClient：惰性创建，App/测试可经 _CoverLoader.use_http 注入覆盖
# ------------------------------------------------------------------ #
_SHARED_HTTP = None
_SHARED_HTTP_LOCK = threading.Lock()


def _shared_http_client():
    """模块级 HttpClient 单例（首次需要时创建）。

    让正文图/封面下载默认走框架反爬链（CF cookie 注入 / impersonate / 代理池 /
    重试 / 反爬识别）；App 层注入的是按 settings 构建的共享实例（见 use_http），
    未注入时资源自给自足，不受 GUI 生命周期约束。
    """
    global _SHARED_HTTP
    if _SHARED_HTTP is None:
        with _SHARED_HTTP_LOCK:
            if _SHARED_HTTP is None:
                from framework.http import HttpClient

                _SHARED_HTTP = HttpClient()
    return _SHARED_HTTP


class _HttpFetchSignals(QObject):
    """http.get_bytes 完成信号（worker 线程 emit → 主线程队列消费）。"""

    finished = Signal(object, object)  # (item, data_bytes)；失败/空字节同样回传


class _HttpFetchTask(QRunnable):
    """后台线程执行一次 HttpClient.get_bytes（一次图片请求）。

    请求头在 worker 内组装：UA + 精确 Referer（显式章节 URL 优先，域名规则兜底），
    HttpClient 自动补 CF cookie / UA 轮换 / 重试 / 反爬识别。异常不抛回 UI，
    回传空字节让主线程统一回退 QNAM 原路径。
    """

    def __init__(self, item, http, proxy, timeout, direct=False):
        super().__init__()
        self.item = item
        self.http = http
        self.proxy = proxy
        self.timeout = timeout
        self.direct = direct
        self.signals = _HttpFetchSignals()

    def run(self) -> None:
        url, _cb, referer, _rl, _c, _p, _sid, _src, _uh = self.item
        headers = {"User-Agent": _BROWSER_UA}
        referer = referer or _infer_referer(url)
        if referer:
            headers["Referer"] = referer
        try:
            data = self.http.get_bytes(
                url, headers=headers, proxy=self.proxy,
                timeout=self.timeout, retries=1, direct=self.direct,
            )
        except Exception:  # noqa: BLE001 —— 失败回退 QNAM，由主线程统一处理
            data = b""
        try:
            self.signals.finished.emit(self.item, data or b"")
        except RuntimeError:
            pass  # 加载器对象已销毁，静默


class _CoverLoader(QObject):
    """单例封面加载器。"""

    def __init__(self):
        super().__init__()
        self._manager = QNetworkAccessManager(self)
        self._manager.finished.connect(self._on_reply)
        self._manager_direct = QNetworkAccessManager(self)  # 无代理 fallback manager
        self._manager_direct.finished.connect(self._on_direct_reply)
        self._queue: List[tuple] = []
        self._pending: dict = {}  # reply → (callback, url, used_proxy, referer)
        self._direct_pending: dict = {}  # direct reply → callback
        self._active = 0
        self._proxy_set = False
        self._proxy_url: Optional[str] = None
        # 系统代理探测结果（后台线程预取，避免首屏封面请求在主线程同步
        # socket 探测 3×150ms 阻塞 UI——书架/发现页首开明显卡顿的来源之一）。
        self._proxy_probe_done = False
        self._proxy_probe = None
        self._proxy_probe_lock = threading.Lock()
        threading.Thread(target=self._probe_proxy, daemon=True).start()
        # 注入的 HttpClient（None = 用模块级共享实例）。下载优先走 get_bytes。
        self._http = None
        # 源 id → SourceConfig：供封面/正文图下载透传源级 direct（列表回填/搜索
        # 回填只传 source_id 不传 source，若不补映射则 direct 丢失 → 直连源封面
        # 走系统代理，代理不稳时封面空白）。App 启动时经 register_source 填充。
        self._sources: dict = {}
        # 内存封面缓存（LRU，字节预算来自设置 cover_cache_size_mb）
        self._cache: "OrderedDict[str, QPixmap]" = OrderedDict()
        self._cache_bytes = 0
        self._cache_budget = 0  # 0 = 关闭缓存
        # Redis 书架封面缓存（可选，None=禁用）。键：cover:{source_id}:{url}（永久，shelf 池）
        self._shelf_cache = None

    def configure(self, cache_mb: float | int = 0, shelf_cache=None) -> None:
        """设置缓存字节预算（MB）。0 关闭内存缓存。启动时调用一次。

        shelf_cache：可选 RedisLikeStore（书架封面持久化）。None 禁用。
        """
        self._cache_budget = max(0, int(cache_mb or 0)) * 1024 * 1024
        self._shelf_cache = shelf_cache
        self._trim_cache()

    def use_http(self, http) -> None:
        """注入共享 HttpClient：正文图/封面下载优先走 get_bytes（框架反爬链）。

        App 启动时传入按 settings 构建的 HttpClient（含 impersonate/user_agents
        接线）；None = 使用模块级惰性共享实例。线程安全：仅存引用，下载时读取。
        """
        self._http = http

    def register_source(self, source) -> None:
        """登记源配置（source_id → SourceConfig），供仅传 source_id 的加载
        请求补出源级 direct / AES 解密等策略。App 启动/源列表变化时调用。
        线程安全：仅写引用，下载 worker 只读。
        """
        if source is None:
            return
        sid = getattr(source, "source_id", "") or ""
        if sid:
            self._sources[sid] = source

    def _resolve_source(self, source, source_id: str):
        """取生效源配置：显式 source 优先，否则按 source_id 查注册表（direct 透传）。"""
        if source is not None:
            return source
        if source_id:
            return self._sources.get(source_id)
        return None

    def clear_cache(self) -> None:
        """清空内存封面缓存。"""
        self._cache.clear()
        self._cache_bytes = 0

    def busy(self) -> bool:
        """是否处于批量加载中（队列非空或请求仍在飞）。

        首屏批量封面会触发大量异步回调，此时逐张做淡入动画会让一大批
        QGraphicsOpacityEffect 同时生效（强制离屏渲染 + 动画风暴），是
        开启页面卡顿的重要来源。调用方可用本方法抑制批量淡入：仅当
        **非忙碌**（单张后补等）时才播动画，批量加载直接显示。
        """
        return bool(self._queue) or self._active > 1

    def _cache_get(self, url: str) -> Optional[QPixmap]:
        """LRU 命中：移到尾部（最新）。"""
        pm = self._cache.get(url)
        if pm is not None:
            self._cache.move_to_end(url)
            return pm
        return None

    def _cache_put(self, url: str, pixmap: QPixmap) -> None:
        if self._cache_budget <= 0 or not url or pixmap.isNull():
            return
        old = self._cache.get(url)
        if old is not None:
            self._cache_bytes -= self._pixmap_bytes(old)
        self._cache[url] = pixmap
        self._cache_bytes += self._pixmap_bytes(pixmap)
        self._trim_cache()

    @staticmethod
    def _pixmap_bytes(pm: QPixmap) -> int:
        try:
            img = pm.toImage()
            return img.byteCount() if not img.isNull() else 0
        except Exception:  # noqa: BLE001
            return 0

    def _trim_cache(self) -> None:
        """按字节预算 LRU 淘汰（删最久未用）。"""
        while self._cache_bytes > self._cache_budget > 0 and self._cache:
            _, pm = self._cache.popitem(last=False)
            self._cache_bytes -= self._pixmap_bytes(pm)

    def _probe_proxy(self) -> None:
        """后台线程探测系统代理（仅写线程安全字段，不碰 Qt 对象）。"""
        try:
            url = _system_proxy()
            with self._proxy_probe_lock:
                self._proxy_probe = url
                self._proxy_probe_done = True
        except Exception:  # noqa: BLE001
            with self._proxy_probe_lock:
                self._proxy_probe_done = True

    def _ensure_proxy(self) -> None:
        """QNetwork 走系统代理（与 Playwright 一致），避免 Clash 下直连超时。

        代理探测在后台线程预取（见 __init__），这里只做已就绪结果的落地；
        线程尚未完成时（极端：首屏请求早于探测）同步探测兜底，不阻塞常规路径。
        记录 _proxy_url 供失败回退直连判断。
        """
        if self._proxy_set:
            return
        with self._proxy_probe_lock:
            ready = self._proxy_probe_done
            proxy_url = self._proxy_probe
        if not ready:
            proxy_url = _system_proxy()  # 兜底：后台结果未就绪时同步探测
        if proxy_url:
            parts = urlsplit(proxy_url)
            if parts.hostname:
                proxy = QNetworkProxy(
                    QNetworkProxy.HttpProxy,
                    parts.hostname,
                    parts.port or 7890,
                )
                self._manager.setProxy(proxy)
                self._proxy_url = proxy_url
                log.info("[cover] 走代理 %s", proxy_url)
        self._proxy_set = True

    # ------------------------------------------------------------------ #
    def load(
        self,
        url: str,
        callback: Callable[[Optional[QPixmap]], None],
        referer: Optional[str] = None,
        cache: bool = True,
        persist: bool = False,
        source_id: str = "",
        source=None,
    ) -> None:
        """异步加载图片。

        referer: 可选，精确 Referer（漫画正文图 = 章节页 URL，图床校验精确页）。
        为空时按图片域名从 _REFERER_RULES 推导兜底。收敛到 HttpClient 后显式
        referer 仍优先于域名兜底。
        cache: 是否进 LRU 内存缓存。封面默认 True；漫画正文长图传 False——
        正文图流式阅读、重看概率低，进共享 LRU 会挤掉封面缓存且内存占用大。
        persist：是否查/写 Redis 书架封面持久化（cover: 键，永久）。True 时
        内存 miss 后先查 Redis（重启后免下载），网络成功下载后写回 Redis。
        source_id: 所属源 id。Redis 持久化键含 source_id，避免不同源引用同一
        图片 URL 时互相覆盖/误命中（cover:{source_id}:{url}）。
        source: 可选源配置（SourceConfig）。封面字节无法直接解码且源配置了
        decryption.image 时，按该配置 AES 解密后再解码（私图床加密封面）。
        仅传 source_id 时按注册表（register_source）补源配置，使源级
        transports.direct 对封面/正文图下载同样生效（列表回填/搜索回填场景）。
        """
        if not url:
            callback(None)
            return
        source = self._resolve_source(source, source_id)
        if cache:
            cached = self._cache_get(url)
            if cached is not None:
                callback(cached)
                return
        # Redis 持久化封面：命中直接构造 QPixmap（内存 miss 后/重启首开免下载）。
        # 优先级：字节内不携带站点，加密站旧缓存存的是密文 → 同样走解密通道。
        if persist and self._shelf_cache is not None:
            try:
                data = self._shelf_cache.get(self._shelf_key(source_id, url))
            except Exception:  # noqa: BLE001
                data = None
            if data is not None:
                p, _ = _decode_pixmap(data, source)
                if p is not None and not p.isNull():
                    if cache:
                        self._cache_put(url, p)
                    callback(p)
                    return
        self._queue.append((url, callback, referer, MAX_RETRIES, cache, persist, source_id, source, True))
        self._pump()

    @staticmethod
    def _shelf_key(source_id: str, url: str) -> str:
        """书架封面持久化键：含 source_id，跨源同 URL 不互扰。"""
        return f"cover:{source_id}:{url}"

    def _pump(self) -> None:
        self._ensure_proxy()
        while self._active < MAX_CONCURRENT and self._queue:
            item = self._queue.pop(0)
            self._active += 1
            # 优先走框架 HttpClient（CF cookie 注入 / impersonate / 代理池 / 重试 /
            # 反爬识别）；仅当该请求显式禁用 http（QNAM 回退）时才走原路径。
            if item[8]:
                http = self._http if self._http is not None else _shared_http_client()
                self._dispatch_http(item, http)
            else:
                self._dispatch_qnam(item)

    def _dispatch_http(self, item, http) -> None:
        """用 HttpClient.get_bytes 异步下载（QThreadPool worker + 信号回主线程）。

        主线程只做收尾（解码 / 缓存 / 回调）；构造参数一次性捕获系统代理与超时，
        worker 内只读，避免线程间共享可变状态。
        """
        _src = item[7]  # source（SourceConfig | None）
        direct = bool(_src.transports().get("direct")) if _src else False
        task = _HttpFetchTask(
            item,
            http,
            proxy=(None if direct else self._proxy_url),
            timeout=REQUEST_TIMEOUT_MS / 1000.0,
            direct=direct,
        )
        task.signals.finished.connect(self._on_http_result)
        QThreadPool.globalInstance().start(task)

    def _dispatch_qnam(self, item) -> None:
        """原始 QNetworkAccessManager 下载路径（HTTP 回退 / 未注入 HttpClient）。

        保留代理请求 → 无代理重试 → 退避重试的既有链，行为与收敛前一致。
        """
        url, callback, referer, retries_left, cache, persist, source_id, source, _uh = item
        request = QNetworkRequest(QUrl(url))
        request.setHeader(QNetworkRequest.UserAgentHeader, _BROWSER_UA)
        request.setTransferTimeout(REQUEST_TIMEOUT_MS)  # 超时，防卡队列
        referer = referer or _infer_referer(url)
        if referer:
            request.setRawHeader(b"Referer", referer.encode("utf-8"))
        # 用属性存回调 + 代理标记，reply 完成后取出
        reply = self._manager.get(request)
        used_proxy = self._proxy_url is not None
        self._pending[reply] = (callback, url, used_proxy, referer, retries_left, cache, persist, source_id, source)

    def _on_http_result(self, item, data) -> None:
        """HttpClient 下载结果回到主线程。

        成功字节走既有解码（含 AES 解密）/ 缓存 / 书架持久化链；失败（异常 /
        空字节 / 解码失败）回退 QNAM 原路径重试（use_http=False），不引入崩溃。
        """
        url, callback, referer, retries_left, cache, persist, source_id, source, _uh = item
        self._active -= 1
        pixmap = None
        raw_data = b""
        if data:
            p, store_bytes = _decode_pixmap(data, source)
            if p is not None:
                pixmap = p
                raw_data = store_bytes or data
        if pixmap is not None:
            if cache:
                self._cache_put(url, pixmap)
            # 网络下载成功 → 写 Redis 持久化封面（书架封面持久化转载）
            if raw_data and self._shelf_cache is not None:
                try:
                    self._shelf_cache.set(self._shelf_key(source_id, url), raw_data)
                except Exception:  # noqa: BLE001
                    pass
            if callback:
                callback(pixmap)
            self._pump()
            return
        # http 失败/解码失败 → 回退 QNAM 原路径（use_http=False 防 http 死循环）
        if url and callback is not None:
            self._queue.append(
                (url, callback, referer, retries_left, cache, persist, source_id, source, False)
            )
        self._pump()

    def _on_reply(self, reply: QNetworkReply) -> None:
        callback, url, used_proxy, referer, retries_left, cache, persist, source_id, source = self._pending.pop(
            reply, (None, "", False, None, 0, True, False, "", None)
        )
        self._active -= 1
        pixmap = None
        raw_data = b""
        try:
            if reply.error() == QNetworkReply.NoError:
                data = reply.readAll()
                raw_data = bytes(data)  # readAll() 只能取一次，先保存供持久化写
                p, store_bytes = _decode_pixmap(raw_data, source)
                if p is not None:
                    pixmap = p
                    raw_data = store_bytes or raw_data  # 加密封面持久化解密后字节
        except Exception:
            pixmap = None
        reply.deleteLater()
        # 代理请求失败 → 用无代理 manager 异步重试（不阻塞 UI；直接改代理会丢 fallback）
        if pixmap is None and used_proxy and url:
            if callback is not None:
                req2 = QNetworkRequest(QUrl(url))
                req2.setHeader(QNetworkRequest.UserAgentHeader, _BROWSER_UA)
                req2.setTransferTimeout(REQUEST_TIMEOUT_MS)
                referer = referer or _infer_referer(url)
                if referer:
                    req2.setRawHeader(b"Referer", referer.encode("utf-8"))
                r2 = self._manager_direct.get(req2)
                self._direct_pending[r2] = (callback, url, referer, retries_left, cache, persist, source_id, source)
            self._pump()
            return
        # 未走代理也失败 → 同样短退避重试（有上限），重试耗尽才判失败
        if pixmap is None and url and callback is not None and retries_left > 0:
            self._retry_later(url, callback, referer, retries_left - 1, cache, persist, source_id, source)
            self._pump()
            return
        if pixmap is not None and url and cache:
            self._cache_put(url, pixmap)
        # 网络下载成功 → 写 Redis 持久化封面（书架封面持久化转载）
        if pixmap is not None and url and raw_data and self._shelf_cache is not None:
            try:
                self._shelf_cache.set(self._shelf_key(source_id, url), raw_data)
            except Exception:  # noqa: BLE001
                pass
        if callback:
            callback(pixmap)
        self._pump()

    def _on_direct_reply(self, reply: QNetworkReply) -> None:
        """无代理 fallback 完成。"""
        callback, url, referer, retries_left, cache, persist, source_id, source = self._direct_pending.pop(
            reply, (None, "", None, 0, True, False, "", None)
        )
        pixmap = None
        raw_data = b""
        try:
            if reply.error() == QNetworkReply.NoError:
                data = reply.readAll()
                raw_data = bytes(data)
                p, store_bytes = _decode_pixmap(raw_data, source)
                if p is not None:
                    pixmap = p
                    raw_data = store_bytes or raw_data
        except Exception:
            pixmap = None
        reply.deleteLater()
        # 直连也失败 → 短退避后重新走代理路径重试（图床延迟抖动大，个别超时不代表永久失败）
        if pixmap is None and url and callback is not None and retries_left > 0:
            self._retry_later(url, callback, referer, retries_left - 1, cache, persist, source_id, source)
            self._pump()
            return
        # 网络成功 → 顺带写 Redis 持久化封面（低频 direct fallback，命中复用好）
        if pixmap is not None and url and raw_data and self._shelf_cache is not None:
            try:
                self._shelf_cache.set(self._shelf_key(source_id, url), raw_data)
            except Exception:  # noqa: BLE001
                pass
        # 保守不缓存 direct fallback（低频）。仅调回。
        if callback:
            callback(pixmap)
        self._pump()

    def _retry_later(self, url, callback, referer, retries_left, cache=True, persist=False, source_id="", source=None) -> None:
        """失败重试：短退避(RETRY_DELAY_MS)后把 URL 重新入队，仍在 MAX_CONCURRENT 限流内。

        主线程安全：QTimer.singleShot 在主线程事件循环触发，重试调度匹配现有结构。
        """
        def _do() -> None:
            self._queue.append((url, callback, referer, retries_left, cache, persist, source_id, source, True))
            self._pump()
        QTimer.singleShot(RETRY_DELAY_MS, _do)


class CoverLoader:
    """对外门面（单例）。"""

    _instance: Optional[_CoverLoader] = None

    @classmethod
    def instance(cls) -> _CoverLoader:
        if cls._instance is None:
            cls._instance = _CoverLoader()
        return cls._instance
