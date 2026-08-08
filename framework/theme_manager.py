"""主题管理（theme_manager.py）。

加载 / 切换 / 预览主题。三个主题的 QSS token 定义在本文件内（对齐 docs/theme.md）。
对应 ui-home.md「主题切换」+ theme.md「token 调色板」。

用法（GUI 层）：
    tm = ThemeManager(settings_manager)
    tm.apply_theme()               # 用 settings_manager 里的当前主题
    tm.switch_to("midnight")       # 实时切换
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

# ---- 主题调色板（来自 theme.md）------------------------------------------- #

THEMES: Dict[str, Dict[str, str]] = {
    "sakura": {
        "accent": "#FF8FAB",
        "accent2": "#7AE0C9",
        "accent3": "#BDA0E0",
        "bg": "#FFF6F9",
        "bg2": "#FFEEF4",
        "bg3": "#F5E6EE",
        "text": "#3D2A3B",
        "text2": "#7D6B7A",
        "danger": "#FF6B6B",
        "warning": "#FFB562",
        "success": "#7BC47A",
        "stroke": "#FFD6E0",
        "glow": "rgba(255,143,171,0.25)",
        "radius_sm": "6px",
        "radius_md": "12px",
        "radius_lg": "20px",
        "radius_pill": "999px",
        "shadow1": "0 2px 6px rgba(0,0,0,0.06)",
        "shadow2": "0 6px 16px rgba(0,0,0,0.10)",
        "shadow_glow": "0 4px 16px rgba(255,143,171,0.25)",
    },
    "mint": {
        "accent": "#7AE0C9",
        "accent2": "#5BC0BE",
        "accent3": "#B0E0E6",
        "bg": "#FFFEF9",
        "bg2": "#F0FDF4",
        "bg3": "#E8F5E9",
        "text": "#2D4033",
        "text2": "#6B8A70",
        "danger": "#FF6B6B",
        "warning": "#FFB562",
        "success": "#7BC47A",
        "stroke": "#D4E8D8",
        "glow": "rgba(122,224,201,0.25)",
        "radius_sm": "6px",
        "radius_md": "12px",
        "radius_lg": "20px",
        "radius_pill": "999px",
        "shadow1": "0 2px 6px rgba(0,0,0,0.06)",
        "shadow2": "0 6px 16px rgba(0,0,0,0.10)",
        "shadow_glow": "0 4px 16px rgba(122,224,201,0.25)",
    },
    "midnight": {
        "accent": "#FF6B9D",
        "accent2": "#4ECDC4",
        "accent3": "#9C7BD3",
        "bg": "#1E1B26",
        "bg2": "#2A2535",
        "bg3": "#35303F",
        "text": "#F2EAF3",
        "text2": "#B5A8BA",
        "danger": "#FF6B6B",
        "warning": "#FFB562",
        "success": "#7BC47A",
        "stroke": "#3D3548",
        "glow": "rgba(255,107,157,0.35)",
        "radius_sm": "6px",
        "radius_md": "12px",
        "radius_lg": "20px",
        "radius_pill": "999px",
        "shadow1": "0 2px 6px rgba(0,0,0,0.20)",
        "shadow2": "0 6px 16px rgba(0,0,0,0.30)",
        "shadow_glow": "0 4px 16px rgba(255,107,157,0.35)",
    },
}

AVAILABLE_THEMES = list(THEMES.keys())


@dataclass
class ThemeInfo:
    """主题信息（GUI 展示用）。"""
    key: str
    name: str
    description: str

THEME_INFO: Dict[str, ThemeInfo] = {
    "sakura":    ThemeInfo("sakura",    "樱花粉", "桃雪背景 + 樱粉强调"),
    "mint":      ThemeInfo("mint",      "薄荷绿", "奶油白背景 + 薄荷绿强调"),
    "midnight":  ThemeInfo("midnight",  "夜樱紫", "紫夜背景 + 霓虹粉强调"),
}


class ThemeManager:
    """主题管理器：读取 settings 里的主题 key，生成 QSS，支持实时切换。"""

    def __init__(self, settings: Any | None = None):
        self._settings = settings
        self._current_key: str = "sakura"
        self._theme_change_callbacks: list = []
        if settings is not None:
            self._current_key = settings.get("ui", "theme", "sakura")

    # ------------------------------------------------------------------ #
    def current_key(self) -> str:
        return self._current_key

    def current_tokens(self) -> Dict[str, str]:
        return THEMES.get(self._current_key, THEMES["sakura"])

    def generate_qss(self, theme_key: str | None = None) -> str:
        key = theme_key or self._current_key
        t = THEMES.get(key, THEMES["sakura"])
        qss = BASE_QSS.format_map(t)
        # 全局字体缩放：scale font-size: Npx → round(N*scale)px（BASE_QSS 内 4 处）
        scale = 1.0
        if self._settings is not None:
            try:
                scale = float(self._settings.get("ui", "font_scale", 1.0) or 1.0)
            except (TypeError, ValueError):
                scale = 1.0
        if scale != 1.0:
            import re as _re

            def _scale_font(match):
                val = round(int(match.group(1)) * scale)
                return f"font-size: {val}px"

            qss = _re.sub(r"font-size:\s*(\d+)px", _scale_font, qss)
        return qss

    def apply_theme(self) -> str:
        """读取当前主题 key，返回 QSS 字符串。GUI 层 qApp.setStyleSheet(qss)。"""
        qss = self.generate_qss()
        return qss

    def switch_to(self, theme_key: str) -> str:
        if theme_key not in THEMES:
            theme_key = "sakura"
        self._current_key = theme_key
        if self._settings is not None:
            self._settings.set("ui", "theme", theme_key)
            self._settings.save()
        qss = self.generate_qss()
        for cb in self._theme_change_callbacks:
            try:
                cb(theme_key)
            except Exception:
                pass
        return qss

    def on_theme_changed(self, callback) -> None:
        self._theme_change_callbacks.append(callback)

    def available_themes(self) -> List[ThemeInfo]:
        return [THEME_INFO[k] for k in AVAILABLE_THEMES]


# ---- 基础 QSS 模板 ------------------------------------------------------- #

BASE_QSS = """
/* ---------- 全局 ---------- */
* {{
    font-family: "Noto Sans CJK SC", "Microsoft YaHei", "Segoe UI Emoji",
        "Apple Color Emoji", "Noto Color Emoji", "Helvetica Neue", sans-serif;
}}
QMainWindow, QDialog {{
    background-color: {bg};
    color: {text};
    border-radius: {radius_md};
}}
/* 注意：不要给 QWidget 全局设 background-color: transparent ——
   那会让每个 widget 单独走样式引擎，重绘性能极差（按钮卡顿根源）。 */

/* ---------- 按钮 ---------- */
QPushButton {{
    background-color: {accent};
    color: #ffffff;
    border: none;
    border-radius: {radius_md};
    padding: 8px 18px;
    font-weight: bold;
}}
QPushButton:hover {{
    background-color: {accent2};
}}
QPushButton:pressed {{
    background-color: {accent2};
}}
QPushButton:disabled {{
    background-color: {text2};
    color: {bg};
}}

/* ---------- 输入框 ---------- */
QLineEdit, QSpinBox, QComboBox {{
    background-color: {bg2};
    border: 1px solid {stroke};
    border-radius: {radius_md};
    padding: 6px 12px;
    color: {text};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border: 2px solid {accent};
}}

/* ---------- 卡片/分组 ---------- */
QFrame {{
    background-color: {bg2};
    border-radius: {radius_md};
    padding: 12px;
}}
QFrame#statsCard {{
    background-color: {bg2};
    border-radius: {radius_md};
    border: 1px solid {stroke};
}}

/* ---------- 列表 ---------- */
QListWidget {{
    background-color: {bg2};
    border-radius: {radius_md};
    border: none;
    padding: 4px;
}}
QListWidget::item {{
    padding: 8px 12px;
    border-radius: {radius_md};
}}
QListWidget::item:hover {{
    background-color: {bg3};
}}
QListWidget::item:selected {{
    background-color: {accent};
    color: #ffffff;
}}

/* ---------- 进度条 ---------- */
QProgressBar {{
    background-color: {bg3};
    border-radius: {radius_sm};
    height: 8px;
}}
QProgressBar::chunk {{
    background-color: {accent2};
    border-radius: {radius_sm};
}}

/* ---------- Tab 导航栏（胶囊式） ---------- */
QTabWidget::pane {{
    background-color: {bg};
    border: none;
    border-radius: {radius_md};
}}
QTabWidget::tab-bar {{
    left: 8px;
}}
QTabBar {{
    background-color: {bg};
    border: none;
}}
QTabBar::tab {{
    background-color: transparent;
    color: {text2};
    border-radius: {radius_pill};
    padding: 8px 18px;
    margin: 4px 4px;
    font-size: 14px;
    font-weight: 600;
    border: none;
}}
QTabBar::tab:selected {{
    background-color: {accent};
    color: #ffffff;
    border: 2px solid {accent};
}}
QTabBar::tab:hover:!selected {{
    background-color: {bg3};
    color: {text};
}}
QTabBar::tab:disabled {{
    color: {text2};
    background: transparent;
}}
/* Tab 里的「开发中」标注 */
QTabBar::tab::corner {{
    background: transparent;
}}

/* ---------- 滚动条 ---------- */
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    border-radius: {radius_sm};
}}
QScrollBar::handle:vertical {{
    background-color: {stroke};
    border-radius: {radius_sm};
}}
/* 横向滚动条：细窄不遮内容（分类栏等） */
QScrollBar:horizontal {{
    background: transparent;
    height: 8px;
    border-radius: {radius_sm};
}}
QScrollBar::handle:horizontal {{
    background-color: {stroke};
    border-radius: {radius_sm};
    min-width: 30px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
    width: 0;
}}

/* ---------- 标签/徽章 ---------- */
QLabel#statsValue {{
    font-size: 18px;
    font-weight: bold;
    color: {accent};
}}
QLabel#statsLabel {{
    font-size: 11px;
    color: {text2};
}}
QLabel#brokenBadge {{
    background-color: {danger};
    color: #ffffff;
    border-radius: {radius_sm};
    padding: 2px 6px;
    font-size: 10px;
}}
"""
