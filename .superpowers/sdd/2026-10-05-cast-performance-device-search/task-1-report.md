# Task 1：手动设备搜索

## 变更

- 新增可见按钮 `搜索设备`，通过 `_on_search_clicked` 调用现有 `_start_scan`。
- `showEvent` 仅调用 `super().showEvent(event)`，打开对话框不再自动执行 SSDP 搜索。
- 无历史设备提示改为引导用户点击「搜索设备」；历史设备仍立即显示、置顶并默认选中。
- 扫描期间搜索与刷新按钮禁用，扫描结束或出错后恢复；刷新按钮保留并可再次启动 worker。
- 保留既有线程脱离/回收和历史设备合并、置顶行为。

## TDD 验证

1. RED：先修改 `tests/test_cast_dialog.py`，运行目标测试得到 `4 failed, 20 passed`，失败原因分别对应缺少搜索按钮、`show()` 自动扫描、提示文本未更新。
2. GREEN：实现 `gui/components/cast_dialog.py` 后运行：`24 passed`。
3. 计划要求的补充验证：Python 3.10 目标测试、`compileall`、`git diff --check`。

## 提交

- Commit：`fix(cast): make device discovery user initiated`
