# -*- coding: utf-8 -*-
"""发现页 API 分支 title/author 解密测试（test_discovery_api_decrypt.py）。

回归：list_works 的 HTML 分支（selector 解析）有 decryption.title/author
translit 解密，但 api_endpoints 分支（_list_works_api，番茄 rank 榜单走这条）
漏接 → GUI 把 PUA 密文渲染成「标题中间多个连续空格」。本文件用 mock JSON 校验
API 分支返回的 Work.title/author 已还原为明文。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework.config import SourceConfig
from framework.discovery import Discovery
from framework.http import NetworkDefaults
from framework.parser import Parser

RAW_TITLE = "最强\uE3EA人"  # \uE3EA → 主
EXPECT_TITLE = "最强主人"
RAW_AUTHOR = "作\uE3EC"  # \uE3EC → 家
EXPECT_AUTHOR = "作家"


def _raw_source(with_decrypt: bool = True) -> dict:
    raw = {
        "$schema_version": 2,
        "$id": "api-dec-test",
        "$type": "novel",
        "$name": "API 解密测试源",
        "$enabled": True,
        "$weight": 1.0,
        "$metadata": {"homepage": "https://t.com/x", "lang": "zh-CN", "region": "cn"},
        "transports": {"base_url": "https://t.com"},
        "api_endpoints": {
            "discovery": {
                "url": "/api/list",
                "method": "GET",
                "params": {
                    "category_id": "{category_id}",
                    "gender": "{gender}",
                    "rankMold": "{rankMold}",
                    "limit": 10,
                    "offset": "{offset}",
                },
                "response_path": "data.book_list",
                "item_fields": {
                    "title": "bookName",
                    "url": "/page/{bookId}",
                    "cover": "thumbUri",
                    "author": "author",
                },
            }
        },
    }
    if with_decrypt:
        raw["decryption"] = {
            "targets": {
                "title": {"strategy": "translit"},
                "author": {"strategy": "translit"},
            }
        }
    return raw


class _FakeHttp:
    defaults = NetworkDefaults()

    def __init__(self, books):
        self._books = books

    def get_json(self, url, **kwargs):
        return {"data": {"book_list": list(self._books)}}


def _run(books, with_decrypt=True):
    http = _FakeHttp(books)
    source = SourceConfig.from_dict(_raw_source(with_decrypt), "<test>")
    checker = type("C", (), {"check": lambda *a, **k: None})()
    d = Discovery(http, Parser(), checker)
    return d.list_works(source, "/api/list?category_id=1&gender=1&rankMold=2")


def test_api_works_title_author_decrypted():
    books = [
        {"bookId": 1, "bookName": RAW_TITLE, "author": RAW_AUTHOR, "thumbUri": ""},
    ]
    works = _run(books)
    assert len(works) == 1
    assert works[0].title == EXPECT_TITLE
    assert works[0].author == EXPECT_AUTHOR
    assert works[0].url == "https://t.com/page/1"


def test_api_works_decrypt_multiple_rows():
    books = [
        {"bookId": 1, "bookName": "最强\uE3EA人", "author": "作\uE3EC", "thumbUri": ""},
        {"bookId": 2, "bookName": "领\uE3EA：从猎人卡开始", "author": "张三", "thumbUri": ""},
    ]
    works = _run(books)
    assert [w.title for w in works] == ["最强主人", "领主：从猎人卡开始"]


def test_api_works_no_decrypt_config_unchanged():
    books = [{"bookId": 1, "bookName": RAW_TITLE, "author": RAW_AUTHOR, "thumbUri": ""}]
    works = _run(books, with_decrypt=False)
    assert works[0].title == RAW_TITLE
    assert works[0].author == RAW_AUTHOR


def test_api_works_skips_row_without_title():
    books = [
        {"bookId": 1, "bookName": "", "author": "作\uE3EC", "thumbUri": ""},
        {"bookId": 2, "bookName": "最强\uE3EA人", "author": "", "thumbUri": ""},
    ]
    works = _run(books)
    assert len(works) == 1
    assert works[0].title == EXPECT_TITLE