"""广告过滤引擎（adblock.py）。

通用广告过滤，供下载与播放统一接入。按源配置 ad_block 驱动：
- enabled:         开关（默认 true）
- block_url_regex: 追加 URL 广告特征正则（源级补充）
- block_domains:   追加广告域名黑名单
- extra_css:       追加 HTML 广告节点 CSS 选择器（源级补充）

内置通用启发式（对所有源生效）：
- 域名黑名单：常见广告/统计/推广域名（Google 系、百度统计/推广、
  欧美 DSP/原生广告、移动广告 SDK、中文统计、出海/联盟）→ 精确后缀匹配
- URL 路径特征：路径含 ad/ads/banner/promo/gg/advert/adserver 等明确
  广告词 → 判为广告（路径段边界匹配，防误伤 normal URL）
- query 参数特征：参数名是明确广告词（ad_id/affid/clickid 等）→ 判为广告
  （只查参数名、不查参数值，避免 from=ad 这类误伤）
- HTML 内容层过滤（filter_html / filter_text / is_ad_image_url）：
  广告节点 CSS 选择器剔除、广告文本特征行过滤、广告图片位 URL 识别；
  源级 extra_css 追加选择器（schema §7.5，原先仅文档提及未实现）
- m3u8 广告段剔除：解析 HLS 播放列表，识别广告段
  （#EXT-X-DISCONTINUITY 分隔 + 段 URL 广告特征/异常短时长），
  重写为无广告段的 m3u8 供播放/下载；单行 EXTINF + 段 URL 不再丢失时长。

对应 core.md「广告过滤规则体系」与 source-schema-v2 §7.5 ad_block。
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional
from urllib.parse import parse_qsl, urlparse

try:
    import lxml.html

    _LXML = True
except ImportError:  # pragma: no cover - 无 lxml 时降级为正则文本过滤
    lxml = None  # type: ignore[assignment]
    _LXML = False

# ------------------------------------------------------------------ #
# 内置规则
# ------------------------------------------------------------------ #
# 内置通用广告 URL 路径特征（只匹配明确广告词，路径段边界收紧防误伤：
# /promotional、/adventure 等非广告词不命中）
_DEFAULT_URL_AD_RE = re.compile(
    r"(?:"
    r"/ads?/|/ad-|/ad\.|/ad/|"            # 基础 ad 形式
    r"_ad\.|\.ads\.|/ad_|/adbanner|"      # 下划线/前缀组合
    r"/advert(?:/|-|\.)|/advertis|/adverts/|"  # advertise/advertising
    r"/adserver|/adframe|/adclick|/adview|/adrotate|/adserve|/adtag|/adunit|"
    r"/banner|/promo(?:/|_)|_promo\.|/promotion/|/guanggao/|/tuiguang/|"
    r"/gg/|/gg_|/ggad|/adv/"
    r")",
    re.IGNORECASE,
)
# 内置广告域名黑名单（精确后缀匹配：host == d 或 host.endswith("." + d)）
_DEFAULT_AD_DOMAINS = (
    # —— Google 系广告/统计 ——
    "doubleclick.net",
    "googlesyndication.com",
    "googletagservices.com",
    "googleadservices.com",
    "google-analytics.com",
    "googletagmanager.com",
    "adservice.google",
    "admob.com",
    "2mdn.net",
    # —— 欧美广告交易平台/DSP/原生广告 ——
    "advertising.com",
    "adnxs.com",
    "adsystem.com",
    "amazon-adsystem.com",
    "taboola.com",
    "outbrain.com",
    "smartadserver.com",
    "criteo.com",
    "criteo.net",
    "pubmatic.com",
    "rubiconproject.com",
    "openx.net",
    "casalemedia.com",
    "moatads.com",
    "adsafeprotected.com",
    "adform.net",
    "adroll.com",
    "adcash.com",
    "adsterra.com",
    "popads.net",
    "exoclick.com",
    "mgid.com",
    "revcontent.com",
    "sharethrough.com",
    "media.net",
    "teads.tv",
    "spotxchange.com",
    "brightroll.com",
    # —— 移动广告 SDK ——
    "applovin.com",
    "chartboost.com",
    "unityads.unity3d.com",
    "tapjoy.com",
    "inmobi.com",
    "vungle.com",
    "adcolony.com",
    "smaato.com",
    # —— 中文统计/推广 ——
    "hm.baidu.com",
    "tongji.baidu.com",
    "cpro.baidu.com",
    "pos.baidu.com",
    "union.baidu.com",
    "eclick.baidu.com",
    "cnzz.com",
    "umeng.com",
    "51.la",
)
# 内置 HTML 广告节点 CSS 选择器（filter_html 用；源级 extra_css 追加）
_DEFAULT_AD_CSS = (
    # —— 通用广告容器 class/id ——
    "div.ad", "div.ads", "div.adsbox", "div.advert", "div.advertisement",
    "div.ad-placeholder", "div.ad-container", "div.ad-wrap", "div.ad-wrap-inner",
    "div.ad-content", "div.ad-banner", "div.banner-ad", "div.banner-ads",
    "div.banner-ad-container", "div.ad-unit", "div.ad-slot", "div.ad-area",
    "div.ad-list", "div.ad-footer", "div.ad-header", "div.ad-popup",
    "div.adv", "div.adv-box", "div.gg", "div.gg-box", "div.guanggao",
    "div.tuiguang", "div.promotion", "div.promo", "div.promobox",
    "div.sponsor", "div.sponsored", "div.paid-content",
    # —— id 形式 ——
    "#ad", "#ads", "#advert", "#advertisement", "#ad-box", "#ad-banner",
    "#banner-ad", "#ad-content", "#ad-container", "#ad-slot", "#ad-wrap",
    "#gg", "#guanggao", "#promo", "#promotion", "#sponsor",
    # —— 常见广告网络注入 ——
    "ins.adsbygoogle", "ins.adsbygoogle2",
    "iframe[src*='ad']", "iframe[src*='ads']", "iframe[id*='ad']",
    "iframe[src*='doubleclick']", "iframe[src*='googleads']",
    "iframe[src*='guanggao']",
    "script[src*='adsbygoogle']", "script[src*='pagead2']",
    "script[src*='google-analytics']", "script[src*='hm.baidu.com']",
    "script[src*='cnzz.com']", "script[src*='umeng.com']",
    # —— 原生广告/联盟 ——
    "div.taboola", "div.outbrain", "div[class*='native-ad']",
    "div[class*='nativead']", "div[class*='sponsored-content']",
    # —— 弹窗/浮层广告 ——
    "div[class*='popup-ad']", "div[class*='pop-ad']", "div[class*='float-ad']",
    "div[class*='fixed-ad']", "div[class*='sticky-ad']",
    # —— 中文小说站常见 ——
    "div#ads_1", "div#ads_2", "div.ads_top", "div.ads_bottom",
    "div.ads_middle", "div.ads_content", "div.chapter-ad", "div.reader-ad",
    "div.read-ad", "div.article-ad", "div.text-ad", "div.tj-ad", "div.yy-ad",
    "#adtext", "#adtexts", "#ggtext", "div#ad_3", "div#ad_4", "div.adtxt",
    "div.content-ad", "div.list-ad", "div.footer-ad",
    # —— 通用匹配（稳妥子串，class 含 ad 的容器）——
    "div[class*='advert']", "div[id*='advert']",
    "div[class*='adsense']", "div[id*='adsense']",
)
# 内置广告文本特征行（filter_text 用；逐行匹配，命中即剔除该行）
_AD_TEXT_PATTERNS = (
    # 广告词（前可接行首/括号/空白等非文字符，后可接括号/空白/冒号/行尾）
    re.compile(r"(?:^|[^\w\u4e00-\u9fa5])(?:广告|推广|赞助|福利)(?:[^\u4e00-\u9fa5]|$)"),
    re.compile(r"点击(?:进入|查看|领取|下载|了解|注册|购买)"),
    re.compile(r"(?:微信|公众号|QQ群|加群|加V|客服).{0,8}(?:领取|咨询|了解|下载)"),
    re.compile(r"(?:推荐|热门|精选)(?:小说|漫画|游戏|APP|应用|网站).{0,10}(?:下载|安装|注册|试玩)"),
    re.compile(r"(?:下载|安装).{0,6}(?:APP|应用|客户端|软件)"),
    re.compile(r"(?:扫码|扫一扫).{0,10}(?:关注|下载|领取)"),
    re.compile(r"(?:更多|最新).{0,6}(?:福利|番号|资源).{0,10}(?:关注|查看|点击)"),
    re.compile(r"\[?(?:本书|本站)?(?:由|提供).{0,20}(?:赞助|冠名|友情链接)"),
    re.compile(r"(?:↑|↑↑|↓).{0,8}(?:点击|返回|目录)"),
    re.compile(r"^(?:手机|PC|电脑).{0,4}(?:用户|端).{0,10}(?:阅读|访问|请)"),
    re.compile(r"(?:神马|百度|搜狗|360).{0,6}(?:搜索|浏览器).{0,8}(?:打开|访问|阅读)"),
)
# 内置 query 参数广告特征（只匹配参数名；值含 ad 的 from=/ref= 等不算）
_AD_QUERY_PARAMS = frozenset(
    {
        "ad", "adid", "ad_id", "adsid", "advid", "adv_id",
        "adunit", "adunit_id", "adtag", "adserver", "adtype", "ad_type",
        "ad_source", "ad_channel", "is_ad", "placement_id",
        "aff", "affid", "aff_id", "affiliate", "affiliate_id",
        "clickid", "click_id",
        "banner", "campaignid", "campaign_id",
    }
)


def _has_ad_query(url_low: str) -> bool:
    """query string 中是否存在明确广告参数名（?ad_id=、&affid= 等）。

    用 parse_qsl 解析（keep_blank_values=True 保证 ?ad= 空值也命中），
    只比较参数名，与参数值无关，防误伤。
    """
    q = urlparse(url_low).query
    if not q:
        return False
    return any(
        name.lower() in _AD_QUERY_PARAMS
        for name, _ in parse_qsl(q, keep_blank_values=True)
    )


def _css_to_regex(sel: str) -> Optional[re.Pattern]:
    """把简单 CSS 选择器转成正则（lxml 缺失时的降级剔除）。

    支持：div.class / #id / div#id / tag[attr*='val'] / ins.adsbygoogle。
    不支持复杂选择器时返回 None（跳过该条）。
    """
    s = sel.strip()
    if not s:
        return None
    # tag[attr*='val'] / tag[attr*="val"]
    m = re.fullmatch(r"([a-zA-Z][\w-]*)?\[(\w+)\*?=['\"]([^'\"]+)['\"]\]", s)
    if m:
        tag = m.group(1) or "[a-zA-Z][\w-]*"
        attr = m.group(2)
        val = re.escape(m.group(3))
        return re.compile(
            r"<" + tag + r"[^>]*\b" + attr + r"=['\"][^'\"]*" + val + r"[^'\"]*['\"]"
            + r"[^>]*>.*?</" + tag + r">",
            re.IGNORECASE | re.DOTALL,
        )
    # div.class / tag.class（单 class）
    m = re.fullmatch(r"([a-zA-Z][\w-]*)?\.([\w-]+)", s)
    if m:
        tag = m.group(1) or "[a-zA-Z][\w-]*"
        cls = re.escape(m.group(2))
        return re.compile(
            r"<" + tag + r"[^>]*\bclass=['\"][^'\"]*\b" + cls + r"\b[^'\"]*['\"]"
            + r"[^>]*>.*?</" + tag + r">",
            re.IGNORECASE | re.DOTALL,
        )
    # #id
    m = re.fullmatch(r"#([\w-]+)", s)
    if m:
        i = re.escape(m.group(1))
        return re.compile(
            r"<[a-zA-Z][\w-]*[^>]*\bid=['\"]" + i + r"['\"]"
            + r"[^>]*>.*?</[a-zA-Z][\w-]*>",
            re.IGNORECASE | re.DOTALL,
        )
    # div#id
    m = re.fullmatch(r"([a-zA-Z][\w-]*)#([\w-]+)", s)
    if m:
        tag = m.group(1)
        i = re.escape(m.group(2))
        return re.compile(
            r"<" + tag + r"[^>]*\bid=['\"]" + i + r"['\"]"
            + r"[^>]*>.*?</" + tag + r">",
            re.IGNORECASE | re.DOTALL,
        )
    # 纯标签
    if re.fullmatch(r"[a-zA-Z][\w-]*", s):
        return re.compile(r"<" + s + r"\b[^>]*>.*?</" + s + r">", re.IGNORECASE | re.DOTALL)
    return None


class AdblockEngine:
    """广告过滤引擎。每个源一个实例（读该源的 ad_block 配置）。"""

    def __init__(self, source=None):
        self._block_re = _DEFAULT_URL_AD_RE
        self._block_domains = list(_DEFAULT_AD_DOMAINS)
        self._enabled = True
        self._extra_regexes: List[re.Pattern] = []
        self._extra_domains: List[str] = []
        if source is not None:
            self.configure(source)

    # ------------------------------------------------------------------ #
    def configure(self, source) -> None:
        """从源配置读 ad_block，构建过滤规则。"""
        # 重置补充规则：同一实例被 configure 多次时不累积旧规则
        self._extra_regexes = []
        self._extra_domains = []
        raw = getattr(source, "raw", None) or {}
        ad = raw.get("ad_block") or {}
        self._enabled = bool(ad.get("enabled", True))
        if not self._enabled:
            return
        # 源级补充 URL 正则
        for pat in ad.get("block_url_regex") or []:
            try:
                self._extra_regexes.append(re.compile(pat, re.IGNORECASE))
            except re.error:
                continue
        # 源级补充域名黑名单
        for dom in ad.get("block_domains") or []:
            d = str(dom).strip().lstrip("*.")
            if d:
                self._extra_domains.append(d)
        # 源级补充 HTML 广告节点 CSS 选择器（schema §7.5 extra_css）
        self._extra_css: List[str] = []
        for sel in ad.get("extra_css") or []:
            s = str(sel).strip()
            if s:
                self._extra_css.append(s)

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------ #
    def is_ad_url(self, url: str) -> bool:
        """判断单个 URL 是否广告（域名黑名单 + 路径特征 + query 特征 + 源级补充）。"""
        if not self._enabled or not url:
            return False
        low = url.lower()
        parsed = urlparse(low)
        host = parsed.hostname or ""
        # 域名黑名单（精确后缀匹配：adservice.google 命中 adservice.google.com，
        # 但不误伤 myadsystem.com / amazon-adsystem.com）
        for dom in self._block_domains + self._extra_domains:
            d = dom.lower()
            if host == d or host.endswith("." + d):
                return True
        # 内置 URL 路径特征
        if self._block_re.search(parsed.path):
            return True
        # 内置 query 参数广告特征（仅参数名命中，防误伤）
        if _has_ad_query(low):
            return True
        # 源级补充正则
        for p in self._extra_regexes:
            if p.search(low):
                return True
        return False

    def filter_urls(self, urls: List[str]) -> List[str]:
        """过滤一批 URL，剔除广告。"""
        if not self._enabled:
            return urls
        return [u for u in urls if not self.is_ad_url(u)]

    # ------------------------------------------------------------------ #
    def _ad_css_selectors(self) -> List[str]:
        """内置 + 源级 extra_css 合并后的广告节点选择器列表。"""
        return list(_DEFAULT_AD_CSS) + list(getattr(self, "_extra_css", []) or [])

    def filter_html(self, html: str) -> str:
        """从 HTML 中剔除广告节点（广告容器/iframe/脚本/图片位）。

        优先用 lxml（可靠），缺失时降级为正则剔除（尽力而为）。
        返回过滤后的 HTML；未启用/无内容时原样返回。
        """
        if not self._enabled or not html:
            return html
        css = self._ad_css_selectors()
        if not css:
            return html
        if _LXML:
            try:
                doc = lxml.html.fromstring(html)
                for sel in css:
                    try:
                        for node in doc.cssselect(sel):
                            parent = node.getparent()
                            if parent is not None:
                                parent.remove(node)
                    except Exception:
                        continue
                return lxml.html.tostring(doc, encoding="unicode")
            except Exception:
                pass  # 解析失败 → 降级正则
        # 正则降级：逐选择器转为粗粒度标签剔除
        out = html
        for sel in css:
            pat = _css_to_regex(sel)
            if pat:
                out = pat.sub("", out)
        return out

    def filter_text(self, text: str) -> str:
        """过滤正文文本中的广告行（按行匹配内置特征，命中即删行）。

        对每行做 trim 后匹配，保留非广告行。用于正文已是纯文本的场景
        （如已 strip 过 HTML 的章节正文）。
        """
        if not self._enabled or not text:
            return text
        lines = text.splitlines()
        kept = [
            ln for ln in lines
            if not any(p.search(ln) for p in _AD_TEXT_PATTERNS)
        ]
        return "\n".join(kept)

    def is_ad_image_url(self, url: str) -> bool:
        """判断图片 URL 是否广告图片位（is_ad_url + 常见广告图特征）。

        在 is_ad_url 基础上追加：URL 含广告位占位词（banner/ads/gg/guanggao）
        且为图片扩展名 → 判广告。供漫画/图片列表过滤用。

        占位词必须作为**路径段边界**出现（前一位是 / 或 . 等非字母），
        避免误伤 upload/download 等词尾部恰好是 "ad" 的正常图片路径。
        """
        if not self._enabled or not url:
            return False
        if self.is_ad_url(url):
            return True
        low = url.lower()
        if not re.search(r"\.(?:jpe?g|png|gif|webp|avif)(?:\?|$)", low):
            return False
        return bool(re.search(r"(?:^|[^a-z])(?:banner|adverts?|ads?|gg|guanggao|tuiguang|promo)[-_/.]", low))

    # ------------------------------------------------------------------ #
    def filter_m3u8(self, m3u8_text: str, base_url: str = "") -> str:
        """剔除 m3u8 里的广告段，返回重写后的播放列表。

        通用启发式：
        1. 段 URL 命中广告特征（is_ad_url）→ 剔除该段
        2. 广告段通常紧跟在 #EXT-X-DISCONTINUITY 之后且时长异常短（< 3s）→ 剔除
        剔除时连同其前后的 DISCONTINUITY 标记一并清理，保持列表合法。
        """
        ad_segs, cleaned = self.detect_m3u8_ads(m3u8_text, base_url)
        return cleaned if ad_segs else m3u8_text
    def detect_m3u8_ads(self, m3u8_text: str, base_url: str = "") -> tuple:
        """检测 m3u8 流内广告段（供加任务队列时标记）。

        返回 (ad_seg_indices, cleaned_text)：
        - ad_seg_indices: 被判定为广告的段序号列表（0 起，与解析到的段一一对应）
        - cleaned_text:   剔除广告段后的播放列表（无广告时与原文本相同）

        判定启发式（与 filter_m3u8 一致）：
        1. 段 URL 命中广告特征（is_ad_url）→ 广告
        2. 同一段 URL 在列表中重复出现 ≥2 次 → 该 URL 全部段判广告
           （正常 VOD 每个分片 URL 唯一；广告素材常被重复插入同一列表，
           如 xuandm 的片头/片尾推广块，2026-08 实测）
        3. 被 #EXT-X-DISCONTINUITY 分隔的**孤立短块**（块内段总时长 < 3s）→ 广告
           （替代旧「DISCONTINUITY 后单段 <3s」：旧规则会误删长正常块开头的
           正常短段——xuandm 实测 seg96 2s 是正片第一段却被删）
        """
        if not self._enabled or not m3u8_text:
            return [], m3u8_text
        from urllib.parse import urljoin

        lines = m3u8_text.splitlines()
        n = len(lines)

        # ---- 第一遍：解析段信息 + 统计段 URL 出现次数（重复段检测） ----
        seg_infos: List[tuple] = []  # (joined_url, dur)
        url_counter: dict = {}
        pending_dur: Optional[float] = None
        for line in lines:
            m = re.match(r"#EXTINF:\s*([\d.]+)", line)
            if m:
                pending_dur = float(m.group(1))
                continue
            if line and not line.startswith("#"):
                joined = urljoin(base_url, line) if base_url else line
                seg_infos.append((joined, pending_dur))
                url_counter[joined] = url_counter.get(joined, 0) + 1
                pending_dur = None
        dup_urls = {u for u, c in url_counter.items() if c >= 2}

        # ---- 第二遍：按 DISCONTINUITY 划分块，计算块级特征 ----
        # 块 = 一段连续段序列；块前紧邻 DISCONTINUITY 视为「独立块」。
        # 独立块且块内段总时长 <3s → 块内所有段判广告（孤立短块）。
        # 列表开头的块（前无 DISCONTINUITY）不判——整列表即短视频时不受影响。
        blocks: List[dict] = []
        cur: Optional[dict] = None
        seg_idx = -1
        for line in lines:
            if line.startswith("#EXT-X-DISCONTINUITY"):
                if cur is not None and cur["seg"]:
                    blocks.append(cur)
                cur = {"seg": [], "dur": 0.0, "independent": True}
                continue
            m = re.match(r"#EXTINF:\s*([\d.]+)", line)
            if m:
                pending_dur = float(m.group(1))
                continue
            if line and not line.startswith("#"):
                seg_idx += 1
                if cur is None:
                    cur = {"seg": [], "dur": 0.0, "independent": False}
                dur = pending_dur
                pending_dur = None
                cur["seg"].append(seg_idx)
                cur["dur"] += dur if dur is not None else 0.0
        if cur is not None and cur["seg"]:
            blocks.append(cur)
        short_block_segs: set = set()
        for b in blocks:
            if b["independent"] and b["dur"] < 3.0:
                short_block_segs.update(b["seg"])

        # ---- 第三遍：重写输出，剔除广告段 ----
        ad_set: set = set()
        # R1: URL 广告特征
        for i0, (joined, _dur) in enumerate(seg_infos):
            if self.is_ad_url(joined):
                ad_set.add(i0)
        # R2: 重复段（同一 URL ≥2 次）
        for i0, (joined, _dur) in enumerate(seg_infos):
            if joined in dup_urls:
                ad_set.add(i0)
        # R3: DISCONTINUITY 分隔的孤立短块
        ad_set |= short_block_segs

        out: List[str] = []
        ad_segs: List[int] = []
        i = 0
        seg_idx = -1
        pending_dur = None
        while i < n:
            line = lines[i]
            m = re.match(r"#EXTINF:\s*([\d.]+)", line)
            if m:
                pending_dur = float(m.group(1))
            if line and not line.startswith("#"):
                seg_idx += 1
                pending_dur = None  # 消费：EXTINF 时长只属于紧随的段
                if seg_idx in ad_set:
                    ad_segs.append(seg_idx)
                    # 回退：删掉刚写入的本段元数据行。只回退属于「当前段」的
                    # EXTINF / DISCONTINUITY / BYTERANGE / 广告块专用 KEY:NONE；
                    # 保留带加密信息的 KEY / MAP——它们是后续所有段共用的加密/
                    # 初始化信息，随广告段一并删除会让 key rotation / fMP4 场景
                    # 的后续段解密失败。KEY:METHOD=NONE 是无加密声明，仅广告块
                    # 独有，删广告段时一并清理（xuandm 广告块 KEY:NONE 实测）。
                    while out and (
                        out[-1].startswith("#EXTINF")
                        or out[-1].startswith("#EXT-X-DISCONTINUITY")
                        or out[-1].startswith("#EXT-X-BYTERANGE")
                        or out[-1].startswith("#EXT-X-KEY:METHOD=NONE")
                    ):
                        out.pop()
                    # 跳过本段及后续紧邻的 DISCONTINUITY
                    i += 1
                    while i < n and lines[i].startswith("#EXT-X-DISCONTINUITY"):
                        i += 1
                    continue
            out.append(line)
            i += 1
        result = "\n".join(out)
        # 无广告剔除：原样返回原始字符串（保持逐字一致，调用方据此判断是否发生过过滤）。
        # 有广告才返回重写结果；重写时保留输入尾部换行。
        if not ad_segs:
            return [], m3u8_text
        if m3u8_text.endswith("\n") and not result.endswith("\n"):
            result += "\n"
        return ad_segs, result


# ------------------------------------------------------------------ #
# 便捷单例（无源配置时用内置规则）
# ------------------------------------------------------------------ #
_default_engine: Optional[AdblockEngine] = None


def adblock_for(source=None) -> AdblockEngine:
    """取某源的广告引擎（读源 ad_block 配置）。缺省用内置规则。"""
    if source is not None:
        return AdblockEngine(source)
    global _default_engine
    if _default_engine is None:
        _default_engine = AdblockEngine()
    return _default_engine
