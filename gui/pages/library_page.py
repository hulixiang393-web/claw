"""书架（library_page.py）v2。

本地已下载 + 手动收藏的聚合视图，数据全部经 ShelfService 统一 API：
- **本地优先**：本地已下载且收藏的书合并成一条（online 标记），点开直接读本地（零网络）
- **本地视频可播**：点本地视频书 → 弹集选择 → 播本地 mp4（经 play_local_video_requested）
- **搜索 / 排序**：标题模糊搜索 + 最近阅读/添加/书名排序
- 本地扫描后台线程（不卡 UI），epub 类型检测服务层缓存

对应需求：本地书籍直接加载本地、本地/收藏合并去重、收藏分类、搜索排序。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, QRunnable, QObject, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gui.components.hover_title import HoverTitle
from framework.series_group import (
    group_favorites,
    series_badge,
    series_member_label,
    split_series,
)

from .base_page import BasePage


class _ScanSignals(QObject):
    done = Signal(object)  # list[dict]（ShelfItem.to_rec）


class _ScanTask(QRunnable):
    """后台书架扫描：目录遍历 + 类型检测 + 合并去重全部在 worker 线程。"""

    def __init__(self, service, content_type: str, tag: str, folder: str,
                 keyword: str, sort: str):
        super().__init__()
        self.signals = _ScanSignals()
        self._service = service
        self._ctype = content_type
        self._tag = tag
        self._folder = folder
        self._kw = keyword
        self._sort = sort

    def run(self) -> None:
        try:
            items = self._service.list_items(
                content_type=self._ctype, tag=self._tag, folder=self._folder,
                keyword=self._kw, sort=self._sort,
            )
            recs = [it.to_rec() for it in items]
            try:
                self.signals.done.emit(recs)
            except RuntimeError:
                pass
        except Exception:  # noqa: BLE001 —— 扫描失败保持空书架，不崩溃
            try:
                self.signals.done.emit([])
            except RuntimeError:
                pass


class _ShelfCard(QFrame):
    """书架单张卡片：封面 + 标题 + 类型/作者/集数/续读 + 本地徽章。点击触发。"""

    clicked = Signal(object)  # 发记录 dict
    menu_requested = Signal(object, object)  # (rec, pos) 右键菜单

    def __init__(self, rec: dict, parent=None):
        super().__init__(parent)
        self.rec = rec
        self.setObjectName("shelfCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_menu)
        self.setFixedWidth(210)
        self.setFixedHeight(268)  # 卡片尺寸固定：封面/书名(3行)/作者/来源/状态各占独立行

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        # 封面（独立区域：图片铺满 + 右上角系列角标，与下方信息区分离）
        cover_box = QWidget()
        cover_box.setFixedHeight(90)
        cbox = QGridLayout(cover_box)
        cbox.setContentsMargins(0, 0, 0, 0)
        cover = QLabel("📚")
        cover.setAlignment(Qt.AlignCenter)
        cover.setStyleSheet(
            "background: palette(midlight); border-radius: 8px; font-size: 36px;"
        )
        cbox.addWidget(cover, 0, 0)
        layout.addWidget(cover_box)
        self._cover = cover
        self._load_cover(rec.get("cover") or "")

        # 系列合并角标（如「共 3 話」「第1-3季」，悬停显示成员全名）
        if rec.get("series_badge"):
            badge = QLabel(rec["series_badge"])
            badge.setAlignment(Qt.AlignCenter)
            badge.setStyleSheet(
                "font-size: 10px; font-weight: bold; color: white;"
                "background: rgba(0, 0, 0, 160); border-radius: 3px;"
                "padding: 1px 6px;"
            )
            cbox.addWidget(badge, 0, 0, Qt.AlignTop | Qt.AlignRight)
        if rec.get("series_tooltip"):
            self.setToolTip("该系列成员：\n" + rec["series_tooltip"])

        # 书名（独立单行，超长省略；悬停滑出完整标题浮层）
        title_text = rec.get("title") or "无题"
        title = HoverTitle(title_text, parent_card=self)
        title.setFixedHeight(20)
        title.setStyleSheet("font-size: 15px; font-weight: bold;")
        layout.addWidget(title)
        self._title = title

        # 作者（独立行；本地书无元数据则不显示）
        author_text = (rec.get("author") or "").strip()
        if author_text:
            author = QLabel(author_text)
            author.setWordWrap(False)
            author.setFixedHeight(16)
            author.setStyleSheet("color: palette(dark); font-size: 11px;")
            author.setToolTip(author_text)
            layout.addWidget(author)

        # 来源（独立行：源站名/源 id，纯本地书显示「本地」）
        src = (rec.get("source_name") or rec.get("source_id") or "").strip()
        if not src:
            src = "本地"
        source = QLabel(f"来源：{src}")
        source.setWordWrap(False)
        source.setFixedHeight(16)
        source.setStyleSheet("color: palette(dark); font-size: 11px;")
        source.setToolTip(src)
        layout.addWidget(source)

        # 状态行（独立：本地徽章 + 类型/集数/大小/续读）
        state = QHBoxLayout()
        if rec.get("kind") == "local":
            badge = QLabel("本地")
            badge.setFixedWidth(36)
            badge.setAlignment(Qt.AlignCenter)
            badge.setStyleSheet(
                "font-size: 10px; padding: 1px 0px; border-radius: 3px;"
                "background: palette(highlight); color: palette(highlighted-text);"
            )
            state.addWidget(badge)
        meta_text = self._meta_text()
        meta = QLabel()
        meta.setWordWrap(False)
        meta.setFixedHeight(16)
        meta.setStyleSheet("color: palette(dark); font-size: 10px;")
        meta.setText(meta_text)
        meta.setToolTip(meta_text)
        state.addWidget(meta, stretch=1)
        layout.addLayout(state)
        # 标题收紧为单行后多余高度 → 底部 stretch（卡片等高不破）
        layout.addStretch(1)

        self._apply_style()

    def _load_cover(self, url: str) -> None:
        """异步加载书架卡片封面（CoverLoader 全局限流）。"""
        if not url:
            return  # 无封面保留占位符
        from PySide6.QtGui import QPixmap

        if url.startswith("data:"):
            try:
                _, b64 = url.split(",", 1)
                import base64

                data = base64.b64decode(b64)
                pix = QPixmap()
                if pix.loadFromData(data) and not pix.isNull():
                    self._on_cover_ready(pix)
                    return
            except Exception:  # noqa: BLE001
                pass
            return
        from gui.components.cover_loader import CoverLoader

        CoverLoader.instance().load(
            url, self._on_cover_ready,
            source_id=self.rec.get("source_id") or "",
            persist=True,  # 书架封面持久化：Redis cover: 键命中免重复下载
        )

    def _on_cover_ready(self, pixmap) -> None:
        import shiboken6

        if pixmap is None or not shiboken6.isValid(self._cover):
            return  # 加载失败或控件已销毁，保留占位
        from PySide6.QtCore import Qt as _Qt

        scaled = pixmap.scaled(
            210, 90, _Qt.KeepAspectRatioByExpanding, _Qt.SmoothTransformation
        )
        sx = max(0, (scaled.width() - 210) // 2)
        sy = max(0, (scaled.height() - 90) // 2)
        cropped = scaled.copy(sx, sy, min(210, scaled.width()), min(90, scaled.height()))
        self._cover.setPixmap(cropped)
        from gui.components.cover_loader import CoverLoader, fade_in

        if not CoverLoader.instance().busy():
            fade_in(self._cover)

    def _meta_text(self) -> str:
        rec = self.rec
        ctype = rec.get("content_type", "")
        type_label = {"novel": "小说", "comic": "漫画", "video": "视频", "epub": "epub"}.get(ctype, "")
        parts = []
        if type_label:
            parts.append(type_label)
        if rec.get("episode_count"):
            # 视频=集数；epub 多本文件夹=本数（单选一本读）
            unit = "本" if ctype in ("novel", "comic", "epub") else "集"
            parts.append(f"共 {rec['episode_count']} {unit}")
        if rec.get("online"):
            parts.append("可离线")
        size = rec.get("size_bytes") or 0
        if size > 0:
            parts.append(self._fmt_size(size))
        tags = rec.get("tags") or []
        if tags:
            parts.append(" ".join(f"#{t}" for t in tags[:3]))
        resume = rec.get("resume_title")
        if resume:
            parts.append(f"{'看到' if ctype == 'video' else '读到'} {resume}")
        return " · ".join(parts) or ctype

    @staticmethod
    def _fmt_size(n: int) -> str:
        if n >= 1073741824:
            return f"{n / 1073741824:.1f} GB"
        if n >= 1048576:
            return f"{n / 1048576:.1f} MB"
        if n >= 1024:
            return f"{n / 1024:.0f} KB"
        return f"{n} B"

    def _apply_style(self) -> None:
        self.setStyleSheet(
            "QFrame#shelfCard { border: 1px solid palette(mid); border-radius: 8px;"
            " background: transparent; }"
            "QFrame#shelfCard:hover { border-color: palette(highlight); }"
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.rec)
        super().mousePressEvent(event)

    def _on_menu(self, pos) -> None:
        self.menu_requested.emit(self.rec, self.mapToGlobal(pos))


class LibraryPage(BasePage):
    """书架页 v2。"""

    open_epub_requested = Signal(object)      # epub 文件路径 → reader.open_epub
    open_online_requested = Signal(object)    # 收藏在线书 (source_id, url, content_type) → reader.open
    download_requested = Signal(object)       # 收藏在线书 (source_id, url, content_type) → 下载队列
    play_local_video_requested = Signal(object)  # 本地视频书 rec → 弹集选择播本地 mp4

    def __init__(
        self,
        output_dir: str | Path = "downloads",
        library_store=None,
        reading_progress=None,
        shelf_export_dir: str | Path = "library",
        shelf_service=None,
        cover_backfiller=None,
        parent=None,
    ):
        super().__init__(parent)
        if shelf_service is not None:
            self._shelf = shelf_service
        else:
            from framework.shelf_service import ShelfService

            self._shelf = ShelfService(
                output_dir=output_dir,
                library_store=library_store,
                reading_progress=reading_progress,
            )
        self._store = library_store
        self._shelf_export_dir = Path(shelf_export_dir) if shelf_export_dir else Path("library")
        self._scan_task = None  # 后台扫描任务持有引用（防 GC）
        # 缺封面收藏后台补写回调（App 层注入：内容层 fetch_cover → store.set_cover）
        self._cover_backfiller = cover_backfiller

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        # ---- 顶栏：类型 / 收藏夹 / 搜索 / 排序 / 操作 ----
        top = QHBoxLayout()
        top.addWidget(QLabel("类型"))
        self.type_combo = QComboBox()
        self.type_combo.addItems(["全部", "小说", "漫画", "视频"])
        self.type_combo.currentTextChanged.connect(lambda _: self._rebuild())
        top.addWidget(self.type_combo)

        top.addSpacing(12)
        top.addWidget(QLabel("收藏夹"))
        self.folder_combo = QComboBox()
        self.folder_combo.currentTextChanged.connect(lambda _: self._rebuild())
        top.addWidget(self.folder_combo)

        self.new_folder_btn = QPushButton("新建收藏夹")
        self.new_folder_btn.clicked.connect(self._new_folder)
        top.addWidget(self.new_folder_btn)

        top.addSpacing(12)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索书架…")
        self.search_edit.setFixedWidth(160)
        self.search_edit.returnPressed.connect(self._rebuild)
        top.addWidget(self.search_edit)

        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["最近阅读", "最近添加", "书名"])
        self.sort_combo.currentTextChanged.connect(lambda _: self._rebuild())
        top.addWidget(self.sort_combo)

        self.export_btn = QPushButton("导出书架")
        self.export_btn.clicked.connect(self._export_shelf)
        top.addWidget(self.export_btn)

        top.addStretch(1)
        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color: palette(dark);")
        top.addWidget(self.count_label)
        layout.addLayout(top)

        # ---- 滚动区 ----
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.container = QWidget()
        self.body = QVBoxLayout(self.container)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(16)
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll, stretch=1)

        self._rebuild()

    # ------------------------------------------------------------------ #
    def _rebuild(self) -> None:
        """重建列表：后台扫描（含搜索/排序/合并）→ 主线程渲染。"""
        self._clear_all()
        self.count_label.setText("扫描中…")
        self._sync_combos()

        ctype = self.type_combo.currentText()
        type_map = {"全部": "", "小说": "novel", "漫画": "comic", "视频": "video"}
        want = type_map.get(ctype, "")
        folder = self.folder_combo.currentText()
        keyword = self.search_edit.text().strip()
        sort_map = {"最近阅读": "recent", "最近添加": "added", "书名": "name"}
        sort = sort_map.get(self.sort_combo.currentText(), "recent")
        self._last_ctype = want
        self._last_folder = folder
        self._last_kw = keyword
        self._last_sort = sort

        task = _ScanTask(self._shelf, want, "", folder, keyword, sort)
        task.signals.done.connect(self._render)
        self._scan_task = task
        QThreadPool.globalInstance().start(task)

    def _sync_combos(self) -> None:
        """刷新收藏夹下拉（保持当前选择）。"""
        if self._store is None:
            return
        cur = self.folder_combo.currentText()
        self.folder_combo.blockSignals(True)
        self.folder_combo.clear()
        self.folder_combo.addItem("全部")
        self.folder_combo.addItems(self._shelf.folders())
        idx = self.folder_combo.findText(cur)
        self.folder_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.folder_combo.blockSignals(False)

    def _render(self, books: list[dict]) -> None:
        """主线程渲染扫描结果（本地在前）。"""
        if not self._visible_combo_state():
            return  # 筛选/搜索/排序已变化 → 旧结果丢弃，等新任务
        if not books:
            self._add_empty()
            self.count_label.setText("书架还空着")
            return
        locals_ = [b for b in books if b.get("kind") == "local"]
        favorites = [b for b in books if b.get("kind") == "favorite"]
        self.count_label.setText(f"共 {len(books)} 本 · 本地{len(locals_)} / 收藏{len(favorites)}")
        if locals_:
            self._add_group("本地", [{"rec": b} for b in locals_])
        if favorites:
            # 收藏先按系列合并（同源+同主书名 → 一张卡）→ 再从每条开卡
            items: list[dict] = []
            for grp in group_favorites(favorites):
                items.append(self._to_shelf_item(grp))
            self._add_group("收藏", items)
        self._maybe_backfill_covers(favorites)

    def _maybe_backfill_covers(self, favorites: list[dict]) -> None:
        """收藏记录封面无效 → 后台补详情封面（幂等、失败静默、不阻塞渲染）。

        有效性判定交给 App 层 backfiller（空 / 等于 base_url / 命中占位图都
        算无效——历史 bug 曾把 base_url 这类垃圾写进收藏库）。每次书架渲染都
        触发：已补齐的记录 cover 已写回不再进 need；补写失败的记录下次渲染
        重试。真实网络只在详情页缓存缺失时发生（fetch_cover 复用 page: 缓存），
        串行后台执行不压垮站点。
        """
        if self._cover_backfiller is None:
            return
        need = [b for b in favorites
                if b.get("source_id") and b.get("url")]
        if need:
            try:
                self._cover_backfiller(need)
            except Exception:  # noqa: BLE001 —— 补封面失败不影响书架显示
                pass

    def _visible_combo_state(self) -> bool:
        ctype = self.type_combo.currentText()
        type_map = {"全部": "", "小说": "novel", "漫画": "comic", "视频": "video"}
        want = type_map.get(ctype, "")
        folder = self.folder_combo.currentText()
        keyword = self.search_edit.text().strip()
        sort_map = {"最近阅读": "recent", "最近添加": "added", "书名": "name"}
        sort = sort_map.get(self.sort_combo.currentText(), "recent")
        return (
            want == getattr(self, "_last_ctype", "")
            and folder == getattr(self, "_last_folder", "")
            and keyword == getattr(self, "_last_kw", "")
            and sort == getattr(self, "_last_sort", "")
        )

    @staticmethod
    def _wipe(layout) -> None:
        """递归删除 layout 下所有 widget（含嵌套子 layout）。"""
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                w = item.widget()
                w.setParent(None)
                w.deleteLater()
            elif item.layout():
                LibraryPage._wipe(item.layout())

    def _clear_all(self) -> None:
        LibraryPage._wipe(self.body)

    # ------------------------------------------------------------------ #
    def _add_group(self, title: str, items: list[dict]) -> None:
        """渲染一组卡片。items 每项为 {"rec": 记录, "members"?=系列成员列表}。

        members>1 → 合并卡（点开弹成员选集）；否则普通卡。
        """
        header = QLabel(title)
        header.setStyleSheet("font-size: 14px; font-weight: bold; color: palette(text);")
        self.body.addWidget(header)
        grid = QGridLayout()
        grid.setSpacing(12)
        for i, item in enumerate(items):
            card = _ShelfCard(item["rec"])
            # clicked 信号携带 rec 参数 → 首参吞掉它，闭包 it=item 才能拿到条目
            card.clicked.connect(
                lambda _rec, it=item: self._on_card_clicked(it)
            )
            card.menu_requested.connect(
                lambda r, p, it=item: self._show_card_menu(it["rec"], p)
            )
            row, col = divmod(i, 3)
            grid.addWidget(card, row, col)
        self.body.addLayout(grid)

    @staticmethod
    def _to_shelf_item(group: list[dict]) -> dict:
        """系列组 → 书架条目（{"rec", "members"?}）。

        单本组 → 普通条目 {rec}；多本合并 → 显示记录为首部（标题换成主书名，
        去系列后缀），注入 series_badge（角标）与 series_tooltip（悬停成员
        列表）。存储不动——收藏仍各自独立，右键操作落在首部记录上。
        """
        if len(group) <= 1:
            return {"rec": group[0]}
        leader = dict(group[0])
        base, _ = split_series(leader.get("title") or "")
        if base:
            leader["title"] = base
        leader["series_badge"] = series_badge(group)
        leader["series_tooltip"] = "\n".join(
            series_member_label(m) or (m.get("title") or "未命名")
            for m in group
        )
        return {"rec": leader, "members": group}

    def _add_empty(self) -> None:
        empty = QLabel("书架还空着，去发现里找点好东西吧\n（可搜索、可分类、本地书可直接离线阅读）")
        empty.setAlignment(Qt.AlignCenter)
        empty.setStyleSheet("color: palette(mid); font-size: 14px; padding: 60px;")
        self.body.addWidget(empty)

    # ------------------------------------------------------------------ #
    def _new_folder(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        name, ok = QInputDialog.getText(self, "新建收藏夹", "收藏夹名称：")
        if not ok or not name.strip():
            return
        name = name.strip()
        self._shelf.create_folder(name)
        idx = self.folder_combo.findText(name)
        if idx < 0:
            self.folder_combo.addItem(name)
            idx = self.folder_combo.count() - 1
        self.folder_combo.setCurrentIndex(idx)

    def _show_card_menu(self, rec: dict, pos) -> None:
        """右键菜单：本地书 + 收藏操作（合并条目两者都提供）。"""
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        is_local = rec.get("kind") == "local"
        is_online = rec.get("online") or rec.get("kind") == "favorite"

        if is_local:
            menu.addAction("📂 打开所在文件夹").triggered.connect(
                lambda: self._open_folder(rec)
            )
            menu.addAction("🗑 删除本地文件").triggered.connect(
                lambda: self._delete_local(rec)
            )
            menu.addAction("从书架移除").triggered.connect(
                lambda: self._remove_local(rec)
            )
        if is_online:
            if is_local:
                menu.addSeparator()
            url = rec.get("url", "")
            if url:
                menu.addAction("⬇ 下载到本地").triggered.connect(
                    lambda: self._request_download(rec)
                )
                menu.addAction("📋 复制地址").triggered.connect(
                    lambda: self._copy_url(rec)
                )
                menu.addAction("打开源详情页").triggered.connect(
                    lambda: self._open_online(rec)
                )
            menu.addSeparator()
            sub = menu.addMenu("移动到收藏夹")
            for f in self._shelf.folders():
                if f == rec.get("folder", ""):
                    continue
                act = sub.addAction(f)
                act.triggered.connect(lambda _, _f=f: self._move_favorite(rec, _f))
            sub.addAction("（不归类）").triggered.connect(
                lambda: self._move_favorite(rec, "")
            )
            menu.addAction("移除收藏").triggered.connect(
                lambda: self._remove_favorite(rec)
            )
        menu.exec(pos)

    def _copy_url(self, rec: dict) -> None:
        from PySide6.QtWidgets import QApplication

        url = rec.get("url", "")
        if url:
            QApplication.clipboard().setText(url)

    def _request_download(self, rec: dict) -> None:
        """收藏在线书右键「下载到本地」→ 转交 App 层拉详情入下载队列。"""
        url = rec.get("url", "")
        if not url:
            return
        self.download_requested.emit(
            (rec.get("source_id", ""), url, rec.get("content_type", ""))
        )

    def _open_online(self, rec: dict) -> None:
        import webbrowser

        url = rec.get("url", "")
        if url:
            webbrowser.open(url)

    def _delete_local(self, rec: dict) -> None:
        """本地书：删除本地文件（需确认）。"""
        from PySide6.QtWidgets import QMessageBox

        path = Path(rec.get("path", ""))
        if not path.exists():
            QMessageBox.information(self, "删除", "本地文件已不存在")
            return
        resp = QMessageBox.question(
            self, "删除本地文件",
            f"确定删除《{rec.get('title')}》的本地文件吗？\n{path}\n\n该操作不可恢复。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if resp != QMessageBox.Yes:
            return
        try:
            import shutil

            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass
        self._rebuild()

    def _remove_local(self, rec: dict) -> None:
        """本地书：从书架列表移除（不删文件，仅取消显示）。"""
        from PySide6.QtWidgets import QMessageBox

        resp = QMessageBox.question(
            self, "从书架移除",
            f"确定将《{rec.get('title')}》从书架移除吗？（本地文件保留）",
            QMessageBox.Yes | QMessageBox.No,
        )
        if resp != QMessageBox.Yes:
            return
        self._shelf.hide_local(rec.get("title", ""))
        self._rebuild()

    def _move_favorite(self, rec: dict, folder: str) -> None:
        self._shelf.favorite_move(rec.get("url", ""), folder)
        self._rebuild()

    def _remove_favorite(self, rec: dict) -> None:
        self._shelf.favorite_remove(rec.get("url", ""))
        self._rebuild()

    def _open_folder(self, rec: dict) -> None:
        import os
        import subprocess

        path = Path(rec.get("path", "")).parent
        if not path.is_dir():
            return
        if os.name == "nt":
            os.startfile(str(path))
        elif os.name == "posix":
            subprocess.Popen(["xdg-open", str(path)])

    # ------------------------------------------------------------------ #
    def _export_shelf(self) -> None:
        from PySide6.QtWidgets import QFileDialog, QMessageBox

        if self._store is None:
            QMessageBox.information(self, "导出书架", "暂无书架数据可导出。")
            return
        from datetime import date

        default_dir = str(self._shelf_export_dir)
        default_name = f"书架导出_{date.today().strftime('%Y%m%d')}.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出书架", str(Path(default_dir) / default_name),
            "JSON 文件 (*.json)",
        )
        if not path:
            return
        try:
            out = self._shelf.export_backup(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "导出书架", f"导出失败：{exc}")
            return
        QMessageBox.information(self, "导出书架", f"已导出到：\n{out}")

    # ------------------------------------------------------------------ #
    def _on_card_clicked(self, item: dict) -> None:
        """点击书架卡片。item = {"rec": 记录, "members"?=系列成员列表}。

        系列合并卡（members>1）→ 弹成员选集列表，逐个打开；
        单本 → 原打开逻辑（本地优先加载，无本地走网络）。
        """
        members = item.get("members")
        if members:
            self._pick_series_member(item["rec"], members)
            return
        self._open_rec(item["rec"])

    def _pick_series_member(self, leader: dict, members: list[dict]) -> None:
        """系列合并卡：弹出该系列各话/各季成员列表，选一个打开。"""
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        for m in members:
            label = series_member_label(m) or (m.get("title") or "未命名")
            title = (m.get("title") or "").strip()
            if title and label != title:
                label = f"{label}（{title}）"  # 主名带全名（防重名歧义）
            act = menu.addAction(label)
            act.triggered.connect(
                lambda checked=False, mm=m: self._open_rec(mm)
            )
        menu.setTitle(f"《{leader.get('title', '')}》选集")
        menu.exec()  # 默认显示在光标处

    def _open_rec(self, rec: dict) -> None:
        """打开单个书架记录：本地优先加载（视频播本地、epub 读本地），无本地才走网络。

        一个文件夹多本 epub（episode_paths>1）→ 弹下拉框选读哪本（同视频选集）。
        """
        if not rec:
            return
        if rec.get("kind") == "local":
            # 本地优先；但收藏组并卡时 serial members 可能不是本地
            if rec.get("content_type") == "video" and rec.get("episode_paths"):
                self.play_local_video_requested.emit(rec)
            elif rec.get("episode_paths") and len(rec["episode_paths"]) > 1:
                self._pick_epub(rec)
            elif rec.get("path"):
                self.open_epub_requested.emit(rec["path"])
        elif rec.get("url"):
            self.open_online_requested.emit(
                (rec.get("source_id", ""), rec.get("url", ""), rec.get("content_type", ""))
            )

    def _pick_epub(self, rec: dict) -> None:
        """多本 epub：弹下拉框选一本读（同视频选集模式）。"""
        from PySide6.QtWidgets import QInputDialog

        paths = list(rec.get("episode_paths") or [])
        names = [Path(p).name for p in paths]
        item, ok = QInputDialog.getItem(
            self, "选择书籍", f"《{rec.get('title', '')}》文件夹含多本，选一本阅读：",
            names, 0, False,
        )
        if not ok or not item:
            return
        idx = names.index(item)
        self.open_epub_requested.emit(paths[idx])

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        self._rebuild()
