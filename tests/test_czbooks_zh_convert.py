# -*- coding: utf-8 -*-
"""CZBooks 阅读界面繁→简转换测试（test_czbooks_zh_convert.py）。

需求：czbooks 是繁体站，阅读界面（正文/标题/作者/简介）希望显示简体中文。
实现：decryption 新增 zh_convert 策略（framework/zh_convert.py，zhconv 封装），
czbooks.json decryption.targets 各字段配 zh_convert/to=zh-cn。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_SRC = json.loads(
    (Path(__file__).resolve().parents[1] / "sources" / "czbooks.json").read_text("utf-8")
)


def test_zh_convert_available():
    """zhconv 依赖已安装（繁体源转简体的实现基础）。"""
    from framework import zh_convert

    assert zh_convert.available() is True


def test_to_simplified_basic():
    from framework.zh_convert import to_simplified

    assert to_simplified("漢陽郡，雲華宗") == "汉阳郡，云华宗"
    assert to_simplified("資訊網路軟體") == "资讯网络软件"


def test_convert_noop_without_change():
    from framework.zh_convert import to_simplified

    assert to_simplified("") == ""
    assert to_simplified("已经简体的文字") == "已经简体的文字"


def test_decrypter_zh_convert_strategy():
    """decryption.targets.chapter.strategy=zh_convert → 正文转简体。"""
    from framework.config import SourceConfig
    from framework.decrypter import Decrypter

    class _Http:
        defaults = type("D", (), {"timeout": 10, "retries": 0, "interval_ms": 0})()

        def get_text(self, *a, **k):
            return ""

    src = SourceConfig.from_dict(_SRC, "<czbooks-test>")
    out = Decrypter(_Http()).decrypt(src, "東原王國，漢陽郡", target="chapter")
    assert out == "东原王国，汉阳郡"


def test_decrypter_title_and_summary_strategy():
    from framework.config import SourceConfig
    from framework.decrypter import Decrypter

    class _Http:
        defaults = type("D", (), {"timeout": 10, "retries": 0, "interval_ms": 0})()

        def get_text(self, *a, **k):
            return ""

    src = SourceConfig.from_dict(_SRC, "<czbooks-test>")
    dec = Decrypter(_Http())
    assert dec.decrypt(src, "劍來", target="title") == "剑来"
    assert dec.decrypt(src, "這是一部網絡小說", target="summary") == "这是一部网络小说"


def test_decrypter_zh_convert_unknown_target_noop():
    """未配置 zh_convert 的 target（如 image）原样返回。"""
    from framework.config import SourceConfig
    from framework.decrypter import Decrypter

    class _Http:
        defaults = type("D", (), {"timeout": 10, "retries": 0, "interval_ms": 0})()

        def get_text(self, *a, **k):
            return ""

    src = SourceConfig.from_dict(_SRC, "<czbooks-test>")
    assert Decrypter(_Http()).decrypt(src, "劍來", target="image") == "劍來"


def test_czbooks_config_targets_configured():
    """czbooks 的 title/author/chapter/content/summary 都配了 zh_convert。"""
    targets = _SRC["decryption"]["targets"]
    for key in ("title", "author", "chapter", "content", "summary"):
        assert targets[key]["strategy"] == "zh_convert"
        assert targets[key]["to"] == "zh-cn"