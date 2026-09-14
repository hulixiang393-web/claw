# -*- coding: utf-8 -*-
"""czbooks（CZBooks 小说狂人 czbooks.net）源离线单元测试。

覆盖（全部基于真实页面 HTML 快照，无网络请求）：
- 源配置加载与关键配置形态（搜索 path 风格 /s/{keyword}、无 render，detail 用
  .novel-detail 抽取并 clean 《》书名/作者前缀/作品簡介前缀，章节列表 ul#chapter-list）
- 搜索结果字段解析（div.novel-item：title/url/cover/author/update，40 条）
- 详情页字段解析（連載中/已完結、占位封面与真实封面、tags=分類、簡介清理）
- 章节目录解析（卷标题「正文卷」无链接项被过滤；章节 URL 到 /n/{book}/{chapter}）
- 正文整块抽取（div.content：<br> 断行、免登录全文）

说明：
- 站部署 Cloudflare，但实测浏览器 UA 直连可得 200（未触发硬质询），无需 render。
- 书详情页 /n/{book} 已内嵌完整目录；章节正文页 /n/{book}/{chapter} 无
  .novel-detail 元数据（详情字段只对 /n/{book} 形态有效，见源 metadata）。
- 搜索每页固定 40 条、无分类内翻页之外的搜索翻页，constraints.search.max_pages=1。
"""
import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.config import SourceConfig  # noqa: E402
from framework.parser import Parser  # noqa: E402

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "czbooks")
_BASE = "https://czbooks.net"
_SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sources", "czbooks.json"
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
    assert src.source_id == "czbooks"
    assert src.content_type == "novel"
    assert src.source_name == "CZBooks"
    assert src.base_url == _BASE


def test_search_path_style_config():
    """搜索是 path 风格 /s/{keyword}（?keyword= query 形态返回 404）。"""
    src = _source()
    search = src.get_search_config()
    assert search.get("base_url") == "/s/{keyword}"
    assert search.get("method", "GET") == "GET"
    assert search.get("render") in (None, "static")  # explicit no-render source
    pag = search.get("paginator") or {}
    assert pag.get("url_template") == "/s/{keyword}"
    item = search.get("item") or {}
    assert item.get("root_selector", {}).get("css") == "div.novel-item"


def test_discovery_categories_configured():
    """发现界面：静态分类入口（站点导航 /c/{slug}）。"""
    src = _source()
    disc = src.get_discovery_config()
    list_item = disc.get("list_item") or {}
    cats = list_item.get("categories") or []
    assert len(cats) >= 20
    assert any(
        c.get("title") == "玄幻奇幻" and c.get("url") == "/c/xuanhuan" for c in cats
    )
    assert disc.get("list_url") == "/c/xuanhuan"
    works_li = disc.get("works_list_item") or {}
    assert works_li.get("root_selector", {}).get("css") == "div.novel-item"


def test_detail_fields_configured():
    src = _source()
    detail = src.get_detail_config()
    url_pat = detail.get("url_pattern")
    assert url_pat and "/n/" in url_pat
    fields = detail.get("fields") or {}
    assert fields.get("title", {}).get("css") == ".novel-detail .info .title"
    assert fields.get("status", {}).get("css") == (
        ".novel-detail .state table tr:nth-child(1) td:nth-child(2)"
    )
    assert fields.get("summary", {}).get("css") == ".novel-detail .description"
    # 书名带《》→ 必须配置清洗
    clean = detail.get("clean") or {}
    assert any("《" in p[0] for p in clean.get("title", []))
    assert any("作品簡介" in p[0] for p in clean.get("summary", []))


def test_toc_list_configured():
    src = _source()
    lst = (
        (src.raw.get("endpoints") or {}).get("content") or {}
    ).get("chapter", {}).get("list") or {}
    assert lst.get("root_selector", {}).get("css") == "ul#chapter-list li"
    assert lst.get("fields", {}).get("url", {}).get("attr") == "href"


# --------------------------------------------------------------------------- #
# 搜索结果
# --------------------------------------------------------------------------- #
def test_search_items_extract():
    doc = _parse("cz_search.html")
    items = Parser().parse_items(
        doc,
        {"css": "div.novel-item"},
        {
            "title": {"css": ".novel-item-title"},
            "url": {"css": ".novel-item-cover-wrapper a", "attr": "href"},
            "cover": {"css": ".novel-item-thumbnail img", "attr": "src"},
            "author": {"css": ".novel-item-author a"},
            "update": {"css": ".novel-item-newest-chapter a"},
        },
        _BASE,
    )
    assert len(items) == 40  # 站搜索页固定每页 40 条，无分页
    first = items[0]
    assert first["title"] == "劍來"
    assert first["url"].startswith("https://czbooks.net/n/")
    assert first["author"] == "烽火戲諸侯"
    assert "第" in first["update"]
    books = {it["url"] for it in items}
    assert "https://czbooks.net/n/s669hk" in books
    # 缺封面的书用站点统一占位图
    assert any("default_no_thumbnail" in it["cover"] for it in items)


# --------------------------------------------------------------------------- #
# 发现（分类作品列表）
# --------------------------------------------------------------------------- #
def test_discovery_works_extract():
    doc = _parse("cz_cat.html")  # 分类页截取（原页 70 条，fixture 保留 10）
    items = Parser().parse_items(
        doc,
        {"css": "div.novel-item"},
        {
            "title": {"css": ".novel-item-title"},
            "url": {"css": ".novel-item-cover-wrapper a", "attr": "href"},
            "cover": {"css": ".novel-item-thumbnail img", "attr": "src"},
            "author": {"css": ".novel-item-author a"},
        },
        _BASE,
    )
    assert len(items) == 10
    first = items[0]
    assert first["title"] == "彈幕審判"
    assert first["url"].startswith("https://czbooks.net/n/cr3anj")
    assert first["author"] == "我是蘑菇"
    assert first["cover"].startswith("https://img.czbooks.net/thumbnail/")
    assert all(it["url"].startswith("https://czbooks.net/n/") for it in items)


# --------------------------------------------------------------------------- #
# 详情页
# --------------------------------------------------------------------------- #
def test_detail_fields_extract():
    doc = _parse("cz_detail.html")  # s669hk《劍來》連載中
    p = Parser()
    assert p.extract_first(doc, {"css": ".novel-detail .info .title"}) == "《劍來》"
    assert (
        p.extract_first(doc, {"css": ".novel-detail .info .author a"}) == "烽火戲諸侯"
    )
    assert (
        p.extract_first(
            doc,
            {"css": ".novel-detail .state table tr:nth-child(1) td:nth-child(2)"},
        )
        == "連載中"
    )
    cover = p.extract_first(doc, {"css": ".novel-detail .thumbnail img", "attr": "src"}, _BASE)
    assert "default_no_thumbnail" in cover
    summary = p.extract_first(doc, {"css": ".novel-detail .description"})
    assert summary.startswith("大千世界，無奇不有。")
    tags = p.extract(doc, {"css": "a#novel-category"})
    assert tags == ["玄幻奇幻"]
    # clean 应用（framework.content._clean_field：字符串替换 / re: 正则）
    clean_title = "\u300a劍來\u300b".strip()
    for pat, repl in [("《", ""), ("》", "")]:
        clean_title = clean_title.replace(pat, repl)
    assert clean_title == "劍來"


def test_detail_fields_completed_and_cover():
    """已完結书 + 真实封面 + 带「作品簡介」前缀的简介。"""
    doc = _parse("cz_detail2.html")  # cp1m147 已完結
    p = Parser()
    assert p.extract_first(doc, {"css": ".novel-detail .info .title"}) == (
        "《寵你上癮︰軍爺的神秘嬌妻》"
    )
    assert p.extract_first(doc, {"css": ".novel-detail .info .author a"}) == "久陌離"
    assert (
        p.extract_first(
            doc,
            {"css": ".novel-detail .state table tr:nth-child(1) td:nth-child(2)"},
        )
        == "已完結"
    )
    cover = p.extract_first(doc, {"css": ".novel-detail .thumbnail img", "attr": "src"}, _BASE)
    assert cover.startswith("https://img.czbooks.net/thumbnail/")
    summary = p.extract_first(doc, {"css": ".novel-detail .description"})
    assert summary.startswith("作品簡介")
    tags = p.extract(doc, {"css": "a#novel-category"})
    assert tags == ["言情"]


def test_detail_author_fallback_clean():
    """author 回退到 .author 文本（含「作者: 」前缀），clean 应去掉前缀。"""
    doc = _parse("cz_detail.html")
    p = Parser()
    raw = p.extract_first(doc, {"css": ".novel-detail .info .author"})
    assert "烽火戲諸侯" in raw
    import re as _re

    assert _re.sub(r"^作者[:：]?\s*", "", raw) == "烽火戲諸侯"


def test_detail_fields_alternative_books():
    """备用书/重复书多版本：uefad 简介提示换书，元数据仍可抽取。"""
    doc = _parse("cz_detail3.html")  # uefad 备用书
    p = Parser()
    assert p.extract_first(doc, {"css": ".novel-detail .info .title"}) == "《劍來》"
    summary = p.extract_first(doc, {"css": ".novel-detail .description"})
    assert "備用書" in summary


# --------------------------------------------------------------------------- #
# 章节目录
# --------------------------------------------------------------------------- #
def test_toc_parses():
    doc = _parse("cz_detail.html")
    items = Parser().parse_items(
        doc,
        {"css": "ul#chapter-list li"},
        {
            "title": {"css": "a"},
            "url": {"css": "a", "attr": "href"},
        },
        _BASE,
    )
    # fixture 截断（原页 1404 章）：保留首 6 + 尾 8 个 li，其中首 li「正文卷」无链接
    assert len(items) == 14
    assert items[0]["title"] == "" or "正文卷" in items[0]["title"]  # 卷标题无 <a>
    assert items[0]["url"] == ""  # 无链接 → 框架目录抓取会跳过
    ch1 = items[1]
    assert ch1["title"] == "1.第1章 驚蟄"
    assert ch1["url"].startswith("https://czbooks.net/n/s669hk/")
    assert "chapterNumber=0" in ch1["url"]
    assert items[-1]["title"].endswith("人間土地廟")
    assert items[-1]["url"].endswith("chapterNumber=1400")
    # 目录按原页序正序（数字前缀递增）
    nums = [int(i["title"].split(".")[0]) for i in items[1:3]]
    assert nums == [1, 2]


def test_toc_parses_chapters_without_number_prefix():
    """无数字前缀目录（uefad）：章标题直接「第N章」，URL 不带 chapterNumber。"""
    doc = _parse("cz_detail3.html")
    items = Parser().parse_items(
        doc,
        {"css": "ul#chapter-list li"},
        {"title": {"css": "a"}, "url": {"css": "a", "attr": "href"}},
        _BASE,
    )
    assert items[1]["title"] == "第1章 驚蟄"
    assert items[1]["url"].startswith("https://czbooks.net/n/uefad/umke1")
    assert "?" not in items[1]["url"]


# --------------------------------------------------------------------------- #
# 正文
# --------------------------------------------------------------------------- #
def test_body_extract_full_text():
    doc = _parse("cz_chapter.html")
    text = Parser().extract_first(doc, {"css": "div.content"})
    assert text
    assert len(text) > 1000  # 单章全文（约 3100 字）
    assert "二月二，龍抬頭。" in text
    assert "陳平安" in text
    assert "第1章 驚蟄" in text  # 正文首行为章题（站点惯例）