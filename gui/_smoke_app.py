"""App 启动离屏验证（MainWindow）。"""
import os
import sys
import pathlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication
from gui.app import MainWindow, TABS, IMPLEMENTED_TABS


def main():
    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    assert win.tabs.count() == len(TABS), f"Tab 数={win.tabs.count()}, 期望 {len(TABS)}"

    labels = [win.tabs.tabText(i) for i in range(win.tabs.count())]
    print("导航栏:", labels)
    expected = ["首页", "发现", "搜索·开发中", "阅读", "下载·开发中", "书架·开发中", "源管理·开发中", "设置·开发中"]
    assert labels == expected, labels

    # 首页已实现，其余占位
    assert "首页" in labels
    print("已实现 Tab:", IMPLEMENTED_TABS)
    print("占位 Tab:", [l for l, k in TABS if k not in IMPLEMENTED_TABS])

    # 主题已应用
    qss = app.styleSheet()
    assert len(qss) > 100, "QSS 未应用"
    print("主题 QSS 已应用，长度 =", len(qss))

    win.show()
    app.processEvents()
    print("\n=== App 启动验证通过 ===")


if __name__ == "__main__":
    main()
