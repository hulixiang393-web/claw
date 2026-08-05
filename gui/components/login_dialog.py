"""登录弹窗（login_dialog.py）。

用 QWebEngineView 内嵌浏览器让用户在源站点登录，登录成功后
提取 cookie 并保存到 CookieManager（独立文件 data/cookies/<source_id>.json）。

通用：任何需要登录的源都能用。登录 URL 默认取源 homepage，
可配 auth.login_url 覆盖。

登录成功判定：URL 离开登录页，或检测到登录标志 selector/URL 关键词
（配置 auth.login_success_url 包含的子串，如 "/home" "/user" "@" 等）。
"""

from __future__ import annotations

import json
import time
from typing import Optional

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineProfile
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)


class LoginDialog(QDialog):
    """内嵌浏览器登录弹窗。登录成功保存 cookie 后关闭。"""

    logged_in = Signal(str)  # source_id

    def __init__(
        self,
        source_id: str,
        login_url: str,
        cookie_manager=None,
        success_markers: Optional[list] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._source_id = source_id
        self._login_url = login_url
        self._cookie_manager = cookie_manager
        # 登录成功标志：URL 含这些子串（登录后跳转离开登录页）视为登录成功
        self._success_markers = success_markers or ["/home", "/user", "/dashboard", "@"]
        self._logged_in_detected = False

        self.setWindowTitle(f"登录 {source_id}")
        self.resize(900, 700)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.url_label = QLabel(login_url)
        self.url_label.setStyleSheet(
            "font-size: 11px; color: palette(mid);"
        )
        layout.addWidget(self.url_label)

        # 内嵌浏览器（独立 profile，避免污染全局 cookie）
        self.profile = QWebEngineProfile("login-" + source_id, self)
        self.view = QWebEngineView(self)
        # 关键：链接到该 profile 的 cookieStore 以提取 cookie
        self._cookie_store = self.profile.cookieStore()
        layout.addWidget(self.view, stretch=1)

        # 底部操作
        btn_row = QHBoxLayout()
        self.status_label = QLabel("请在页面上完成登录，完成后点击「完成」")
        self.status_label.setStyleSheet("color: palette(mid);")
        btn_row.addWidget(self.status_label, stretch=1)
        self.done_btn = QPushButton("完成登录 → 保存Cookie")
        self.done_btn.clicked.connect(self._on_done)
        btn_row.addWidget(self.done_btn)
        layout.addLayout(btn_row)

        # 监听 URL 变化（登录成功自动提示）
        self.view.urlChanged.connect(self._on_url_changed)

        # 加载登录页
        self.view.load(QUrl(login_url))

    # ------------------------------------------------------------------ #
    def _on_url_changed(self, url: QUrl) -> None:
        """URL 变化：若离开登录页且含成功标志，标记可保存。"""
        self.url_label.setText(url.toString())
        u = url.toString().lower()
        if (not self._logged_in_detected
                and any(m in u for m in self._success_markers)):
            self._logged_in_detected = True
            self.status_label.setText("检测到已登录，可点击「完成登录」保存 Cookie")

    # ------------------------------------------------------------------ #
    def _on_done(self) -> None:
        """提取 cookie 并保存（cookiesLoaded 信号异步收集）。"""
        collected = []

        def _collect(cookies):
            for c in cookies:
                collected.append(
                    {
                        "name": c.name(),
                        "value": c.value(),
                        "domain": c.domain(),
                        "path": c.path(),
                        "secure": c.isSecure(),
                    }
                )

        def _finish():
            self._save_and_close(collected)

        self._cookie_store.cookiesLoaded.connect(_collect)
        self._cookie_store.cookiesLoaded.connect(lambda cs: _finish())
        self._cookie_store.loadAllCookies()

    def _save_and_close(self, cookies) -> None:
        # 若 cookies 为空，兜底用 document.cookie
        if not cookies:
            self.view.page().runJavaScript(
                "document.cookie",
                lambda js: self._save_from_js(js or ""),
            )
        else:
            self._save_from_js("", cookies)

    def _save_from_js(self, js: str, cookies=None) -> None:
        if cookies is None:
            cookies = []
            for part in js.split(";"):
                part = part.strip()
                if "=" in part:
                    name, val = part.split("=", 1)
                    cookies.append({"name": name.strip(), "value": val.strip()})
        if not cookies:
            self.status_label.setText("未获取到 Cookie，请确认已登录后重试")
            return
        if self._cookie_manager is not None:
            self._cookie_manager.save(self._source_id, cookies)
            self.status_label.setText(f"已保存 {len(cookies)} 个 Cookie ✓")
        self.logged_in.emit(self._source_id)
        self.accept()