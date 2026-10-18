"""快速切换源稳定性测试（模拟快速连点不卡死）。"""
import os, sys, time, pathlib
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QEventLoop, QTimer


def main():
    app = QApplication.instance() or QApplication([])
    from gui.app import MainWindow

    win = MainWindow()
    win.show()
    app.processEvents()

    discover = win.tabs.widget(1)
    print("可发现源:", [discover.source_combo.itemText(i) for i in range(discover.source_combo.count())])

    # 快速切换源 10 次（模拟连点），不应卡死
    t0 = time.time()
    for i in range(10):
        idx = i % discover.source_combo.count()
        discover.source_combo.setCurrentIndex(idx)
        app.processEvents()  # 处理 UI 事件，不应阻塞
    t1 = time.time()
    print(f"快速切换 10 次耗时: {t1-t0:.2f}s（应 < 3s，不卡死）")
    assert t1 - t0 < 3, f"切换太慢/卡死: {t1-t0:.2f}s"

    # 等后台任务收敛
    loop = QEventLoop()
    QTimer.singleShot(3000, loop.quit)
    loop.exec()
    app.processEvents()
    print("最终状态:", discover.status_label.text())
    print("source_epoch:", discover._source_epoch)

    print("\n=== 快速切换源稳定性测试通过 ===")


if __name__ == "__main__":
    main()
