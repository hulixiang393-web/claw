"""JSON 数据体不得被 HTML 反爬特征词误杀（test_http_json_not_antiscrape.py）。

现场（2026-11-03 07:42，纵横中文网）：
用户搜「真牛圣女」，纵横源状态条红叉 `❌ 纵横中文网`，tooltip 只有
`反爬特征响应（命中：被封）…/search/book?keyword=…`。实际 HTTP 200 +
合法 JSON，命中来自搜索结果里某本书的简介：「年仅二十便被封为史上最年轻的
圣女」——`ANTI_SCRAPE_KEYWORDS` 的「被封」命中了小说正文内容。

根因：`_is_anti_scrape_text` 是把响应当 HTML 挑战页扫的（先剥标签再匹配
特征词），对 JSON 数据体没有标签可剥，词表直接命中正常内容。

修法：`_is_json_document` 认得完整 JSON 就不做特征词扫描；真正的挑战页是
HTML，`json.loads` 必失败，照样命中（下面有对应用例，别让这次修复把拦截
能力一起改掉）。

运行：python -m pytest tests/test_http_json_not_antiscrape.py -q
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import framework.http as http_mod  # noqa: E402
from framework.config import load_source  # noqa: E402
from framework.http import HttpClient, NetworkDefaults  # noqa: E402
from framework.parser import Parser  # noqa: E402
from framework.search import Search  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# 纵横搜索接口的最小真实形状：简介里就带「被封」（被 封为…）
ZONGHENG_SEARCH_JSON = """
{"code":0,"data":{"datas":{"pageSize":20,"total":513,"pageNo":1,"totalPage":26,
"list":[
 {"bookId":1468769,"name":"\\u003cfont color=\\"RED\\"\\u003e圣女\\u003c/font\\u003e大人，我只是个杂役啊",
  "coverUrl":"/cover/85/93/8593b99cca94c7ef0fd95e9fc3f60fb1.jpeg",
  "authorName":"万里长安风","serialStatus":0,
  "updateTime":"2025-11-18T16:01:29+08:00",
  "description":"“论天资，我是千年难遇的奇才，年仅二十便被封为史上最年轻的圣女”"},
 {"bookId":1622170,"name":"穿成反派跟班，开局截胡\\u003cfont color=\\"RED\\"\\u003e圣女\\u003c/font\\u003e",
  "coverUrl":"/cover/85/93/8593b99cca94c7ef0fd95e9fc3f60fb2.jpeg",
  "authorName":"陈小桉","serialStatus":0,
  "updateTime":"2026-11-02T22:35:45+08:00","description":"开局绑了宗门圣女"}
]}}}
"""

HTML_BLOCK_PAGE = (
    "<html><body><h1>访问被拒绝</h1>"
    "<p>您的IP已被封禁，请稍后再试</p></body></html>"
)


class FakeResponse:
    def __init__(self, status_code=200, text="", content=b"",
                 content_type="application/json; charset=utf-8"):
        self.status_code = status_code
        self._text = text
        self.content = content or text.encode("utf-8")
        self.headers = {"Content-Type": content_type}
        self.encoding = None
        self.apparent_encoding = "utf-8"

    @property
    def text(self):
        return self._text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise http_mod.RequestError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def get(self, url, headers=None, proxies=None, timeout=None, **kw):
        self.calls.append({"method": "GET", "url": url})
        return self.resp

    def post(self, url, headers=None, proxies=None, timeout=None, **kw):
        self.calls.append({"method": "POST", "url": url})
        return self.resp


def _client(resp):
    """不带重试的客户端：特征词命中的重试语义不在本文件范围内。"""
    client = HttpClient(sleeper=lambda s: None, defaults=NetworkDefaults(retries=0))
    client._session = FakeSession(resp)
    return client


# --------------------------------------------------------------------------- #
# 1) JSON 数据体：不再被特征词误杀
# --------------------------------------------------------------------------- #
def test_json_body_with_banned_word_is_not_anti_scrape():
    """简介里的「被封」是正常小说内容，不能当反爬（纵横搜索现场）。"""
    client = _client(FakeResponse(text=ZONGHENG_SEARCH_JSON))
    text = client._get_once("http://x/search/book", None, None, 5)
    assert "被封为史上最年轻的圣女" in text


def test_get_json_with_banned_word_returns_payload():
    client = _client(FakeResponse(text=ZONGHENG_SEARCH_JSON))
    payload = client.get_json("http://x/search/book")
    assert payload["code"] == 0
    assert len(payload["data"]["datas"]["list"]) == 2


def test_post_json_with_banned_word_returns_payload():
    client = _client(FakeResponse(text=ZONGHENG_SEARCH_JSON))
    payload = client.post_json("http://x/search/book", json_body={"keyword": "圣女"})
    assert payload["data"]["datas"]["total"] == 513


def test_post_form_with_banned_word_returns_text():
    """表单 POST（如纵横目录 bookId）返回 JSON 时同样不做特征词扫描。"""
    client = _client(FakeResponse(text=ZONGHENG_SEARCH_JSON))
    text = client.post_form("http://x/search/book", form_data={"keyword": "圣女"})
    assert "被封为史上最年轻的圣女" in text


def test_post_text_json_body_with_banned_word_returns_text():
    client = _client(FakeResponse(text=ZONGHENG_SEARCH_JSON))
    text = client.post_text("http://x/search/book", json_body={"keyword": "圣女"})
    assert "被封为史上最年轻的圣女" in text


# --------------------------------------------------------------------------- #
# 2) HTML 挑战页：拦截能力不变（这条防「修误报把真反爬一起放掉」）
# --------------------------------------------------------------------------- #
def test_html_block_page_still_raises():
    client = _client(FakeResponse(text=HTML_BLOCK_PAGE,
                                  content_type="text/html; charset=utf-8"))
    with pytest.raises(http_mod.AntiScrapeError) as err:
        client._get_once("http://x/search", None, None, 5)
    assert "被封" in str(err.value)


def test_html_with_inline_json_fragment_is_not_json_document():
    """`<html>…{"a":1}…</html>` 不是数据体：仍按 HTML 扫特征词。"""
    assert http_mod._is_json_document('<html>{"a":1}</html>') is False
    assert http_mod._is_json_document(ZONGHENG_SEARCH_JSON.lstrip()) is True
    assert http_mod._is_json_document('["被封"]') is True
    assert http_mod._is_json_document("") is False
    assert http_mod._is_json_document("{ not json") is False


# --------------------------------------------------------------------------- #
# 3) 搜索层：纵横源用真实配置解析这份 JSON（字段/标题清洗/封面前缀）
# --------------------------------------------------------------------------- #
class FakeApiHttp:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.defaults = type("D", (), {"timeout": 10})()
        self.cache = None

    def get_json(self, url, **kwargs):
        self.calls.append(url)
        return self.payload


def test_zongheng_search_parses_payload_with_banned_word():
    source = load_source(ROOT / "sources" / "zongheng.json")
    http = FakeApiHttp(__import__("json").loads(ZONGHENG_SEARCH_JSON))
    results = Search(http, Parser()).search_one(source, "圣女")

    assert [r.title for r in results] == [
        "圣女大人，我只是个杂役啊",
        "穿成反派跟班，开局截胡圣女",
    ]
    assert results[0].url == "https://book.zongheng.com/book/1468769.html"
    assert results[0].cover.startswith("https://static.zongheng.com/cover/")
    # 参数名必须是 keyword（?q= 会拿到 keyword=null → 无结果）
    assert "keyword=%E5%9C%A3%E5%A5%B3" in http.calls[0]


# --------------------------------------------------------------------------- #
# 4) 失败必须留日志（现场「日志里一条都没有」的教训）
# --------------------------------------------------------------------------- #
def test_api_search_failure_is_logged(caplog):
    source = load_source(ROOT / "sources" / "zongheng.json")

    class BoomHttp(FakeApiHttp):
        def get_json(self, url, **kwargs):
            raise http_mod.AntiScrapeError("反爬特征响应（命中：waf）http://x/")

    with caplog.at_level(logging.WARNING):
        with pytest.raises(http_mod.AntiScrapeError):
            Search(BoomHttp({}), Parser()).search_one(source, "圣女")

    assert any(
        "zongheng" in rec.message and "API 搜索失败" in rec.message
        for rec in caplog.records
    ), [rec.message for rec in caplog.records]
