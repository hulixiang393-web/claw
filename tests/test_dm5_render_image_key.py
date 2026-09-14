# -*- coding: utf-8 -*-
"""dm5（动漫屋）渲染提取 key 修复测试（test_dm5_render_image_key.py）。

覆盖：
- 配置 image_api.render_config 的源：先 Playwright 渲染章节页，用 DOM 里带
  真实 key 的图片 URL 覆盖接口正则提取的 key（JS 动态计算，静态 key 可能 403）；
- 渲染失败/未拿到 key：回退接口 key_regex 结果（原逻辑，不影响其他源）；
- 未配置 render_config：完全不触达渲染通道。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from framework.config import SourceConfig
from framework.parser import Parser

CHAPTER_HTML = """var x=1;DM5_CID=1328356;DM5_MID=81521;
DM5_VIEWSIGN="signabc";
DM5_VIEWSIGN_DT="2026-01-01 00:00:00";
DM5_IMAGE_COUNT=3;"""

API_TEXT = (
    'setkey("dm5", "draw", function(){pix="https://cdn.example/82/1/1328356";'
    'pvalue=["/1_7738.jpg","/2_7410.jpg","/3_5893.jpg"];key=\'interfacekey\';});'
)

IMAGE_API = {
    "url": "/chapterfun.ashx",
    "params": {
        "cid": "{cid}", "page": "{page}", "key": "",
        "language": 1, "gtk": 6,
        "_cid": "{cid}", "_mid": "{mid}",
        "_dt": "{sign_dt}", "_sign": "{sign}",
    },
    "vars": {
        "cid": "DM5_CID\\s*=\\s*(\\d+)",
        "mid": "DM5_MID\\s*=\\s*(\\d+)",
        "sign": "DM5_VIEWSIGN\\s*=\\s*\"([^\"]+)\"",
        "sign_dt": "DM5_VIEWSIGN_DT\\s*=\\s*\"([^\"]+)\"",
        "count": "DM5_IMAGE_COUNT\\s*=\\s*(\\d+)",
    },
    "packer": True,
    "pix_regex": "pix=\"([^\"]+)\"",
    "paths_regex": "pvalue=\\[([^\\]]*)\\]",
    "key_regex": "key='([^']+)'",
    "url_template": "{pix}{path}?cid={cid}&key={key}",
    "max_pages": 500,
}


def _raw(render_config=None):
    d = {
        "$schema_version": 2,
        "$id": "dm5-test",
        "$type": "comic",
        "$name": "dm5测试",
        "$enabled": True,
        "$weight": 1.0,
        "transports": {"base_url": "https://www.dm5.com", "interval_ms": 0},
        "endpoints": {
            "content": {
                "page": {
                    "body": {"image_api": dict(IMAGE_API)},
                }
            }
        },
    }
    if render_config:
        d["endpoints"]["content"]["page"]["body"]["image_api"]["render_config"] = render_config
    return d


class _FakeChecker:
    pass


class _FakeHttp:
    """假 HttpClient：章节页返回 JS 变量，接口返回 packer（pix/paths/key）。"""

    defaults = None

    def __init__(self):
        from framework.http import NetworkDefaults

        self.defaults = NetworkDefaults()

    def get_text(self, url, **kw):
        if "/chapterfun.ashx" in url:
            return API_TEXT
        return CHAPTER_HTML

    def close(self):
        pass


def _make_content():
    from framework.content import Content

    return Content(_FakeHttp(), Parser(), _FakeChecker())


def _fetch_dm5(content, render_config):
    src = SourceConfig.from_dict(_raw(render_config), "<mem>")
    return content._fetch_comic_page_imgs(
        src,
        src.raw["endpoints"]["content"]["page"]["body"],
        "https://www.dm5.com/m1328356/",
    )


# ---------------------------------------------------------------------- #
def test_render_config_uses_rendered_key():
    """配置 render_config 时走渲染通道，用 DOM 里带真实 key 的 URL 覆盖接口 key。"""
    content = _make_content()
    render_cfg = {
        "extract_mode": "img",
        "img_selector": "#showimage .item img",
        "wait_for": "#cp_img img[src*='.jpg']",
        "wait_until": "domcontentloaded",
        "timeout_ms": 30000,
        "extra_delay_ms": 1200,
    }
    with patch(
        "framework.playwright_helper.fetch_rendered_images_sync",
        return_value=[
            "https://cdn.example/82/1/1328356/1_7738.jpg?cid=1328356&key=renderkey123"
        ],
    ) as mock_render:
        imgs = _fetch_dm5(content, render_cfg)
    mock_render.assert_called_once()
    assert len(imgs) == 3
    assert all("key=renderkey123" in u for u in imgs), imgs
    assert all("key=interfacekey" not in u for u in imgs), imgs


def test_render_failure_falls_back_to_interface_key():
    """渲染失败/未拿到 key：回退接口 key_regex 提取结果（原逻辑，不整话失败）。"""
    content = _make_content()
    render_cfg = {"extract_mode": "img", "img_selector": "#showimage .item img"}
    with patch(
        "framework.playwright_helper.fetch_rendered_images_sync",
        side_effect=RuntimeError("playwright not available"),
    ):
        imgs = _fetch_dm5(content, render_cfg)
    assert len(imgs) == 3
    assert all("key=interfacekey" in u for u in imgs), imgs


def test_render_empty_result_falls_back_to_interface_key():
    """渲染返回空列表（get不到正文图）：同样回退接口 key。"""
    content = _make_content()
    render_cfg = {"extract_mode": "img", "img_selector": "#showimage .item img"}
    with patch(
        "framework.playwright_helper.fetch_rendered_images_sync",
        return_value=[],
    ):
        imgs = _fetch_dm5(content, render_cfg)
    assert len(imgs) == 3
    assert all("key=interfacekey" in u for u in imgs), imgs


def test_no_render_config_never_touches_playwright():
    """未配置 render_config：完全不触发渲染通道（其他源不受影响）。"""
    content = _make_content()
    with patch(
        "framework.playwright_helper.fetch_rendered_images_sync",
        return_value=["https://should.not.be.called"],
    ) as mock_render:
        imgs = _fetch_dm5(content, None)
    mock_render.assert_not_called()
    assert len(imgs) == 3
    assert all("key=interfacekey" in u for u in imgs), imgs


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))