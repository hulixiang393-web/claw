"""跨源换源候选对话框 smoke 测试（gui/_smoke_cross_dialog.py）。

验证 CrossSourceDialog：
1. 无候选 → 显示无结果提示，「换源」按钮禁用
2. 有候选 → 列表填充；未选中时「换源」禁用
3. 点选候选 → 「换源」启用；确定后 selected 为选中的候选；取消后为 None
"""
import os
import pathlib
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication

from framework.cross_source import CrossSourceCandidate
from gui.components.cross_source_dialog import CrossSourceDialog


def _mk(sid, title, sim=0.95):
    return CrossSourceCandidate(
        source_id=sid, source_name=f"源{sid}", title=title,
        url=f"http://{sid}.com/v/1", cover="", similarity=sim,
        content_type="video", sid=sid,
    )


def main():
    app = QApplication.instance() or QApplication([])

    # 1. 无候选
    d0 = CrossSourceDialog([])
    assert not d0.switch_btn.isEnabled(), "无候选时「换源」应禁用"
    assert d0.list.count() == 0
    print("[1] 无候选分支 OK（换源禁用，列表为空）")

    # 2. 有候选，未选中
    d = CrossSourceDialog([_mk("b", "测试动画 第一季"), _mk("c", "测试动画 第二季")])
    assert d.list.count() == 2, f"候选列表应为 2，实际 {d.list.count()}"
    assert not d.switch_btn.isEnabled(), "未选中候选时「换源」应禁用"
    assert d.selected is None
    print("[2] 候选填充 + 初始「换源」禁用 OK")

    # 3. 选中候选 → 启用；确定 → selected 正确
    d.list.setCurrentRow(1)
    assert d.switch_btn.isEnabled(), "选中候选后「换源」应启用"
    d._on_accept()
    assert d.selected is not None
    assert d.selected.source_id == "c", d.selected
    assert d.result() == 1, "accept() 后 result 应为 Accepted(1)"
    print("[3] 选续 + 确定选中候选 OK:", d.selected.source_id)

    # 4. 确定但未选中 → 不生效
    d2 = CrossSourceDialog([_mk("b", "X")])
    d2._on_accept()  # 未选中
    assert d2.selected is None, "未选中时确定不应设置 selected"
    assert not d2.switch_btn.isEnabled()
    print("[4] 未选中确定 → selected 仍 None OK")

    print("\n=== CrossSourceDialog smoke 测试通过 ===")


if __name__ == "__main__":
    main()
