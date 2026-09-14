# -*- coding: utf-8 -*-
"""qk17（17K小说网 www.17k.com）源离线单元测试。

覆盖（全部基于真实页面 HTML 快照，无网络请求）：
- 源配置加载与关键配置形态（search 走 playwright render + complete URL 模板）
- 搜索结果字段解析（.search-list > .textlist > .textleft，title=img[alt]，
  排除顶部作者卡与列表内「标签」链接；20 条/页）
- 详情页字段解析（连载/完本 两个真实样本：title/author/status/cover/summary/tags）
- 章节目录解析（dl.Volume dd a 章节链接为根，url 用 xpath="." 取根自身 href）
- 正文段落抽取（#readArea .readAreaBox.content .p p:not(.copy) 排除广告行，
  不含水文特征行）

说明与线上差异：
- 线上 search 走 playwright 渲染（search.17k.com 对 HTTP 直连恒 405）；
- 渲染引擎按 root 节点提取 title=a[title]|img[alt]、href、src、text，与
  本离线测试用 parse_items(img[alt], a[href], img[src]) 等价（root 为 .textleft，
  其内只有封面 a>img，a 无 title 属性）。
- 正文离线 lxml text_content 与线上 HTML 管线同源，无渲染差异（body 未配 render）。
"""
import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.config import SourceConfig  # noqa: E402
from framework.parser import Parser  # noqa: E402

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "qk17")
_BASE = "https://www.17k.com"
_SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sources", "qk17.json"
)


def _parse(name: str):
    with open(os.path.join(_FIXTURES, name), encoding="utf-8") as f:
        return Parser().parse(f.read())


def _html(name: str) -> str:
    with open(os.path.join(_FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def _source() -> SourceConfig:
    with open(_SOURCE_PATH, encoding="utf-8") as f:
        return SourceConfig.from_dict(json.load(f), _SOURCE_PATH)


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def test_source_config_loads():
    src = _source()
    assert src.source_id == "qk17"
    assert src.content_type == "novel"
    assert src.source_name == "17K小说"
    assert src.base_url == _BASE


def test_search_render_layout():
    """搜索必须走 playwright（search.17k.com 对 HTTP 直连恒 405）。"""
    src = _source()
    search = src.get_search_config()
    assert search.get("render") == "playwright"
    assert search.get("keyword_param") == "c.q"
    # 完整 URL 模板：搜索在 subdom search.17k.com（与 www.17k.com 不同主机）
    tpl = search.get("paginator", {}).get("url_template")
    assert tpl.startswith("https://search.17k.com/")
    assert "{keyword}" in tpl
    item = search.get("item") or {}
    assert item.get("root_selector", {}).get("css") == ".search-list > .textlist > div.textleft"
    # 站内模糊匹配（搜「近战狂兵」返回超级兵王/至尊狂兵等）→ 关闭硬过滤
    rc = item.get("render_config") or {}
    assert rc.get("filter_keyword") is False


def test_detail_fields_configured():
    src = _source()
    detail = src.get_detail_config()
    fields = detail.get("fields") or {}
    assert fields.get("title", {}).get("css") == ".Info.Sign h1 a"
    # 作者从 og:title（《书名》作者著）正则提取，带 Keywords 兜底
    author = fields.get("author", {})
    assert author.get("attr") == "content"
    assert author.get("regex")
    assert author.get("fallback")
    assert fields.get("status", {}).get("css") == ".Info.Sign .label a:first-child span"
    assert fields.get("tags", {}).get("css") == "table tr.label td a span"


def test_toc_list_configured():
    src = _source()
    lst = (
        (src.raw.get("endpoints") or {}).get("content") or {}
    ).get("chapter", {}).get("list") or {}
    # 独立目录页模板 + 章节链接为根
    assert lst.get("chapters_url") == "/list/{id}.html"
    assert lst.get("root_selector", {}).get("css") == "dl.Volume dd a"
    assert lst.get("fields", {}).get("url", {}).get("xpath") == "."
    assert lst.get("fields", {}).get("url", {}).get("attr") == "href"


def test_body_selector_is_ssr_no_render():
    src = _source()
    body = (
        (src.raw.get("endpoints") or {}).get("content") or {}
    ).get("chapter", {}).get("body") or {}
    # 正文不配 render（17K 正文 SSR 纯文本直连可抓）
    assert "render" not in body
    assert body.get("selector", {}).get("css") == "#readArea .readAreaBox.content .p p:not(.copy)"


# --------------------------------------------------------------------------- #
# 搜索结果（HTML 解析路径；线上为 render 模式）
# --------------------------------------------------------------------------- #
def test_search_items_extract():
    doc = _parse("search.html")
    items = Parser().parse_items(
        doc,
        {"css": ".search-list > .textlist > div.textleft"},
        {
            "title": {"css": "img", "attr": "alt"},
            "url": {"css": "a", "attr": "href"},
            "cover": {"css": "img", "attr": "src"},
        },
        _BASE,
    )
    assert len(items) == 20  # 搜索 17K 站内固定每页 20 条
    first = items[0]
    assert first["title"] == "近战狂兵"
    assert first["url"] == "https://www.17k.com/book/2809894.html"
    assert first["cover"].startswith("https://cdn.static.17k.com/book/")
    # 无作者卡（user.17k.com 链接）混入
    assert not any("user.17k.com" in it["url"] for it in items)
    # 无「标签当书名」（整卡 a[title]）污染的标题
    assert not any(it["title"] in ("叶军浪", "撒旦", "热血") for it in items)


def test_search_keyword_is_fuzzy():
    """搜「近战狂兵」含模糊匹配结果（超级兵王等），render 侧已配 filter_keyword=false。"""
    doc = _parse("search.html")
    items = Parser().parse_items(
        doc,
        {"css": ".search-list > .textlist > div.textleft"},
        {"title": {"css": "img", "attr": "alt"}},
        _BASE,
    )
    titles = {it["title"] for it in items}
    assert "近战狂兵" in titles
    assert "超级兵王" in titles  # 模糊结果，若 filter_keyword 默认 true 会被误杀
    # 原书在首条（最相关）
    assert items[0]["title"] == "近战狂兵"


# --------------------------------------------------------------------------- #
# 详情页
# --------------------------------------------------------------------------- #
def test_detail_fields_extract_serial():
    """连载书样本：近战狂兵（book/2809894）。"""
    doc = _parse("detail.html")
    p = Parser()
    fields = {"title": {"css": ".Info.Sign h1 a"}}
    assert p.extract_first(doc, fields["title"]) == "近战狂兵"
    author = p.extract_first(
        doc,
        {"css": "meta[property='og:title']", "attr": "content", "regex": "《[^》]*》([^著]+)著"},
    )
    assert author == "梁七少"
    cover = p.extract_first(doc, {"css": "#bookCover img.book", "attr": "src"})
    assert cover == "https://cdn.static.17k.com/book/189x272/94/98/2809894.jpg-189x272?v=0"
    status = p.extract_first(doc, {"css": ".Info.Sign .label a:first-child span"})
    assert status == "连载小说"
    summary = p.extract_first(doc, {"css": "#bookInfo .cont p.intro"})
    assert "撒旦" in summary and "铁血霸气" in summary
    tags = p.extract(doc, {"css": "table tr.label td a span"})
    assert set(tags) == {"叶军浪", "撒旦", "热血"}
    update = p.extract_first(doc, {"css": "dl.Tab dt.tit em"})
    assert update.startswith("更新:")


def test_detail_fields_extract_complete():
    """完本书样本：超级兵王（book/108821）。"""
    doc = _parse("detail_complete.html")
    p = Parser()
    assert p.extract_first(doc, {"css": ".Info.Sign h1 a"}) == "超级兵王"
    author = p.extract_first(
        doc,
        {"css": "meta[property='og:title']", "attr": "content", "regex": "《[^》]*》([^著]+)著"},
    )
    assert author == "步千帆"
    status = p.extract_first(doc, {"css": ".Info.Sign .label a:first-child span"})
    assert status == "完本小说"
    summary = p.extract_first(doc, {"css": "#bookInfo .cont p.intro"})
    assert "兵王" in summary and "战神" in summary
    tags = p.extract(doc, {"css": "table tr.label td a span"})
    assert "特种兵" in tags and "雇佣兵" in tags
    assert len(tags) == 8


def test_detail_author_keywords_fallback():
    """作者兜底（meta[name=Keywords] 书名,作者,..）：og:title 缺失时的后备。"""
    doc = _parse("detail.html")
    kw = Parser().extract_first(
        doc, {"css": "meta[name='Keywords']", "attr": "content", "regex": "^[^,]+,([^,]+)"}
    )
    assert kw == "梁七少"


# --------------------------------------------------------------------------- #
# 章节目录
# --------------------------------------------------------------------------- #
def test_toc_parses():
    """目录 root=章节链接 <a>（dl.Volume dd a），url 取根自身 href。"""
    doc = _parse("list.html")
    items = Parser().parse_items(
        doc,
        {"css": "dl.Volume dd a"},
        {
            "title": {"css": "span.ellipsis"},
            "url": {"xpath": ".", "attr": "href"},
        },
        _BASE,
    )
    assert len(items) == 7  # fixture 截断：原页 5728 章（作品相关 3 + 正文 5725）
    assert items[0]["title"] == "新书感言"
    assert items[0]["url"] == "https://www.17k.com/chapter/2809894/34922029.html"
    # 正文（第一章）与末章
    assert items[3]["title"].startswith("第001章")
    assert items[3]["url"] == "https://www.17k.com/chapter/2809894/34983688.html"
    assert items[-1]["title"].startswith("第5723章")
    assert items[-1]["url"] == "https://www.17k.com/chapter/2809894/50477098.html"


def test_toc_url_uses_root_self():
    """url 字段必须从根节点自身取 href（xpath='.'），而非根的子元素查询。"""
    doc = _parse("list.html")
    items = Parser().parse_items(
        doc,
        {"css": "dl.Volume dd a"},
        {"url": {"xpath": ".", "attr": "href"}},
        _BASE,
    )
    assert all(it["url"].startswith("https://www.17k.com/chapter/") for it in items)


# --------------------------------------------------------------------------- #
# 正文
# --------------------------------------------------------------------------- #
def test_body_paragraphs():
    doc = _parse("chapter.html")
    paras = Parser().extract(doc, {"css": "#readArea .readAreaBox.content .p p:not(.copy)"})
    assert len(paras) >= 30
    assert len(paras[0]) > 0


def test_body_excludes_ad_copy():
    """:not(.copy) 剔除页脚广告 p.copy（本书首发来自17K小说网…）。"""
    html = _html("chapter.html")
    assert "class=\"copy" in html
    doc = Parser().parse(html)
    paras = Parser().extract(doc, {"css": "#readArea .readAreaBox.content .p p"})
    all_p = len(paras)
    clean = Parser().extract(doc, {"css": "#readArea .readAreaBox.content .p p:not(.copy)"})
    assert len(clean) == all_p - 1  # 恰好排除广告行


def test_body_has_chapter_marker():
    doc = _parse("chapter.html")
    paras = Parser().extract(doc, {"css": "#readArea .readAreaBox.content .p p:not(.copy)"})
    txt = "\n".join(paras)
    # 正文章段（感言章），含作者文风特征——确认抽到的是正文而非广告/占位
    assert "叶军浪" in txt or "七少" in txt or "书友" in txt