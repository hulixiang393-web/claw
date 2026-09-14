# 搜索页多选源 + 结果筛选 设计文档

日期：2026-09-08
状态：已实现

## 背景

用户需求：搜索页支持**多选源搜索** + 结果内**按来源筛选**。

现状调研：搜索页已具备大部分能力，仅「源范围」选择了 `src_combo`（单源下拉：全部源 / 单源），无法多选。

## 现状（`gui/pages/search_page.py`）

已有能力：
- `_SearchTask`（56-92）：每源一个后台任务，`search_one(source, keyword, on_page=...)` 边抓边显示 + `finished` 全量。**搜索走实时路径**（用户已拍板不接缓存）。
- 源范围：`src_combo`（173）单源下拉（"全部源" / 单选某源）；`type_combo` 类型筛选；`merge_check` 合并相似。
- 每源状态 chip 行（`_build_status_bar` 316）。
- 结果内来源筛选：已有 `filter_bar_widget` + `_set_filter`（880）/ `_clear_filter`（889）/ 来源角标 chip。
- 结果网格：4 列 `WorkCard`，滚动懒加载（`_on_scroll` 895）。

## 缺口

1. 源选择：只能「全部源」或**单源**，无法勾选多个特定源。
2. 结果筛选已有，但需确认「多选源」与「结果内来源筛选」不冲突（见下）。

## 设计 A：源选择改为「多选源弹层」（推荐）

把 `src_combo`（QComboBox）替换为：

- `QPushButton(f"源：{n}个 / 全部")` 点击弹出 `QMenu`（复选菜单）。
- 菜单项：顶部「全部」复选项 + 每启一个源一个复选项（`checkable=True`）。
- 逻辑：
  - 默认「全部」勾选。
  - 勾选「全部」→ 其余源项全勾（且源项不可手动取消，只能取消「全部」？否——细节见下）。
  - 若用户取消「全部」→ 按勾选的源搜索；源全取消 → 等效「全部」。
  - 按钮文本实时反映：全部 → `f"源：全部"`；部分 → `f"源：{n}个"`。

**简化实现**（避免复杂互斥状态机）：
- 「全部」不是菜单项，而是按钮点击弹出菜单顶部放一个「✔ 全部」action（checkable）；再跟一个分隔线 + 各源。
- 勾选状态只记一个集合：`self._selected_sources: set[source_id]`。
  - 空集合 = 全部源（语义等价，且是初始态）。
  - 点某源勾选 → 加入集合；取消 → 移除。
  - 点「全部」→ 清空集合（= 全部）。
  - 展示：集合空 → `源：全部`；非空 → `源：{len(集合)}个`。

## 设计 B：保留下拉 + 弹层多选（折中）

保留 `src_combo`（兼容大量既有代码 `_rebuild_src_combo`/`currentData`），旁边加「多选」按钮弹 `QMenu`。

影响面：新增约 60 行 + 改 `_on_search` 的源集合选取；`_rebuild_src_combo` 保留。

## 推荐

选 **设计 A**：`src_combo` 整体替换为多选按钮。理由：
- 心智统一：一个入口表达「源范围」；
- 现有 `_on_search` 只需改 3 行（源集合来源），`_rebuild_src_combo` 换成 `_rebuild_sources_menu`（refresh() 调用不变）；
- 多选源与「结果内筛选」天然互补：多选=并行抓取并集，结果筛选=看单一来源。

## 结果内筛选（现状已满足，仅微调）

- 现有 `_set_filter(source_id)` 点击来源角标 → 只显示该源结果，`filter_bar_widget` 显示「仅看 XX」。已满足需求，不改。
- 需要补：多选源搜索后，结果落 `_results` 仍带各自 `source_id`（SearchResult 已含），筛选逻辑天然正确。

## 设计地雷排查

- `_on_search`（266）：`selected_src = self.src_combo.currentData()` → 改为使用 `_selected_sources`：
  ```python
  selected = self._selected_sources  # 空 = 全部
  if selected:
      sources = [s for s in self._manager.enabled_sources() if s.source_id in selected]
      if selected_type:
          sources = [s for s in sources if s.content_type == selected_type]
  else:  # 全部
      sources = self._manager.enabled_sources()
      if selected_type:
          sources = [s for s in sources if s.content_type == selected_type]
  ```
- `refresh()`（906）调用 `_rebuild_src_combo` → 换成 `_rebuild_sources_menu`。
- `src_combo` 引用点：仅在 `_on_search`（286）、`refresh`/`_rebuild_src_combo`（906-920）。无其他消费。
- 状态条（`_build_status_bar`）按 sources 列表构建，多选源自动适配，无需改。

## 明确不动

- `_SearchTask` / `search_one`（实时，不接缓存）
- 结果网格、懒加载、封面泵
- `filter_bar` / `_set_filter` / 来源角标
- `merge_check` 合并相似
- 首屏/批量操作

## 验收标准

1. 「源」按钮默认显示全部；点开菜单可勾选多个/取消；
2. 只勾选 2 个源搜索 → 只有这两个源的状态 chip + 结果（其余源不请求）；
3. 结果内点来源角标 → 只看该源；清除筛选恢复；
4. 「全部」勾选 → 等效原「全部源」行为；
5. `pytest tests/` 全量通过；`gui/_smoke_search.py` 通过（新增多选源断言）。

## 风险

- 低。改动集中在 `_on_search` 一小段 + 新增一个按钮/菜单；不触碰 task/渲染链路。
- 唯一注意：换源后（源列表变化）调 `refresh()` 时重建菜单，保持已勾选集合（不在新列表的源自动丢弃）。