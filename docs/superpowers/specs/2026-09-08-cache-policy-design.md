# 缓存策略设计（2026-09-08）

## 用户决策

发现页、搜索页 **完全不走缓存**；**只有书架界面** 走缓存（读取进度 + 封面）。

- 封面缓存：`cover:{source_id}:{url}`（`gui/components/cover_loader.py`，永久，shelf 池）——**保留**
- 书架读取进度：GUI 端 `position_changed` 信号落盘（reader 内）——**保留，未动**

## 诊断结论

「换源卡」根因 **不是** 缓存命中后仍并发联网——`list_works_cached` / `list_categories_cached`
命中缓存时均为**同步直接返回**，无后台刷新线程。

真正问题：每次切换源，新源的缓存键（含 `source_id`）**必然 miss**，浏览器侧 `list_categories_cached`
联网 → 分类返回后 `_reset_works` 并发 3 个 `_FetchWorksTask`（首页 + 2 预加载）全部 miss →
共 4 路网络请求同时发出。首次访问某源时缓存完全不生效，等于**每次换源都全量联网**，
与「加载缓存的同时发网络请求」的用户直觉在宏观效果上一致，但机制上是因为缓存永远 miss
导致每次都走全量网络，而非命中后重复发请求。

## 改动

- `gui/pages/discover_page.py`：`_FetchWorksTask.run` 直接调 `list_works`；`_FetchCategoriesTask.run`
  直接调 `list_categories`（发现页不再走缓存）。
- `framework/discovery.py`：删除 `list_works_cached`、`list_categories_cached` 与 `cache` 构造参数。
- `framework/search.py`：删除 `search_one_cached` 与 `cache` 构造参数；`search_type` 改调 `search_one`。
  搜索页本就走实时 `search_one`，无缓存键。
- `tests/test_cache_service.py`：删除 4 个针对已移除缓存方法的测试，新增 1 个
  `test_discovery_list_and_categories_never_cache` 验证发现列表/分类不读缓存。

## 验证

`python -m pytest tests/ -q` → **113 passed, 0 failed**。
