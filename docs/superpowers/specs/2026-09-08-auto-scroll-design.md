# 漫画/小说阅读器自动滚动 设计文档

日期：2026-09-08
状态：已实现（含速度滑块 1~10 档，默认 5 保持 60px/tick 手感）

## 背景

用户需求：漫画/小说阅读器支持**自动滚动**（解放双手，连续阅读场景）。
原话：「怎么进行自动翻页，没有快捷键吗？只需要在阅读界面显示就行」——
即阅读界面（工具栏）只显示一个「自动滚动」按钮 + 快捷键开关，速度固定一个合理值。

## 现状调研

### 小说 `gui/pages/reader/novel_view.py`

- 两种模式：`scroll`（默认，`QScrollArea` + `QLabel`）+ `pager`（翻页）。
- 滚动模式：`self.scroll.verticalScrollBar()`，`valueChanged` → `_on_scroll_position`（349，节流 1.5s 存盘续读位置）。
- 到底部不自动翻章；底部导航有「上一章/下一章」按钮。

### 漫画 `gui/pages/reader/comic_view.py`

- 两种模式：`gallery`（默认，`QScrollArea` 纵向滚动）+ `flip`（横向翻页）。
- 滚动相关：
  - `_on_scroll_lazy`（623）：滚动逼近已渲染末端 → 增量渲染下一批图（懒加载，`LAZY_BATCH`）。
  - `_on_scroll_prefetch`（579）：读到当前话 70% → 后台预渲染下一话。
  - `_on_scroll_position`（646）：节流存盘续读。
- 读到底部不自动切话。

### 共通点

两个视图滚动都由 `verticalScrollBar().setValue()` 驱动；现有懒加载/续读存盘都挂在 `valueChanged`，**自动滚动天然兼容**（程序化 setValue 一样触发）。唯一要防的是「自动滚动 vs 用户交互」冲突与「到底停止」。

## 设计

### 1. 工具栏加「自动滚动」按钮 + 快捷键

两个视图工具栏各加：

- `auto_scroll_btn = QPushButton("▶ 自动滚动")`（toggle：`▶`→`⏸ 停止`），点击开始/停止。
- 快捷键 **Ctrl+Alt+A**（QShortcut，视图级）toggle。不与现有键冲突（小说/漫画均用
  方向键/PgUp/PgDn/侧键/Ctrl+滚轮，中文输入法不抢占该组合）。
- 速度滑块：`auto_scroll_speed_slider`（QSlider 横向，固定宽 100px，范围 1~10，默认 5）
  + 窄档位 `auto_scroll_speed_label`（显示当前档位数字）+ tooltip「自动滚动速度（1最慢，10最快）」，
  追加在自动滚动按钮之后。默认档 5 保持原 60px/tick 的手感。

追加到现有工具栏布局（novel 工具栏 64-99；comic 工具栏对应段）。

### 2. 滚动执行 + 速度调节

- `QTimer`（interval = 80ms）
  - novel：每次 `vbar.setValue(vbar.value() + slider_value * 12)`。
  - comic：同上，懒加载触发渲染后 `maximum()` 增长——用 `value + slider_value * 12` 持续推进，天然跟随。
- 到 `vbar.maximum()` → 自动停止（novel 停在章尾、comic 停在话尾），不自动翻章/话（**本期不做自动翻页**）。

**速度滑块实现（选 px/位移，不调 timer interval）**：`_auto_scroll_tick` 每 tick 读一次
`auto_scroll_speed_slider.value()` 作为速度，档位 1~10 映射每 tick 位移 `12~120px`（0.25~3x 于默认）。
采用位移而非 interval 的理由：不需在改档后 `restart` QTimer，且拖动滑块时下一次 tick 即实时生效
——**正在自动滚动时拖滑块不打断滚动**（不调用 `_stop_auto_scroll`），满足「实时调节」。

**滑块可用性**：`_update_auto_scroll_slider_state()` 在滚动模式且 `vbar.maximum() > 0` 时启用滑块，
否则禁用（与 `_toggle_auto_scroll` 守卫一致）。初始禁用；内容加载完成后（novel 在 `_display_chapter`
末尾经 `QTimer.singleShot(0, ...)`、comic 在 `_finish_episode_load` 末尾）与 `_toggle_mode` 切回滚动
模式时重新评估启用状态。

### 3. 停止条件（用户交互优先）

自动滚动期间任一事件触发即停、按钮复位「▶」：

- 用户普通滚轮（无 Ctrl）——但 Ctrl+滚轮 = 调字号/缩放，**不停止**。
- 键盘 PageUp/Down/方向键。
- 点目录跳章/话、点上一章/下一章/上一话/下一话、切模式（scroll↔pager / gallery↔flip）、换书。

实现：

- 两个视图各自 `wheelEvent`：`event.modifiers() & ControlModifier` 之外 → `_stop_auto_scroll()`；
  Ctrl+滚轮分支不动（comic 缩放 / novel 调字号照常）。
- `_load_chapter` / `_load_episode` 开头调 `_stop_auto_scroll()`（目录跳章、侧键翻章、上一章/下一章全走此路径）。
- `_toggle_mode` 开头调 `_stop_auto_scroll()`。
- `keyPressEvent`：novel scroll 分支 / comic 统一入口调 `_stop_auto_scroll()`。
- comic 懒加载/预取在滚动中自然触发，无需改动。
- 换书走 `load()` → `_load_chapter`/`_load_episode` → 同样停止。

### 4. 边界

- 自动滚动开启时切到 `pager`/`flip` 模式 → 自动停止（该模式无纵向滚动）。
- 在 `pager`/`flip` 模式点击按钮或按快捷键 → 不启动（提示即按钮 tooltip 已说明「仅滚动模式可用」）。
- 开启自动滚动前若已在章尾/话尾 → 第一个 tick 立即停止（无意义，按钮复位）。

### 5. 状态栏

滚动手动 vs 程序化：续读存盘 `_on_scroll_position` 节流 1.5s 已存在，自动滚动触发的高频 `valueChanged` 不会写爆磁盘（节流天然兜底），不改。

## 明确不动

- 懒加载批尺寸、预渲染窗口、续读逻辑、翻页/横向模式。
- 分享/下载/收藏等无关功能。

## 验收标准

1. 小说/漫画滚动模式：点「▶ 自动滚动」→ 正文/漫画匀速下滚（默认 60px/80ms）；到底停止、按钮复位「▶」。
2. 自动滚动中用户滚轮/方向键 → 立即停止，且后续手动操作正常（续读存盘、懒加载、预渲染全不受影响）。
3. Ctrl+滚轮调字号/缩放时自动滚动**不**停止。
4. 切 pager/flip 模式或换章/换话 → 自动滚动自动停止。
5. 自动滚动中拖速度滑块（1~10）→ 实时变速且**不中断**滚动；默认档 5 保持 60px/tick 手感。
6. 无滚动内容（`vbar.maximum()<=0`）或非滚动模式（pager/flip）时滑块禁用，进入滚动模式加载完内容后恢复可用。
7. `pytest tests/` 全量通过；`gui/_smoke_reader.py` 通过（新增自动滚动按钮存在 + 点击后 `_auto_scrolling` 状态翻转断言）。

## 验证记录（2026-09-08）

- `python -m pytest tests/ -q` → 109 passed，2 failed（`tests/test_search_page_multisource.py` 两个用例，
  为搜索页多源搜索预有问题，与本任务无关，改动前即失败）。
- `python gui/_smoke_reader.py` → 通过（含新增按钮/toggle 断言）。
- 临时离屏脚本（已删除）逐项验证：novel/comic 开启后滚动值递增、用户滚轮停止+按钮复位、
  Ctrl+滚轮不停止（调字号/缩放）、方向键停止、切模式停止、切章/切话停止、滚到底自动停止+按钮复位、
  pager/flip 模式按钮不启动。
- 速度滑块：档位 1=12px/tick、默认 5=60px/tick、10=120px/tick；拖动滑块期间滚动持续不中断；
  切到 pager/flip 或 maximum<=0 时滑块禁用，加载完成后恢复。

## 风险

- 自动滚动与 comic 懒加载的竞态：持续 setValue 逼近最大 → `_on_scroll_lazy` 触发渲染 → `maximum` 增长 → 继续滚动，链条已存在（手动滚动同路径），预期稳定（合成离屏测试中该竞态经 stub 隔离验证 tick 逻辑）。
- **遗留**：`pager`/`flip`（翻页/横向）模式暂不支持自动翻页——用户原话「自动翻页」若后续想要翻页模式自动翻页，可在 `_pager_turn` / flip 导航上做定时自动翻页，本期保持滚动模式自动滚动。