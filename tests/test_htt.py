# -*- coding: utf-8 -*-
"""htt（和图书 hetushu.com）源离线单元测试。

覆盖（全部基于真实页面 HTML 切片，无网络请求）：
- 源配置加载与关键配置形态（search/body 走 playwright render；detail 纯字段）
- 搜索结果字段解析（title/url/cover/author/summary；author 去斜杠清洗）
- 详情页字段解析（title/author/cover/summary/tags）
- 章节目录全量解析（dl#dir dd 平铺，正序，含首尾章节）
- 正文段落抽取（#content div：含水印的 HTML 文本管线 + h2 不被捕获）

说明：与线上行为差异
- 线上 search/body 走 playwright 渲染（Cloudflare 托管质询，HTTP 直连 403）。
- 渲染正文用 inner_text → 遵循站点 #content acronym/cite 的 display:none，
  水印自然滤除；本离线测试用 lxml text_content，会保留水印（= 框架正文
  HTML 管线的真实行为），据此断言水印属于已知受限项而非解析错误。
"""
import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.config import SourceConfig  # noqa: E402
from framework.parser import Parser  # noqa: E402

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "htt")
_BASE = "https://www.hetushu.com"
_SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sources", "htt.json"
)


def _parse(name: str):
    with open(os.path.join(_FIXTURES, name), encoding="utf-8") as f:
        return Parser().parse(f.read())


def _source() -> SourceConfig:
    with open(_SOURCE_PATH, encoding="utf-8") as f:
        return SourceConfig.from_dict(json.load(f), _SOURCE_PATH)


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def test_source_config_loads():
    src = _source()
    assert src.source_id == "htt"
    assert src.content_type == "novel"
    assert src.source_name == "和图书"
    assert src.base_url == _BASE


def test_source_render_layout():
    """搜索与正文必须走 playwright（CF 质询站，HTTP 直连 403）。"""
    src = _source()
    search = src.get_search_config()
    assert search.get("render") == "playwright"
    item = search.get("item") or {}
    assert item.get("root_selector", {}).get("css") == "dl.list#body dd h4 a"
    body = (
        (src.raw.get("endpoints") or {}).get("content") or {}
    ).get("chapter", {}).get("body") or {}
    assert body.get("render") == "playwright"
    assert body.get("selector", {}).get("css") == "#content div"


def test_detail_fields_configured():
    src = _source()
    detail = src.get_detail_config()
    fields = detail.get("fields") or {}
    assert fields.get("title", {}).get("css") == "div.book_info h2"
    assert fields.get("author", {}).get("css") == "div.book_info div a[href*='/author/']"
    assert fields.get("cover", {}).get("css") == "div.book_info img"
    assert fields.get("summary", {}).get("css") == "div.book_info div.intro"
    # 站点 class 恒为 "book_info finish"（连连载中作品也是），status 不可靠故不配置
    assert "status" not in fields


def test_toc_list_configured():
    src = _source()
    lst = (
        (src.raw.get("endpoints") or {}).get("content") or {}
    ).get("chapter", {}).get("list") or {}
    assert lst.get("root_selector", {}).get("css") == "dl#dir dd"
    assert lst.get("fields", {}).get("url", {}).get("attr") == "href"


# --------------------------------------------------------------------------- #
# 搜索结果（HTML 选择器路径；线上为 render 模式）
# --------------------------------------------------------------------------- #
def test_search_items_extract():
    doc = _parse("htt_search.html")
    items = Parser().parse_items(
        doc,
        {"css": "dl.list#body dd"},
        {
            "title": {"css": "h4 a"},
            "url": {"css": "h4 a", "attr": "href"},
            "cover": {"css": "a img", "attr": "src"},
            "author": {"css": "h4 span"},
            "summary": {"css": "div.intro"},
        },
        _BASE,
    )
    assert len(items) == 5  # 站内确认「共搜索到 5 本作品（关键词：斗破）」
    first = items[0]
    assert first["title"] == "斗破之元素封神"
    assert first["url"] == "https://www.hetushu.com/book/7631/index.html"
    assert first["cover"] == "https://www.hetushu.com/book/cover.pic/cover_7631.jpg"
    assert "风行云亦行" in first["author"]
    assert first["summary"].startswith("这是斗气的世界")
    # 命中原书
    books = {it["title"]: it for it in items}
    assert books["斗破苍穹"]["url"] == "https://www.hetushu.com/book/17/index.html"


def test_search_author_clean():
    """author 字段「/ 风行云亦行 /」→ 去斜杠清洗后为作者名。"""
    doc = _parse("htt_search.html")
    items = Parser().parse_items(
        doc,
        {"css": "dl.list#body dd"},
        {"author": {"css": "h4 span"}},
        _BASE,
    )
    cleaned = [t.strip().strip("/").strip() for t in (it["author"] for it in items)]
    assert cleaned[0] == "风行云亦行"
    # 配置文件里的替换对等价
    src = _source()
    pairs = src.get_search_config().get("item", {}).get("clean", {}).get("author", [])
    assert any("/" in p[0] for p in pairs)


# --------------------------------------------------------------------------- #
# 详情页
# --------------------------------------------------------------------------- #
def test_detail_fields_extract():
    doc = _parse("htt_detail.html")
    p = Parser()
    assert p.extract_first(doc, {"css": "div.book_info h2"}) == "斗破苍穹"
    assert p.extract_first(doc, {"css": "div.book_info div a[href*='/author/']"}) == "天蚕土豆"
    cover = p.extract_first(doc, {"css": "div.book_info img", "attr": "src"}, _BASE)
    assert cover == "https://www.hetushu.com/book/cover.pic/cover_17.jpg"
    summary = p.extract_first(doc, {"css": "div.book_info div.intro"})
    assert summary.startswith("这里是属于斗气的世界")
    tags = p.extract(doc, {"css": "dl.tag dd a"})
    assert "玄幻" in tags
    assert "异界大陆" in tags


# --------------------------------------------------------------------------- #
# 章节目录
# --------------------------------------------------------------------------- #
def test_toc_parses():
    doc = _parse("htt_detail.html")
    items = Parser().parse_items(
        doc,
        {"css": "dl#dir dd"},
        {
            "title": {"css": "a"},
            "url": {"css": "a", "attr": "href"},
        },
        _BASE,
    )
    assert len(items) == 7  # fixture 截断：原页 1646 章
    assert items[0]["title"] == "第1章 陨落的天才"
    assert items[0]["url"] == "https://www.hetushu.com/book/17/12657.html"
    assert items[-1]["title"] == "第1646章 五帝破空（大结局）"
    # 目录正序（依原页序号）
    nums = [int(t["title"].split("章 ")[0][1:]) for t in items[:5]]
    assert nums == [1, 2, 3, 4, 5]


# --------------------------------------------------------------------------- #
# 正文
# --------------------------------------------------------------------------- #
def test_body_paragraphs():
    doc = _parse("htt_chapter.html")
    paras = Parser().extract(doc, {"css": "#content div"})
    assert len(paras) >= 50
    assert paras[0] == "“斗之力，三段！”"
    assert paras[1].startswith("“三段？嘿嘿")


def test_body_excludes_chapter_title():
    """#content div 不应把章题 h2 当段落。"""
    doc = _parse("htt_chapter.html")
    paras = Parser().extract(doc, {"css": "#content div"})
    assert not any(p == "第1章 陨落的天才" for p in paras)


def test_body_watermark_is_known_limitation():
    """HTML 文本管线会保留站点注入的水印（acronym/cite）。

    这是已知受限项：线上正文走 playwright 渲染，fetch_rendered_text_sync
    用 inner_text，遵循站点 CSS `#content acronym, cite {display:none}`，
    水印会被浏览器过滤。此处断言水印确实存在于源 HTML，而非解析错误。
    """
    with open(os.path.join(_FIXTURES, "htt_chapter.html"), encoding="utf-8") as f:
        html = f.read()
        assert "<acronym>" in html or "<cite>" in html
    doc = _parse("htt_chapter.html")
    paras = Parser().extract(doc, {"css": "#content div"})
    txt = "\n".join(paras)
    assert "和图书" in txt or "ｈｅｔｕ" in txt or "hetushu.com" in txt