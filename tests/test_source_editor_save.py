# -*- coding: utf-8 -*-
"""源编辑器保存数据完整性测试（test_source_editor_save.py）。

背景：曾在源里只改搜索限制后保存，结果把配置改坏（搜索/发现字段选择器
fallback 丢失、source_switch 删除、decryption 删除，并注入一堆默认键）。
本文件回归：编辑一个字段保存 → 其余功能配置零损失、且不注入多余默认键。

覆盖：
- 仅改限制保存 → 完整保留 fallback 选择器 / source_switch / decryption.video_url；
- 不留空壳 endpoints.content.<type>.list / play_url；
- 不注入 auth / media / transports.follow_redirects / retry_backoff / 额外 constraints；
- 只有被改的字段发生变化；
- 显式取消勾选「启用换源」→ 才移除 source_switch；
- SelectorGrid：仅含 fallback（无 css/attr）的字段保存时原样保留。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from PySide6.QtWidgets import QApplication

from framework.config import SourceConfig
from gui.components import source_editor as se_mod
from gui.components.editor_grids import SelectorGrid

TITLE_FALLBACK = {
    "fallback": [
        {"css": "div.entry-title a", "attr": "title"},
        {"css": "div.entry-title a"},
        {"xpath": "following-sibling::div[contains(@class,'entry-title')]/a",
         "attr": "title"},
        {"xpath": "following-sibling::div[contains(@class,'entry-title')]/a"},
    ]
}
URL_FALLBACK = {
    "fallback": [
        {"css": "div.entry-title a", "attr": "href"},
        {"xpath": "following-sibling::div[contains(@class,'entry-title')]/a",
         "attr": "href"},
    ]
}


@pytest.fixture(scope="module")
def app(_qapp):
    return _qapp


def _source_dict():
    return {
        "$schema_version": 2,
        "$id": "tricky",
        "$type": "video",
        "$name": "Tricky Source",
        "$enabled": True,
        "$weight": 1.0,
        "$metadata": {"homepage": "https://example.com"},
        "transports": {
            "base_url": "https://example.com",
            "headers": {"User-Agent": "UA", "Referer": "https://example.com/"},
            "charset": "utf-8",
        },
        "endpoints": {
            "discovery": {
                "works_list_url": "/hot",
                "list_url": "/hot",
                "list_item": {"categories": [{"title": "A", "url": "/type/1"}]},
                "works_list_item": {
                    "root_selector": {"css": "div.video-item"},
                    "fields": {
                        "title": TITLE_FALLBACK,
                        "url": URL_FALLBACK,
                        "cover": {"css": "img.lazy", "attr": "data-original"},
                    },
                },
            },
            "search": {
                "base_url": "/search",
                "method": "GET",
                "keyword_param": "wd",
                "item": {
                    "root_selector": {"css": "div.video-item"},
                    "fields": {"title": TITLE_FALLBACK, "url": URL_FALLBACK},
                },
            },
            "detail": {
                "fields": {
                    "title": {"css": "h1", "regex": "(?s)标题[：:]\\s*(.*?)(?:\\s*作者[：:]|$)"},
                    "cover": {"regex": "MacPlayer\\.Pic\\s*=\\s*\"([^\"]+)\""},
                }
            },
            "content": {
                "episode": {
                    "single_chapter": True,
                    "source_switch": {
                        "play_regex": "var player_aaaa=(\\{.*?\\})\\s*</script>",
                        "max_switch_attempts": 2,
                    },
                }
            },
        },
        "decryption": {
            "targets": {
                "video_url": {"strategy": "maccms_url", "input": "text", "output": "text"}
            }
        },
        "constraints": {
            "search": {"max_pages": 10, "max_results": 240},
            "detail": {"max_pages": 1},
            "episode": {"max_pages": 1, "max_items": 1},
        },
    }


def _editor(d):
    cfg = SourceConfig.from_dict(d, path="<test>")
    return se_mod.SourceEditor(source_config=cfg, sources_dir=Path.cwd())


def test_edit_limits_only_changes_only_limits(app):
    """只改搜索限制保存 → 功能配置零损失、不注入多余键、且只有限制变化。"""
    ed = _editor(_source_dict())
    ed._f_search_pages.setValue(25)  # 10 -> 25
    saved = ed._build_dict()

    # 只有限制被改
    assert saved["constraints"]["search"]["max_pages"] == 25
    assert saved["constraints"]["search"]["max_results"] == 240

    # fallback 选择器原样保留
    assert saved["endpoints"]["search"]["item"]["fields"]["title"] == TITLE_FALLBACK
    assert saved["endpoints"]["search"]["item"]["fields"]["url"] == URL_FALLBACK
    wl = saved["endpoints"]["discovery"]["works_list_item"]["fields"]
    assert wl["title"] == TITLE_FALLBACK
    assert wl["url"] == URL_FALLBACK

    # source_switch 与 decryption 保留
    assert saved["endpoints"]["content"]["episode"]["source_switch"] == {
        "play_regex": "var player_aaaa=(\\{.*?\\})\\s*</script>",
        "max_switch_attempts": 2,
    }
    assert saved["decryption"]["targets"]["video_url"] == {
        "strategy": "maccms_url", "input": "text", "output": "text"
    }

    # 没有注入多余默认键/空壳
    ep = saved["endpoints"]["content"]["episode"]
    assert "list" not in ep and "play_url" not in ep and "body" not in ep
    assert "auth" not in saved
    assert "media" not in saved
    assert "follow_redirects" not in saved["transports"]
    assert "retry_backoff" not in saved["transports"]
    cons = saved["constraints"]
    assert "max_concurrency" not in cons
    assert "timeout_per_page_sec" not in cons.get("detail", {})
    assert "global" not in cons

    # 未被动的其它部分与原始一致
    orig = _source_dict()
    assert saved["endpoints"]["detail"]["fields"] == orig["endpoints"]["detail"]["fields"]
    assert saved["endpoints"]["discovery"]["list_item"] == orig["endpoints"]["discovery"]["list_item"]
    assert saved["$metadata"] == orig["$metadata"]


def test_uncheck_source_switch_removes_it(app):
    """用户显式取消「启用换源」→ 才移除 source_switch。"""
    ed = _editor(_source_dict())
    ed._ss_enable.setChecked(False)
    saved = ed._build_dict()
    assert "source_switch" not in saved["endpoints"]["content"]["episode"]


def test_save_kanav_real_file_zero_loss(app):
    """真实 kanav 源：仅改搜索限制保存 → 与原始json逐键等价（除被改键）。"""
    path = Path(__file__).resolve().parents[1] / "sources" / "kanav.json"
    if not path.exists():
        pytest.skip("sources/kanav.json 不存在")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["constraints"]["search"]["max_pages"] = 5  # 制造一个真实改动
    ed = _editor(raw)

    saved = ed._build_dict()
    saved["constraints"]["search"]["max_pages"] = raw["constraints"]["search"]["max_pages"]
    # 排除保存流程会重写的等价键后逐键比对
    for k, v in raw.items():
        assert saved.get(k) == v, f"键 {k} 在保存后被改动: {v} vs {saved.get(k)}"


def test_selector_grid_keeps_fallback_only_field(app):
    """仅有 fallback 无 css/attr 的字段：未编辑时保存原样保留，清 css 字段才删除。"""
    grid = SelectorGrid(None, ["title", "url", "cover"])
    grid.set_entries({
        "title": TITLE_FALLBACK,
        "url": {"css": ".url", "xpath": "//a"},
        "cover": {"css": "img"},
    })

    # 不动任何输入 → fallback-only 的 title 保留，css 字段保留
    out = grid.entries()
    assert out["title"] == TITLE_FALLBACK
    assert out["url"] == {"css": ".url", "xpath": "//a"}
    assert out["cover"] == {"css": "img"}

    # 清掉 url 的 css → 该字段被移除
    row = next(r for r in grid._entries if r[0] == "url")
    row[1].clear()
    assert "url" not in grid.entries()