import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.config import SourceConfig
from framework.content import Content
from framework.parser import Parser


class _Http:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []
        self.defaults = type(
            "Defaults", (), {"timeout": 10, "retries": 0, "interval_ms": 0}
        )()

    def get_text(self, url, **kwargs):
        self.calls.append(url)
        return self.pages[url]


def _source(pagination):
    return SourceConfig.from_dict(
        {
            "$schema_version": 2,
            "$id": "chapter-pagination",
            "$type": "novel",
            "$name": "chapter pagination",
            "diagnostics": {"selfcheck": {"strategy": "off"}},
            "transports": {"base_url": "https://reader.example"},
            "endpoints": {
                "content": {
                    "chapter": {
                        "body": {"selector": {"css": "#chaptercontent p"}},
                        "pagination": pagination,
                    }
                }
            },
        }
    )


def _page(text, next_url=""):
    nav = (
        f'<nav class="read-nav"><a href="{next_url}">next</a></nav>'
        if next_url
        else ""
    )
    return f'<div id="chaptercontent"><p>{text}</p></div>{nav}'


def _content(pages):
    http = _Http(pages)
    return Content(http, Parser(), object()), http


def test_discovery_page_url_template_supports_suffix_pagination():
    from framework.config import SourceConfig
    from framework.discovery import Discovery

    source = SourceConfig.from_dict({
        "$schema_version": 2,
        "$id": "suffix-pagination",
        "$type": "novel",
        "$name": "suffix pagination",
        "transports": {"base_url": "https://reader.example"},
        "endpoints": {
            "discovery": {
                "list_url": "/list-8/",
                "list_paginator": {
                    "type": "increment",
                    "first_page_without_param": True,
                    "page_url_template": "/list-8-{page}/",
                },
            }
        },
    })

    assert Discovery._build_page_url(Discovery, source, "/list-8/", 1) == "/list-8/"
    assert Discovery._build_page_url(Discovery, source, "/list-8/", 2) == "/list-8-2/"


def test_discovery_first_page_can_avoid_page_query_parameter():
    from framework.config import SourceConfig
    from framework.discovery import Discovery

    source = SourceConfig.from_dict({
        "$schema_version": 2,
        "$id": "discovery-pagination",
        "$type": "novel",
        "$name": "discovery pagination",
        "transports": {"base_url": "https://reader.example"},
        "endpoints": {
            "discovery": {
                "list_url": "/latest/",
                "list_paginator": {"first_page_without_param": True},
            }
        },
    })

    assert Discovery._build_page_url(Discovery, source, "/latest/", 1) == "/latest/"


def test_same_chapter_regex_joins_special_page_suffix_without_rewriting_request_url():
    first = "https://reader.example/book/4406993-35.html"
    continuation = "https://reader.example/book/4406993-35-2.html"
    content, http = _content(
        {
            first: _page("first", "/book/4406993-35-2.html"),
            continuation: _page("continuation"),
        }
    )
    source = _source(
        {
            "enabled": True,
            "same_chapter_regex": r"(?P<base>/book/\d+-\d+)(?:-\d+)?\.html$",
            "same_chapter_replacement": r"\g<base>.html",
        }
    )

    assert content.fetch_chapter(source, first) == "first\ncontinuation"
    assert http.calls == [first, continuation]


def test_without_same_chapter_regex_keeps_numeric_underscore_pagination():
    first = "https://reader.example/book/4406993-35.html"
    continuation = "https://reader.example/book/4406993-35_1.html"
    content, http = _content(
        {
            first: _page("first", "/book/4406993-35_1.html"),
            continuation: _page("continuation"),
        }
    )
    source = _source({"enabled": True})

    assert content.fetch_chapter(source, first) == "first\ncontinuation"
    assert http.calls == [first, continuation]


def test_invalid_same_chapter_regex_falls_back_to_default_pagination_rule():
    first = "https://reader.example/book/4406993-35.html"
    continuation = "https://reader.example/book/4406993-35_1.html"
    content, http = _content(
        {
            first: _page("first", "/book/4406993-35_1.html"),
            continuation: _page("continuation"),
        }
    )
    source = _source(
        {
            "enabled": True,
            "same_chapter_regex": "[",
            "same_chapter_replacement": "",
        }
    )

    assert content.fetch_chapter(source, first) == "first\ncontinuation"
    assert http.calls == [first, continuation]
