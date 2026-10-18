"""内容详情 actor 字段解析测试（framework 层，不动源配置）。

- 源 detail.fields 配置 actor 选择器 → Detail.actor 提取到值（现有 css/attr 方式）
- 源 detail.fields 未配置 actor → Detail.actor 为空（不影响普通源回归）
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DETAIL_HTML = """<html><body>
<h1>测试视频</h1>
<div class="cast">张三 饰 主角, 李四 饰 配角</div>
</body></html>"""


class _Http:
    def __init__(self):
        self.calls = {}
        self.defaults = type("D", (), {"timeout": 10, "retries": 0, "interval_ms": 0})()

    def get_text(self, url, headers=None, proxy=None, timeout=10, retries=3,
                 interval_ms=0, encoding=None, proxy_pool=None, direct=False):
        self.calls[url] = self.calls.get(url, 0) + 1
        return DETAIL_HTML

    def close(self):
        pass


def _make_source(fields):
    class S:
        source_id = "demo"
        base_url = "https://example.com"
        content_type = "video"
        _raw = {"endpoints": {"detail": {"fields": fields}}}

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

    return S()


class _Checker:
    pass


def _make_content(http):
    from framework.content import Content
    from framework.parser import Parser

    return Content(http, Parser(), _Checker())


def test_detail_actor_parsed_when_configured():
    """detail.fields 配了 actor 选择器 → Detail.actor 提取到演员文案。"""
    fields = {"title": {"css": "h1"}, "actor": {"css": ".cast"}}
    src = _make_source(fields)
    http = _Http()
    detail = _make_content(http).fetch_detail(src, "https://example.com/v/1")
    assert detail.actor == "张三 饰 主角, 李四 饰 配角"


def test_detail_actor_empty_when_not_configured():
    """detail.fields 未配 actor → Detail.actor 为空（普通源不受影响）。"""
    fields = {"title": {"css": "h1"}}
    src = _make_source(fields)
    http = _Http()
    detail = _make_content(http).fetch_detail(src, "https://example.com/v/1")
    assert detail.actor == ""


def test_detail_actor_uses_attr_extraction():
    """actor 选择器支持 attr 提取（与现有字段同款 css/attr 方式）。"""
    html = '<html><body><h1>测试视频</h1>\n' \
           '<meta property="video:actor" content="王小美">\n</body></html>'

    class _H(_Http):
        def get_text(self, url, **kw):
            return html

    fields = {
        "title": {"css": "h1"},
        "actor": {"css": "meta[property='video:actor']", "attr": "content"},
    }
    src = _make_source(fields)
    detail = _make_content(_H()).fetch_detail(src, "https://example.com/v/1")
    assert detail.actor == "王小美"


def test_detail_cover_url_replace():
    """fields.cover 配 url_replace → 封面换域并去签名（91pornacomic 死链图床）。"""
    html = ('<html><body><h1>测试视频</h1>\n'
            '<meta property="og:image" '
            'content="https://expose.eisees.com/u/2024/9/x.jpg?auth=abc123&t=1">\n'
            '</body></html>')

    class _H(_Http):
        def get_text(self, url, **kw):
            return html

    fields = {
        "title": {"css": "h1"},
        "cover": {
            "css": "meta[property='og:image']",
            "attr": "content",
            "url_replace": ["https?://expose\\.eisees\\.com([^?#]*)",
                            "https://pic.zdmhyg.cn{1}"],
        },
    }
    src = _make_source(fields)
    detail = _make_content(_H()).fetch_detail(src, "https://example.com/v/1")
    assert detail.cover == "https://pic.zdmhyg.cn/u/2024/9/x.jpg"


def test_detail_cover_url_replace_no_match_keeps():
    """url_replace 无匹配 → 原样保留（普通图床源不受影响）。"""
    html = ('<html><body><h1>测试视频</h1>\n'
            '<meta property="og:image" content="https://img.other.net/a.jpg">\n'
            '</body></html>')

    class _H(_Http):
        def get_text(self, url, **kw):
            return html

    fields = {
        "title": {"css": "h1"},
        "cover": {
            "css": "meta[property='og:image']",
            "attr": "content",
            "url_replace": ["https?://expose\\.eisees\\.com([^?#]*)",
                            "https://pic.zdmhyg.cn{1}"],
        },
    }
    src = _make_source(fields)
    detail = _make_content(_H()).fetch_detail(src, "https://example.com/v/1")
    assert detail.cover == "https://img.other.net/a.jpg"


def test_detail_tags_from_meta_content():
    """detail.fields.tags 从 meta[video:tag] 提取（18mh-video 推荐关键词来源）。"""
    html = ('<html><body><h1>测试视频</h1>\n'
            '<meta property="video:tag" content="动漫">\n'
            '<meta property="video:tag" content="中文配音">\n</body></html>')

    class _H(_Http):
        def get_text(self, url, **kw):
            return html

    fields = {
        "title": {"css": "h1"},
        "tags": {"css": "meta[property='video:tag']", "attr": "content"},
    }
    src = _make_source(fields)
    detail = _make_content(_H()).fetch_detail(src, "https://example.com/v/1")
    assert detail.tags == ["动漫", "中文配音"]