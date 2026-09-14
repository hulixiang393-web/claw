"""单集视频源（single_chapter）分集行为回归测试。

对应 2026-09-09 视频分集显示修复：avgood / 18mh-video / 18j 均为
「详情页即单个视频、无真分集结构」的站点，正确行为是返回 1 条非空分集：
- avgood（single_chapter + list 有 iframe）→ 1 集，URL 取 iframe 播放页
  （取流必须走 api_endpoints.episode 的播放 id，故不能回退详情 URL）
- avgood 无 iframe 页（磁力下载页）→ 回退详情 URL 唯一分集，不空白
- 18j（single_chapter + 空 list {}）→ 回退详情 URL 唯一分集
- 18mh-video（single_chapter 无 list）→ 回退详情 URL 唯一分集

用真实源配置加载 + mock HTML，锁定「分集区永不空白、且不错播相关推荐」。
"""
import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.config import SourceConfig  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.parser import Parser  # noqa: E402

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "sources")


class _Http:
    """固定 HTML 假 HttpClient（按详情 URL 区分配置的页面）。"""

    defaults = type("D", (), {"timeout": 10, "retries": 0, "interval_ms": 0})()

    def __init__(self, pages):
        self.pages = pages

    def get_text(self, url, headers=None, proxy=None, timeout=10, retries=3,
                 interval_ms=0, encoding=None, proxy_pool=None, direct=False):
        return self.pages.get(url, self.pages.get("*", ""))

    def close(self):
        pass


class _Checker:
    pass


def _content(http):
    return Content(http, Parser(), _Checker())


def _load_source(sid):
    path = os.path.join(SRC_DIR, f"{sid}.json")
    with open(path, encoding="utf-8") as f:
        return SourceConfig.from_dict(json.load(f))


AVGOOD_WITH_IFRAME = """<html><body>
<header><nav><a href="/t/664/">線上區</a></nav></header>
<div class="content-container">
  <h1 class="content-title">和禁慾很久的美女房東約炮 [28:30]</h1>
  <iframe id="video-player" src="/remote_play/video/play/240576.html"></iframe>
</div>
<div class="card"><a href="/t/300676.html">老公出差，少妇约我去她家里操她</a></div>
</body></html>"""

AVGOOD_NO_IFRAME = """<html><body>
<header><nav><a href="/t/664/">線上區</a></nav></header>
<div class="content-container">
  <h1 class="content-title">MSD159 寂寞白领猎艳江湖小盗 [26:32]</h1>
</div>
</body></html>"""

SIMPLE_DETAIL = "<html><body><h1>3D高清后入肥臀御姐，饱满肉感大屁股极限顶弄超爽</h1></body></html>"


def test_avgood_single_chapter_list_with_iframe():
    """avgood：single_chapter + list 解析到 iframe → 1 集，URL 为绝对播放页。"""
    src = _load_source("avgood")
    url = "https://avgood.com/t/671812.html"
    detail = _content(_Http({url: AVGOOD_WITH_IFRAME})).fetch_detail(src, url)
    assert len(detail.chapters) == 1
    ch = detail.chapters[0]
    assert ch.title.startswith("和禁慾很久的美女房東約炮")
    assert ch.url == "https://avgood.com/remote_play/video/play/240576.html"


def test_avgood_single_chapter_fallback_when_no_iframe():
    """avgood：详情页无播放器（磁力下载页）→ 回退详情 URL 唯一分集，不空白。"""
    src = _load_source("avgood")
    url = "https://avgood.com/c/656899.html"
    detail = _content(_Http({url: AVGOOD_NO_IFRAME})).fetch_detail(src, url)
    assert len(detail.chapters) == 1
    ch = detail.chapters[0]
    assert ch.url.rstrip("/").lower() == url.rstrip("/").lower()
    assert ch.title  # 标题不空（视频本身），分集区不空白


def test_18j_single_chapter_empty_list_falls_back():
    """18j：single_chapter + 空 list {} → 回退详情 URL 唯一分集。"""
    src = _load_source("18j")
    url = "https://18j.tv/v/48002/"
    detail = _content(_Http({url: SIMPLE_DETAIL})).fetch_detail(src, url)
    assert len(detail.chapters) == 1
    ch = detail.chapters[0]
    assert ch.url.rstrip("/").lower() == url.rstrip("/").lower()


def test_18mh_single_chapter_no_list_falls_back():
    """18mh-video：single_chapter 无 list → 回退详情 URL 唯一分集。"""
    src = _load_source("18mh-video")
    url = "https://18mh.net/mv/detail/69179"
    detail = _content(_Http({url: SIMPLE_DETAIL})).fetch_detail(src, url)
    assert len(detail.chapters) == 1
    ch = detail.chapters[0]
    assert ch.url.rstrip("/").lower() == url.rstrip("/").lower()