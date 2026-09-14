"""视频源 single_chapter + list 分集解析测试（framework 层，不动源配置）。

针对 91porna/hciyuan/badnews 类单视频详情页配 list 的场景：
- single_chapter=true 且配置 list（如 hciyuan 合集帖的 WordPress 分页）：
  list 逐 item 解析出多集（root 每匹配一个元素 = 一个分集），
  first_page_is_current 把详情页自身（<span class="current"> 无 href）补入第 1 集，
  当前视频置首 → 分集列表完整、不错播。
- single_chapter=true 但页面有 list 选择器无匹配 → 回退唯一分集（详情 URL）。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

PAGINATED_HTML = """<html><body>
<h1>『合集｜8话』Overflow</h1>
<div class="page-links">
  <span class="post-page-numbers current">1</span>
  <a class="post-page-numbers" href="/2026/01/coll/2/">2</a>
  <a class="post-page-numbers" href="/2026/01/coll/3/">3</a>
  <span class="dot">…</span>
  <a class="post-page-numbers" href="/2026/01/coll/8/">8</a>
</div>
</body></html>"""

PLAIN_PAGE_HTML = """<html><body>
<h1>OVA 里番 ＃3 催眠女友</h1>
<div class="entry-content">正文</div>
</body></html>"""

LIST_CFG = {
    "single_chapter": True,
    "list": {
        "first_page_is_current": True,
        "root_selector": {"css": "div.page-links a.post-page-numbers"},
        "chapter_order": "asc",
        "fields": {
            "title": {"css": "a.post-page-numbers"},
            "url": {"css": "a.post-page-numbers", "attr": "href"},
        },
    },
}


class _Http:
    def __init__(self, html):
        self.html = html
        self.defaults = type("D", (), {"timeout": 10, "retries": 0, "interval_ms": 0})()

    def get_text(self, url, headers=None, proxy=None, timeout=10, retries=3,
                 interval_ms=0, encoding=None, proxy_pool=None, direct=False):
        return self.html

    def close(self):
        pass


def _make_source(raw):
    class S:
        source_id = "demo"
        base_url = "https://example.com"
        content_type = "video"

        def __init__(self, _raw):
            self._raw = _raw

        @property
        def raw(self):
            return self._raw

        def transports(self):
            return {}

        def request_headers(self):
            return {}

        def proxy_pool(self):
            return None

        def get_detail_config(self):
            return self._raw["endpoints"]["detail"]

        def get_discovery_config(self):
            return {}

        def get_search_config(self):
            return {}

    return S(raw)


class _Checker:
    pass


def _make_content(http, raw):
    from framework.content import Content
    from framework.parser import Parser

    src = _make_source(raw)
    return Content(http, Parser(), _Checker()), src


def _raw_of():
    detail_cfg = {"fields": {"title": {"css": "h1"}}}
    content_cfg = {"episode": LIST_CFG}
    return {"endpoints": {"detail": detail_cfg, "content": content_cfg}}


def test_single_chapter_wp_pagination_full_episodes():
    """分页合集帖：list 逐 item 出多集，first_page_is_current 补入第 1 集。"""
    content, src = _make_content(_Http(PAGINATED_HTML), _raw_of())
    detail = content.fetch_detail(src, "https://example.com/2026/01/coll/")
    urls = [c.url for c in detail.chapters]
    # 当前详情 URL 置首，其余分页页 URL 跟随
    assert urls[0] == "https://example.com/2026/01/coll/"
    assert "https://example.com/2026/01/coll/2/" in urls
    assert "https://example.com/2026/01/coll/3/" in urls
    assert "https://example.com/2026/01/coll/8/" in urls
    assert urls[0] in {c.url for c in detail.chapters}


def test_single_chapter_no_list_match_falls_back():
    """list 选择器无匹配 → 回退为详情 URL 唯一分集（不空白、不错播）。"""
    content, src = _make_content(_Http(PLAIN_PAGE_HTML), _raw_of())
    detail = content.fetch_detail(src, "https://example.com/v/3")
    assert len(detail.chapters) == 1
    assert detail.chapters[0].url == "https://example.com/v/3"