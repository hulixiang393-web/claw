from __future__ import annotations

import json
from pathlib import Path

from framework.config import SourceConfig
from framework.content import Content
from framework.parser import Parser


ROOT = Path(__file__).resolve().parent.parent
SOURCE_PATH = ROOT / "sources" / "guazimanhua.json"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "guazimanhua"
BASE = "https://www.guazimanhua.com"
DETAIL_URL = f"{BASE}/comic.php?id=17623"
CHAPTER_URL = f"{BASE}/chapter.php?id=863908"
IMAGE_URLS = [
    "https://img.guazicdn.com/th/comics/chapters/30/260421/1_1.webp",
    "https://img.guazicdn.com/th/comics/chapters/30/260421/1_2.webp",
    "https://img.guazicdn.com/th/comics/chapters/30/260421/1_3.webp",
]


class _Checker:
    pass


class _Http:
    def __init__(self):
        self.defaults = None
        self.calls: list[str] = []

    def get_text(self, url, **kwargs):
        self.calls.append(url)
        if url == CHAPTER_URL:
            return (FIXTURES / "chapter.html").read_text(encoding="utf-8")
        return (FIXTURES / "detail.html").read_text(encoding="utf-8")

    def close(self):
        pass


def _source() -> SourceConfig:
    return SourceConfig.from_dict(
        json.loads(SOURCE_PATH.read_text(encoding="utf-8")), str(SOURCE_PATH)
    )


def test_start_reading_navigation_merges_same_url_and_preserves_real_title():
    source_data = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    source_data["endpoints"]["content"]["page"]["list"].pop("exclude_title_patterns", None)
    source = SourceConfig.from_dict(source_data, str(SOURCE_PATH))
    parser = Parser()
    doc = parser.parse(
        '<h1>瓜子漫画测试</h1><a href="/chapter.php?id=863908">从第一章开始阅读</a>'
        '<a href="/chapter.php?id=863908">第1话</a>'
    )
    content = Content(_Http(), parser, _Checker())

    chapters = content._fetch_chapters(
        source, doc, book_title="瓜子漫画测试", detail_url=DETAIL_URL
    )

    assert [(chapter.title, chapter.url) for chapter in chapters] == [
        ("第1话", CHAPTER_URL)
    ]


def test_start_reading_navigation_is_retained_when_it_is_the_only_link():
    source_data = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    source_data["endpoints"]["content"]["page"]["list"].pop("exclude_title_patterns", None)
    source = SourceConfig.from_dict(source_data, str(SOURCE_PATH))
    parser = Parser()
    doc = parser.parse(
        '<h1>瓜子漫画测试</h1><a href="/chapter.php?id=863907">开始阅读</a>'
    )
    content = Content(_Http(), parser, _Checker())

    chapters = content._fetch_chapters(
        source, doc, book_title="瓜子漫画测试", detail_url=DETAIL_URL
    )

    assert [(chapter.title, chapter.url) for chapter in chapters] == [
        ("开始阅读", f"{BASE}/chapter.php?id=863907")
    ]


def test_generic_source_keeps_navigation_label_as_first_regular_entry():
    source_data = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    source_data["endpoints"]["content"]["page"]["list"].pop("exclude_title_patterns", None)
    source = SourceConfig.from_dict(source_data, str(SOURCE_PATH))
    parser = Parser()
    doc = parser.parse(
        '<h1>瓜子漫画测试</h1><a href="/chapter.php?id=863907">从头开始阅读</a>'
        '<a href="/chapter.php?id=863908">第2话</a>'
    )
    content = Content(_Http(), parser, _Checker())

    chapters = content._fetch_chapters(
        source, doc, book_title="瓜子漫画测试", detail_url=DETAIL_URL
    )

    assert [chapter.title for chapter in chapters] == ["从头开始阅读", "第2话"]


def test_guazi_first_chapter_extracts_all_body_images():
    source = _source()
    http = _Http()
    content = Content(http, Parser(), _Checker())

    images = content._fetch_comic_page_imgs(
        source,
        source.raw["endpoints"]["content"]["page"]["body"],
        CHAPTER_URL,
    )

    assert images == IMAGE_URLS
    assert http.calls == [CHAPTER_URL]
