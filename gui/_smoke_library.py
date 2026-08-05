"""书架页离屏测试（LibraryPage）：收藏/收藏夹/导出。"""
import os
import sys
import pathlib
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication

from framework.library_store import LibraryStore
from gui.pages.library_page import LibraryPage


def main():
    app = QApplication.instance() or QApplication([])
    base = pathlib.Path(tempfile.mkdtemp())
    store = LibraryStore(base / "lib.json")

    # 收藏 + 收藏夹
    store.add("src", "http://a/1", "收藏1", content_type="novel", author="甲")
    store.add("src", "http://b/2", "收藏2", content_type="comic", author="乙")
    store.add("src", "http://c/3", "收藏3", content_type="comic", author="丙")
    store.create_folder("玄幻")
    store.add("src", "http://d/4", "收藏4", content_type="video", folder="玄幻")

    page = LibraryPage(
        output_dir=base / "dl",
        library_store=store,
        shelf_export_dir=base / "export",
    )
    page.resize(800, 600)
    page.show()
    app.processEvents()

    # 收藏数
    assert store.count() == 4, store.count()
    print("收藏数 OK:", store.count())

    # 收藏夹
    folders = store.list_folders()
    assert "玄幻" in folders, folders
    items = store.folder_items("玄幻")
    assert len(items) == 1 and items[0]["title"] == "收藏4"
    print("收藏夹 OK:", folders)

    # 导出功能
    out = base / "export" / "backup.json"
    p = store.export_backup(out)
    assert p.exists(), "导出文件未生成"
    import json

    data = json.loads(p.read_text(encoding="utf-8"))
    assert len(data["favorites"]) == 4, data["favorites"]
    assert "玄幻" in data["folders"]
    print("导出 OK:", p.name, "含收藏", len(data["favorites"]), "个")

    # 导出按钮存在于库页
    assert hasattr(page, "export_btn"), "缺导出按钮"
    assert page.export_btn.text() == "导出书架"
    print("导出按钮 OK")

    print("\n=== 书架离屏测试通过 ===")


if __name__ == "__main__":
    main()