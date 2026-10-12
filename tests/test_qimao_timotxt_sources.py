from __future__ import annotations

import json
from pathlib import Path

from framework.config import SourceConfig
from framework.content import Content
from framework.parser import Parser


ROOT = Path(__file__).resolve().parents[1]


def _source(source_id: str) -> SourceConfig:
    path = ROOT / "sources" / f"{source_id}.json"
    return SourceConfig.from_dict(json.loads(path.read_text(encoding="utf-8")), str(path))


def test_qimao_source_matches_current_ssr_structure():
    source = _source("qimao")
    assert source.content_type == "novel"
    assert source.raw["endpoints"]["content"]["chapter"]["body"]["selector"]["css"] == ".chapter-detail-article .article"
    assert source.raw["endpoints"]["detail"]["fields"]["cover"]["fallback"][0]["css"] == ".book-information .wrap-pic img"
    api = source.raw["endpoints"]["content"]["chapter"]["list"]["chapters_api"]
    assert api["url"] == "/qimaoapi/api/book/chapter-list"
    assert api["response_path"] == "data.chapters"
    assert api["item_fields"] == {"title": "title", "chapter_id": "id"}
    assert api["url_template"] == "/shuku/{id}-{chapter_id}/"


def test_qimao_api_directory_builds_ordered_chapter_urls():
    source = _source("qimao")
    calls = []

    class _Http:
        def get_json(self, url, **kwargs):
            calls.append(url)
            return {"data": {"chapters": [
                {"id": "c1", "title": "第1章"},
                {"id": "c2", "title": "第2章"},
            ]}}

    content = Content(_Http(), Parser(), object())
    cfg = source.raw["endpoints"]["content"]["chapter"]["list"]["chapters_api"]
    items = content._fetch_chapters_api(
        source, cfg, "https://www.qimao.com/shuku/2028957/", ""
    )

    assert calls == [
        "https://www.qimao.com/qimaoapi/api/book/chapter-list?book_id=2028957"
    ]
    assert items == [
        {"title": "第1章", "url": "/shuku/2028957-c1/"},
        {"title": "第2章", "url": "/shuku/2028957-c2/"},
    ]

    chapters = content._fetch_chapters(
        source,
        Parser().parse("<html></html>"),
        book_title="",
        detail_url="https://www.qimao.com/shuku/2028957/",
        html="",
    )
    assert [(chapter.title, chapter.url) for chapter in chapters] == [
        ("第1章", "/shuku/2028957-c1/"),
        ("第2章", "/shuku/2028957-c2/"),
    ]


def test_timotxt_uses_rendered_pages_and_simplified_chinese():
    source = _source("timotxt")
    assert source.raw["endpoints"]["discovery"]["render"] == "playwright"
    assert source.raw["endpoints"]["detail"]["render"] == "playwright"
    body = source.raw["endpoints"]["content"]["chapter"]["body"]
    assert body["render"] == "playwright"
    assert body["selector"]["css"] == "div.chapter-content div.content"
    assert source.raw["decryption"]["targets"]["content"]["to"] == "zh-cn"


def test_chapter_directory_pagination_expands_before_sorting():
    class _Parser:
        def parse(self, html):
            return html

        def parse_items(self, doc, root, fields, base_url):
            return {
                "page-1": [
                    {"title": "第3章", "url": "/chapter/3"},
                    {"title": "第1章", "url": "/chapter/1"},
                ],
                "page-2": [{"title": "第2章", "url": "/chapter/2"}],
            }.get(doc, [])

        def extract_first(self, doc, selector, base_url=""):
            return {"page-1": "/catalog?page=2", "page-2": ""}.get(doc, "")

    class _Http:
        defaults = type("D", (), {"timeout": 10, "retries": 0, "interval_ms": 0})()

        def get_text(self, url, **kwargs):
            assert url == "https://example.com/catalog?page=2"
            return "page-2"

    for content_type, block_name in (("novel", "chapter"), ("comic", "page")):
        raw = {
            "$id": f"paged-{content_type}", "$type": content_type,
            "$name": "Paged", "transports": {"base_url": "https://example.com"},
            "endpoints": {"content": {block_name: {
                "list": {
                    "root_selector": {"css": ".chapter"},
                    "fields": {"title": {"css": "a"}, "url": {"css": "a", "attr": "href"}},
                    "paginator": {
                        "next_link": {"selector": {"css": "a.next", "attr": "href"}},
                        "max_pages": 5,
                    },
                },
                "body": {"selector": {"css": ".content"}},
            }}},
            "constraints": {"chapter": {"max_pages": 5}},
        }
        source = SourceConfig.from_dict(raw, "<paged-test>")
        content = Content(_Http(), _Parser(), object())
        chapters = content._fetch_chapters(
            source, "page-1", detail_url="https://example.com/book/1"
        )
        assert [chapter.title for chapter in chapters] == ["第1章", "第2章", "第3章"]


def test_chapter_body_uses_content_conversion_target_when_configured():
    class _Parser:
        def parse(self, html):
            return html

        def extract(self, doc, selector):
            return ["繁體正文"]

        def extract_first(self, doc, selector, base_url=""):
            return ""

    class _Http:
        defaults = type("D", (), {"timeout": 10, "retries": 0, "interval_ms": 0})()

        def get_text(self, url, **kwargs):
            return "<div class='content'>繁體正文</div>"

    class _Decrypter:
        def __init__(self):
            self.targets = []

        def decrypt(self, source, value, target, **kwargs):
            self.targets.append(target)
            return "简体正文"

    raw = {
        "$id": "convert-test", "$type": "novel", "$name": "Convert",
        "transports": {"base_url": "https://example.com"},
        "endpoints": {"content": {"chapter": {
            "body": {"selector": {"css": ".content"}},
        }}},
        "decryption": {"targets": {
            "chapter": {"strategy": "zh_convert"},
            "content": {"strategy": "zh_convert"},
        }},
    }
    source = SourceConfig.from_dict(raw, "<convert-test>")
    decrypter = _Decrypter()
    content = Content(_Http(), _Parser(), object(), decrypter)
    text, next_url = content._fetch_chapter_page(source, "/chapter/1", pag_enabled=False)
    assert text == "简体正文"
    assert next_url == ""
    assert decrypter.targets == ["content"]
