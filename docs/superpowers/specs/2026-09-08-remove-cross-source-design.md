# 移除「跨源换源」功能 设计文档

日期：2026-09-08
状态：已审批（用户：按方案彻底删）

## 背景

用户要求移除阅读器中的「跨源换源」功能。经调研确认「换源」在代码中有两个独立概念：

1. **跨源换源**（cross-source switch）——本次移除对象：当前源失效时，用作品标题去**其他同类型源站**搜索同一作品，弹对话框选择候选后切换继续阅读。全类型（小说/漫画/视频）均可。
2. **站内多线路换源**（source_switch）——保留：MacCMS 等"同剧多源"（`?sid=N`）站内线路切换，经播放器 ⇄ 菜单/顶部下拉框/阅读器 ⇄ 菜单操作。

保留项还包括：搜索页的「跨源并发搜索」（`search.py` 多源并发）、`content.py:840` 的详情目录 `clean_title`。以上均与「跨源换源」无关，不得误删。

## 删除范围

### 1. 整个文件删除

| 文件 | 说明 |
|---|---|
| `framework/cross_source.py` | 核心逻辑：`clean_title` / `similarity` / `CrossSourceCandidate` / `find_cross_source` / `switch_to_source` / `clamp_episode_no` |
| `gui/components/cross_source_dialog.py` | 候选选择对话框 `CrossSourceDialog` |
| `gui/_smoke_cross_dialog.py` | 对话框 smoke 测试 |
| `tests/test_cross_source.py` | 核心逻辑 pytest |

### 2. `framework/content.py`

- **不修改**。已验证 `content.py:840` 的局部 `clean_title` 是详情目录清洗函数，与跨源换源无关。
- `switch_source` / `_get_source_switch_cfg` / `_maybe_expand_video_series`（站内多线路）全部保留。

### 3. `gui/pages/reader_page.py`

删除：
- `_CrossSourceSignals` / `_CrossSourceTask`（78-122）
- `_CrossSourceSearchSignals` / `_CrossSourceSearchTask`（124-164）
- `__init__` 中的三视图 `cross_source_chosen` 信号连接（259-262）
- `_on_cross_source_triggered`（347-388）
- `_on_cross_source_candidates`（390-422）
- `_on_cross_source_search_failed`（424-431）
- `_set_cross_source_hint`（433-438）
- `_resolve_current_detail`（440-446，仅被跨源换源使用）
- `_current_reader_index`（448-457，仅被跨源换源使用）
- `_on_cross_source_switched`（459-492）
- `_on_cross_source_failed`（494-503）

保留：
- `_on_source_changed`（317 起，站内 VideoView 切源 → 重载分集）
- `_SwitchSource` 相关全部（站内多线路）

### 4. `gui/pages/reader/novel_view.py`

- 删信号 `cross_source_chosen`（38）
- 删工具条 `⇄ 换源` `source_btn`（85-89）
- 删 `load()` 里 `source_btn.setVisible(True)`（217）
- 删 `_emit_cross_source`（351-354）

### 5. `gui/pages/reader/comic_view.py`

- 删信号 `cross_source_chosen`（48）
- 删工具条 `⇄ 换源` `source_btn`（87-91）
- 删 `load()` 里 `source_btn.setVisible(True)`（200）
- 删 `_emit_cross_source`（225-228）

### 6. `gui/pages/reader/video_view.py`

- 删信号 `cross_source_chosen`（137）
- `_refresh_source_menu`（517-542）：删分隔线 + `⇄ 跨源换源` Action（534-538）及 `_source_actions.append(self.cs_action)`；**保留**站内多线路菜单项构建（525-533）
- `source_btn`（⇄ 按钮本身，386-396 区）是站内换源入口，**保留**
- `current_episode_no()`（833）**保留**（独立 getter，可在其他场景复用）

### 7. `gui/_smoke_reader.py`

删除：
- 跨源换源调度段（229-281）：`SRC_B` 源定义、`CrossSourceCandidate`、`_StubDialog`、`_on_cross_source_triggered` 触发与断言
- 三视图跨源入口断言段中跨源部分：`hasattr(un, "cross_source_chosen")`（288-290）、comic 换源按钮段（292-311 中的换源按钮部分）、novel 换源按钮断言（317-325 中换源部分）、视频 `⇄ 跨源换源` 菜单项断言（349-355）

保留：
- 目录长标题换行断言（comic `toc_list.wordWrap()`、novel、video `ep_list` 图标 76×50 + wordWrap）
- `current_episode_no()` 越界断言（343-347）——该 getter 保留
- epub 目录换行段

注意：删除后 `_smoke_reader.py` 中 `ComicView`/`NovelView`/`VideoView` 的 `load` 段仍需保留视图基础断言（内容加载、来源切换等与跨源无关部分）。

### 8. 设计文档与实施计划

- 删除 `docs/superpowers/specs/2026-09-06-cross-source-switch-design.md`（含部分 I 跨源换源 + 部分 II LLM Agent；两者均已移除）
- 删除 `docs/superpowers/plans/2026-09-06-cross-source-and-source-agent.md`（Track A 跨源换源 + Track B LLM Agent；两者均已移除）
- **不修改** `docs/core.md` / `docs/PROJECT.md` / `docs/ui-search.md` 中的「跨源」表述——这些指多源并发搜索（保留功能）
- **不修改** `docs/source-schema-v2.md` 的 `source_switch`——此指站内多线路（保留）

## 明确不动的部分

| 位置 | 说明 |
|---|---|
| `search.py` 跨源并发搜索 / `merge_similar` / `variants` | 搜索页功能，保留 |
| `framework/content.py` `switch_source`/`source_switch`/`clean_title`(840) | 站内线路 + 目录清洗，保留 |
| `video_view.py` `source_btn`/`_refresh_source_menu` 站内线路项/`_populate_source_combo`/`_on_source_switch` | 站内换源 UI，保留 |
| `source_editor.py`「换源（source_switch）」表单 | 站内线路配置，保留 |
| `sources/fdzys.json`/`trtag.json`/`ttdm.json` 的 `source_switch` 配置 | 站内线路数据，保留 |
| `reader_page.py` `_on_source_changed` 及站内换源调度 | 保留 |

## 验收标准

1. `framework/cross_source.py`、`gui/components/cross_source_dialog.py`、`gui/_smoke_cross_dialog.py`、`tests/test_cross_source.py` 已删除，且全仓库 grep 无 `cross_source` / `CrossSourceDialog` / `CrossSourceCandidate` 残留（`docs/analysis` 中的历史分析文档除外，见下）。
2. 三视图均无「⇄ 换源」/「⇄ 跨源换源」UI 元素，`source_btn`（novel/comic）与 `cs_action`（video）已不存在。
3. `reader_page.py` 无 `_on_cross_source_*` 方法、无 `_CrossSource*Task` 类。
4. 站内多线路换源全链路保留：`_smoke_reader` 中站内换源断言、`_smoke_regression` 换源回归保留且通过。
5. `pytest tests/` 全量通过；`gui/_smoke_reader.py`、`gui/_smoke_regression.py`、`gui/_smoke_app.py` 通过。
6. 新写源（91dongman/h18ani7/uacg）仍可被源管理器加载。

## 文档残留豁免

`docs/analysis/03-代码质量分析.md` 的「N8 reader_page.py:161 用 video_view.play_label 显示换源错误信息」是历史代码质量评审笔记，指向站内换源错误提示，与跨源换源无关，保留原样。

## 风险

- 删除 `_resolve_current_detail` / `_current_reader_index` 前需再确认无其他调用方（Grep 已确认仅跨源换源使用）。
- `_smoke_reader.py` 删除跨源段后，需保证 `reader` 实例构建路径仍覆盖到（保留其余断言）。
- 站内换源与跨源换源在 `video_view._refresh_source_menu` 交错，删除时只动 534-538 行，不误删 525-533 站内项。