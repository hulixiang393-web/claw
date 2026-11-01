# -*- coding: utf-8 -*-
"""正文接口（body.api）优先 + 失败回退页面解析；二次转义实体清洗。

背景：timotxt 的服务端转码接口能直接给出明文正文（无子集字体假字），比
「抓页面 + 字形匹配」既准又快，但它偶发返回 `status=300` 无 content。
正文接口必须「优先但不唯一」：接口空了就回退页面解析，正文不能因此变空。
"""
from __future__ import annotations

from framework.config import SourceConfig
from framework.content import Content


BASE_RAW = {
    "$id": "api-test",
    "$type": "novel",
    "$name": "API 测试",
    "transports": {"base_url": "https://www.timotxt.com", "retries": 0},
    "endpoints": {
        "content": {
            "chapter": {
                "body": {
                    "selector": {"css": ".content > p"},
                    "api": {
                        "url": "/chapter/transcode.html?bid={book_id}&cid={chapter_id}&sid=0",
                        "book_id_regex": "/?(\\d{6,})/(\\d+)\\.html",
                        "response_path": "content",
                        "decrypt_target": "chapter",
                        "fallback": True,
                    },
                }
            }
        }
    },
    "decryption": {
        "targets": {
            "chapter": {"strategy": "zh_convert", "to": "zh-cn"},
        }
    },
}


def _source(**body_overrides):
    raw = {
        "$schema_version": 2,
        **BASE_RAW,
        "endpoints": {
            "content": {
                "chapter": {
                    "body": {**BASE_RAW["endpoints"]["content"]["chapter"]["body"],
                             **body_overrides},
                }
            }
        },
    }
    return SourceConfig.from_dict(raw, "<api-test>")


class _Parser:
    def parse(self, html):
        return html

    def extract(self, doc, selector):
        return ["页面的繁體正文。"]

    def extract_first(self, doc, selector, base_url=""):
        return ""


class _Http:
    defaults = type("D", (), {"timeout": 10, "retries": 0, "interval_ms": 0})()

    def __init__(self, payload=None, error=None, text=None):
        self.payload = payload
        self.error = error
        self.page_text = text or "<div class='content'><p>页面的繁體正文。</p></div>"
        self.json_calls = []
        self.text_calls = []

    def get_json(self, url, **kwargs):
        self.json_calls.append(url)
        if self.error:
            raise self.error
        return self.payload

    def get_text(self, url, **kwargs):
        self.text_calls.append(url)
        return self.page_text


class _Decrypter:
    def __init__(self):
        self.targets = []

    def decrypt(self, source, text, target="chapter", **kwargs):
        self.targets.append(target)
        return str(text).replace("繁體", "繁体")


def test_body_api_is_used_before_page_selector():
    http = _Http(payload={"status": 200, "content": "接口的繁體正文。"})
    decrypter = _Decrypter()
    content = Content(http, _Parser(), object(), decrypter)
    text, nxt = content._fetch_chapter_page(
        _source(), "https://www.timotxt.com/0306608834/179.html", pag_enabled=False
    )
    # {book_id} 取自 page URL（timo 路径没有 /novel/<id>/ 段），cid 取文件名
    assert http.json_calls == [
        "https://www.timotxt.com/chapter/transcode.html?bid=0306608834&cid=179&sid=0"
    ]
    assert http.text_calls == []  # 命接口就不该再抓正文页
    assert text == "接口的繁体正文。"
    assert nxt == ""
    assert decrypter.targets == ["chapter"]


def test_body_api_failure_falls_back_to_page_selector():
    http = _Http(error=RuntimeError("status=300 未转码"))
    decrypter = _Decrypter()
    content = Content(http, _Parser(), object(), decrypter)
    text, _nxt = content._fetch_chapter_page(
        _source(), "https://www.timotxt.com/0306608834/1.html", pag_enabled=False
    )
    assert http.json_calls, "接口应该先被尝试"
    assert http.text_calls == ["https://www.timotxt.com/0306608834/1.html"]
    assert text == "页面的繁体正文。", text


def test_body_api_empty_payload_falls_back_to_page_selector():
    http = _Http(payload={"status": 300, "msg": "轉碼失敗"})
    content = Content(http, _Parser(), object(), _Decrypter())
    text, _nxt = content._fetch_chapter_page(
        _source(), "https://www.timotxt.com/0306608834/1.html", pag_enabled=False
    )
    assert text == "页面的繁体正文。", text


def test_double_escaped_entities_are_unescaped():
    """站点把假字写成 `&amp;#xb14a;`：lxml 只解一层，正文里不能留 `&#xb14a;`。"""
    import html as html_lib
    import re

    class _EntityParser(_Parser):
        def extract(self, doc, selector):
            match = re.search(r"<p>(.*?)</p>", doc, re.S)
            return [html_lib.unescape(match.group(1))]  # 与 lxml 一样只解一层

    page = "<div class='content'><p>&amp;#26085;&amp;#xb14a;正文</p></div>"
    text, _nxt = Content(_Http(text=page), _EntityParser(), object(),
                         None)._fetch_chapter_page(
        _source(), "https://www.timotxt.com/0306608834/2.html", pag_enabled=False
    )
    assert "&#" not in text, text
    assert text.startswith("日"), text


def test_plain_text_is_not_touched_by_entity_pass():
    """正常正文里的 `&` 不能被当成实体解掉。"""
    content = Content(_Http(), _Parser(), object(), None)
    assert content._unescape_residual_entities("A&B 公司 & 品牌") == "A&B 公司 & 品牌"
