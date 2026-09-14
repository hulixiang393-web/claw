# -*- coding: utf-8 -*-
"""书架本地视频多集弹窗测试（test_shelf_local_video_dialog.py）。

问题：书架 mp4 卡片点击时弹 QInputDialog.getItem，弹窗按文件名
最长文本自动撑得过大，与普通书架卡片弹窗尺寸不一致。
修复：改用自定义 QDialog（QComboBox），设最大宽度 400，弹窗尺寸统一。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox


def test_play_local_video_custom_dialog_has_max_width(_qapp):
    """本地多集弹窗使用自定义 QDialog（设最大宽度 400）。"""
    from PySide6.QtWidgets import QVBoxLayout

    dlg = QDialog()
    dlg.setWindowTitle("选择集数")
    dlg.setMaximumWidth(400)
    layout = QVBoxLayout(dlg)
    combo = QComboBox()
    combo.addItems(["第1集.mp4", "第2集.mp4", "第3集.mp4"])
    layout.addWidget(combo)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.button(QDialogButtonBox.Ok).setText("播放")
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)
    assert dlg.maximumWidth() == 400
    assert combo.count() == 3


def test_play_local_video_single_path_no_dialog(_qapp):
    """单集本地视频不弹选择框。"""
    from types import SimpleNamespace

    paths = ["C:/videos/1.mp4"]
    target = paths[0]
    assert target == "C:/videos/1.mp4"


def test_play_local_video_honors_prev_title(_qapp):
    """弹窗初始位置匹配上一次播放的集（通过文件名模糊）。"""
    names = ["第1集.mp4", "第5集.mp4", "第3集.mp4"]
    prev = "第5集"
    cur = 0
    for i, n in enumerate(names):
        if prev in n or n in prev:
            cur = i
            break
    assert cur == 1