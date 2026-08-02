"""回归验证：用真实 sources/*.json 验证首页统计。"""
import os
import sys
import pathlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication
from gui.app import MainWindow


def main():
    app = QApplication.instance() or QApplication([])
    win = MainWindow()

    # 真实源加载
    sm = win.source_manager
    print("加载源:", [s.source_id for s in sm.all()])
    print("统计:", sm.count_by_type(), "启用=", sm.count_enabled(), "失效=", sm.count_broken())

    # 首页统计卡片值
    home = win.tabs.widget(0)
    assert home.stats_row.card_novel.value_label.text() == str(sm.count_by_type()["novel"])
    assert home.stats_row.card_comic.value_label.text() == str(sm.count_by_type()["comic"])
    assert home.stats_row.card_video.value_label.text() == str(sm.count_by_type()["video"])
    assert home.stats_row.card_enabled.value_label.text() == str(sm.count_enabled())
    print("首页统计卡片:",
          f"小说={home.stats_row.card_novel.value_label.text()}",
          f"漫画={home.stats_row.card_comic.value_label.text()}",
          f"视频={home.stats_row.card_video.value_label.text()}",
          f"启用={home.stats_row.card_enabled.value_label.text()}")

    # 有源 → 空状态隐藏
    assert home.empty_state.isHidden() is True
    print("空状态: 隐藏（有源） OK")

    # 有 discovery 的源（comic 配了 discovery）
    print("可发现源:", [s.source_id for s in sm.discoverable_sources()])

    # 健康历史文件生成
    sm.update_health("demo_novel", "ok")
    sm.update_health("demo_comic", "broken", "结构变更")
    print("失效源数:", sm.count_broken())
    assert sm.count_broken() == 1

    print("\n=== 回归验证通过 ===")


if __name__ == "__main__":
    main()
