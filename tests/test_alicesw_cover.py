# -*- coding: utf-8 -*-
"""爱丽丝书屋封面链路回归测试（test_alicesw_cover.py）。

背景：爱丽丝书屋（alicesw）列表页结构特殊——作品项不是独立 li.two 块，
而是 div.rec_rullist 内多个 <ul>，每个 <ul> 里 <li class="two"> 放书名链接，
列表页无封面（依赖 cover_backfill 抓详情页回填）；详情页封面在
img.lazyload_book_cover 的 data-src（src 是 321cdn 占位图）。

另修一处封面加载链路的源级 direct 丢失：发现/搜索封面回填只传 source_id
不传 SourceConfig，CoverLoader 无法取到 transports.direct → 直连源的封面
经系统代理下载（代理不稳时封面空白）。现由 register_source 按 source_id
补源配置，使 get_bytes(direct=True, proxy=None)。

全部离线：HTML 为真实页面切片，网络调用用 fake HttpClient。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework.config import SourceConfig
from framework.parser import Parser

_SRC = json.loads(
    (Path(__file__).resolve().parents[1] / "sources" / "alicesw.json").read_text("utf-8")
)

# 真实 /original.html 列表页切片：div.rec_rullist 内多个 <ul>，书名在 li.two。
LIST_HTML = """
<div class="clearfix rec_rullist">
    <ul>
        <li class="one"><span>1</span></li>
        <li class="sev"><span><a href="/lists/56.html">熟女小说</a></span></li>
        <li class="two"><a href="/novel/52373.html" target="_blank">肥臀骚妇徐丽全文阅读</a></li>
        <li class="three"><a href="/book/53760/9bbb233f7cf51.html">后续更新说明</a></li>
        <li class="four">爱做高手</li>
    </ul>
    <ul>
        <li class="one"><span>2</span></li>
        <li class="sev"><span><a href="/lists/19.html">纯爱小说</a></span></li>
        <li class="two"><a href="/novel/51816.html" target="_blank">李富贵的幸福生活全文阅读</a></li>
        <li class="three"><a href="/book/53133/999f08b67045a.html">第四十三章 收缘篇</a></li>
        <li class="four">米酒啊</li>
    </ul>
</div>
"""

# 真实详情页封面切片：src 为 321cdn 占位图，真实封面在 data-src。
DETAIL_COVER_HTML = """
<div class="pic">
    <img src="https://img.321cdn.com/img/01.png"
         onerror="this.onerror=null; this.src='https://img.321cdn.com/img/01.png';"
         data-src="https://img.321cdn.com/uploads/20260725/9ac66fd5741e272ecc18576afd2a57c0.webp"
         class="lazyload_book_cover fengmian2" alt="肥臀骚妇徐丽">
</div>
<div class="novel_title">肥臀骚妇徐丽</div>
"""


def _src_config() -> SourceConfig:
    return SourceConfig.from_dict(_SRC, "<alicesw-cover-test>")


# ------------------------------------------------------------------ #
# 列表页：li.two 作品链接提取（真实 rec_rullist 结构）
# ------------------------------------------------------------------ #
def test_works_list_extracts_links_from_rec_rullist():
    wi = _SRC["endpoints"]["discovery"]["works_list_item"]
    doc = Parser().parse(LIST_HTML)
    items = Parser().parse_items(
        doc, wi["root_selector"], wi["fields"], "https://www.alicesw1.homes/"
    )
    # 表头 li.two（"书名"）无链接 → 空项被过滤，只留 2 个作品
    real = [it for it in items if it.get("title") and it.get("url")]
    assert len(real) == 2
    assert real[0]["title"] == "肥臀骚妇徐丽全文阅读"
    assert real[0]["url"] == "https://www.alicesw1.homes/novel/52373.html"
    assert real[1]["url"] == "https://www.alicesw1.homes/novel/51816.html"


def test_works_list_cover_is_empty_and_backfill_enabled():
    """列表页无封面 → cover 为空，且源开启 cover_backfill（依赖详情回填）。"""
    wi = _SRC["endpoints"]["discovery"]["works_list_item"]
    assert wi.get("cover_backfill") is True
    doc = Parser().parse(LIST_HTML)
    items = Parser().parse_items(
        doc, wi["root_selector"], wi["fields"], "https://www.alicesw1.homes/"
    )
    assert all(it.get("cover", "") == "" for it in items)


# ------------------------------------------------------------------ #
# 详情页：data-src 真实封面提取（src 为占位图）
# ------------------------------------------------------------------ #
def test_detail_cover_from_data_src():
    fields = _SRC["endpoints"]["detail"]["fields"]
    doc = Parser().parse(DETAIL_COVER_HTML)
    p = Parser()
    assert p.extract_first(doc, fields["title"]) == "肥臀骚妇徐丽"
    assert (
        p.extract_first(doc, fields["cover"])
        == "https://img.321cdn.com/uploads/20260725/9ac66fd5741e272ecc18576afd2a57c0.webp"
    )


# ------------------------------------------------------------------ #
# CoverLoader 源级 direct 透传：仅传 source_id 时按注册表补源配置
# ------------------------------------------------------------------ #
def _png_bytes():
    from PySide6.QtCore import QBuffer
    from PySide6.QtGui import QImage

    img = QImage(8, 8, QImage.Format_RGB32)
    img.fill(0xFF3366AA)
    buf = QBuffer()
    buf.open(QBuffer.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


class _FakeHttp:
    def __init__(self, data=b""):
        self.data = data
        self.calls = []

    def get_bytes(self, url, headers=None, proxy=None, timeout=None,
                  retries=None, proxy_pool=None, direct=False):
        self.calls.append(dict(url=url, proxy=proxy, direct=direct))
        return self.data


def _wait_until(cond, timeout=4000):
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()

    def _poll():
        if cond():
            loop.quit()
        else:
            QTimer.singleShot(25, _poll)

    QTimer.singleShot(25, _poll)
    QTimer.singleShot(timeout, loop.quit)
    loop.exec()


def test_cover_loader_resolves_direct_from_registered_source_id(_qapp):
    """封面回填只传 source_id → 注册表补出源级 direct，get_bytes(direct=True)。"""
    from gui.components import cover_loader as cl

    loader = cl._CoverLoader()
    loader.use_http(_FakeHttp(_png_bytes()))
    loader.register_source(_src_config())  # alicesw direct=True
    got = {}
    loader.load(
        "https://img.321cdn.com/uploads/x.webp",
        lambda pm: got.setdefault("pm", pm),
        source_id="alicesw",  # 仅 source_id，无 source 对象
        cache=False,
    )
    _wait_until(lambda: "pm" in got)
    assert got["pm"] is not None and not got["pm"].isNull()
    call = loader._http.calls[0]
    assert call["direct"] is True
    assert call["proxy"] is None


def test_cover_loader_without_registered_source_keeps_proxy(_qapp):
    """未注册的 source_id → 不强制直连（沿用既有代理策略，不误伤其它源）。"""
    from gui.components import cover_loader as cl

    loader = cl._CoverLoader()
    loader.use_http(_FakeHttp(_png_bytes()))
    got = {}
    loader.load(
        "https://img.cdn/1.jpg",
        lambda pm: got.setdefault("pm", pm),
        source_id="unknown-src",
        cache=False,
    )
    _wait_until(lambda: "pm" in got)
    assert loader._http.calls[0]["direct"] is False


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
