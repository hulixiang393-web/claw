# 发现页换源会话缓存设计（2026-09-10）

## 1. 用户需求（原始表述）

「第一次进入到程序的时候当我再点击换源的时候那么上个源的东西就会全部进行清除，当下次再进行点击的时候就会重新进行网络的请求……如果我们只缓存到上次加载到的地方的话，就可以速度更快一点。缓存规则：最多只缓存最近 3 个源；超过 2GB 缓存的时候可以进行清理；只缓存到上次加载到的位置；下次重启应用的时候清那个缓存；换源下来浏览的东西、浏览的位置和页数也可以通过缓存来记录。关闭程序就清除缓存，但不能卡住关闭不了。」

## 2. 需求确认（已问答）

| 决策点 | 结论 |
|---|---|
| 「换源」范围 | **发现页顶部源下拉框**切换数据源（works 作品网格 + 分类 + 分页） |
| 缓存并恢复的内容 | **完整会话状态**：用户当前浏览的分类栏（分类列表 + 选中项）+ 该分类已加载作品网格 + 已加载页数 + 浏览滚动位置 |
| 分类选择 | 换源恢复时**还原上次浏览的分类**（含分类按钮列表与选中项），非仅默认「全部」 |
| 缓存生命周期 | 独立于既有持久缓存：**仅换源缓存**，本次运行有效，**重启即清** |
| 关闭行为 | **不阻塞关闭**：内存缓存无落盘，进程退出即自动释放，无需退出清理代码 |
| 恢复后行为 | **纯缓存，不后台刷新**：命中缓存直接还原会话（含分类栏、作品、滚动），不发请求；后续**按需请求**——用户点其它分类 / 滚动进入未缓存新区时才走网络（类似前端 session 懒合并） |
| 配额上限 | 2GB 内存配额（实际远达不到） |
| 源数量上限 | 5 个最近使用的源（LRU 逐出最旧） |

> **会话语义（类前端 session）**：切走源时把「该源此刻的完整界面状态」冻结保存；切回时无损还原，网络请求完全不发。缓存页是「已浏览到的地方」，滚动越过缓存末尾且 `has_more` 时，`_load_next_page` 照常按需追加缓存并继续正常浏览——前后无缝衔接。

## 3. 背景事实（已核实）

- 换源路径：`DiscoverPage._on_source_changed`（`gui/pages/discover_page.py:275`）→ `_clear_works` + `_clear_cat_buttons` + `_load_categories`；分类就绪后 `_reset_works`（:437）→ 清空 + 逐页 `_load_next_page`（:529）并发抓取页 1..3。
- 已加载数据：`self._works`（list[Work]）、`self._loaded_pages`（set[int]）、`self._current_page`、`self._has_more`、`self._current_cat_url`。
- 滚动位置：`self.scroll.verticalScrollBar()`（discover_page.py:185 接 `_on_scroll` :913）。
- 缓存设施现成：`RedisLikeStore`（`framework/cache_service.py:44`）dict+LRU+TTL+字节配额+可选落盘；`get_shelf_cache`（已落盘 redis_shelf.gz）、`get_search_cache`（10GB）为现有单例。
- 已有决策（`2026-09-08-cache-policy-design.md`）：「发现页、搜索页完全不走缓存，只有书架走缓存」。**本次为会话级内存缓存（重启即清），与该持久缓存决策不冲突**；实现上**不碰** `list_works`/`list_categories`/Content._cache，仅在 GUI 层新增独立的会话快照缓存。
- 退出钩子：`QApplication.aboutToQuit`（`gui/app.py:378`）现有 VLC 释放等；本功能**无需新增退出清理**（不落盘）。

## 4. 架构

```
framework/cache_service.py                 # 改动：新增会话级缓存单例
  _session_singleton = RedisLikeStore(quota=2GB, persist_path=None)   # 不落盘=重启即清
  get_session_cache(data_dir=None) -> RedisLikeStore|None

gui/pages/discover_page.py                 # 改动：换源会话快照，全部逻辑在 GUI 层
  __init__ 增加参数 session_cache=None     # App 层注入 get_session_cache()，None=禁用
  _SOURCE_CACHE_MAX = 5                    # 最多 5 个源
  _snap_lock  / _source_order              # 源顺序（LRU 控制）+ 写入锁
  _save_source_snapshot()                  # 换源离开/每页加载完成时快照
  _try_restore_source_snapshot()           # 换回该源时命中→直接渲染，不发请求
  _record_scroll_ratio()                   # _on_scroll 节流记录滚动比例

gui/app.py                                 # 改动：注入 session_cache 到 DiscoverPage
```

数据存储（`RedisLikeStore` 键值，key = `disc:{source_id}`）：

```python
snapshot = {
    "cats": [(title, url), ...],   # 该源分类列表（重建分类栏用；url 为 None 表示「全部」）
    "cat_url": str,                # 当前选中分类 URL（None = 全部 = works_list_url）
    "works": list[Work],           # 多页合并去重后全量（直接存 dataclass 对象）
    "pages": [1,2,3...],           # 已加载页集合（有序）
    "has_more": bool,
    "current_page": int,           # 已加载最大页
    "scroll": float,               # 0~1 滚动比例 = value / max（max>0）
    "ts": float,                   # 最近写入时间
}
```

- 分类快照：切走/每页加载完成时把 `self._cat_buttons` 的 `(title, cat.url or None)` 与 `_current_cat_url` 一起写入，恢复时**就地重建分类按钮**（复用 `_make_cat_button`/_refresh_cat_buttons 的按钮对象 `cat` 判空逻辑，discover_page.py:364）。

- 序列化：`RedisLikeStore` 内部用 `pickle`（cache_service.py:36 `_value_bytes`），`Work` 是 dataclass（discovery.py:34）可直接 pickle。**保存时存 Work 对象本身，恢复时直接复用**——不做 `as_dict()`/`Work(**d)` 往返（`as_dict` 输出键 `update_info` 与构造字段 `update` 不对称，`Work(**d)` 会抛 TypeError，已实测验证）。
- LRU 源控制：`_source_order`（list[str]）记录源使用顺序；快照写入时 `move_to_end`，超 5 个 `pop(0)` 并 `del` 其缓存键。
- 配额：`RedisLikeStore(quota=2GB)` 自带 `_evict_until_under_quota`（cache_service.py:191）兜底防内存异常。

## 5. 数据流

### 5.1 写入（快照更新时机）

1. `_on_page_loaded` 成功追加一页后（discover_page.py:592 附近）：把最新 `_works/_loaded_pages/_has_more/_current_page/_current_cat_url` 快照写入（覆盖式，全量写入，简单可靠）。
2. `_on_scroll` 节流（~500ms，`_record_scroll_ratio`）更新 `scroll` 字段（仅更新该键的一条）。
3. `_on_source_changed` 切换前（:275 开头）：先对**旧源**做最后快照（含滚动比例），再清空。

> 说明：全量覆盖写而非增量合并——数据量小（5 源 × 每源几十~几百条）且序列化开销可忽略，避免同步协同的复杂度。

- 写锁：`_snap_lock = threading.RLock()` 防止主线程 GUI 与后台 `_on_page_loaded`（信号跨线程）并发写同一键。`RedisLikeStore.set` 本身持 `RLock`（cache_service.py:55,106），但读-改-写快照对象是应用层操作，需页内锁保护。
- 失败降级：`get_session_cache()` 返回 None（初始化失败）→ 整个功能禁用，行为与现状完全一致。

### 5.2 读取（恢复时机）

`_on_source_changed`（切到新源）与 `_reset_works` 流程中：

1. 换到某源 `sid` 时查 `disc:{sid}`：
   - **未命中** → 正常 `_load_categories` → `_reset_works` → 网络加载（现状行为）。
   - **命中** → 进入恢复分支，完成后 `**0 个网络请求**`：
     - **重建分类栏**：用快照 `cats` 就地重建分类按钮（不调用网络 `_load_categories`），并把 `cat_url` 对应按钮设为选中态。
     - `_reset_works` 前置检查快照：命中则**不调 `_load_next_page`**，改为把快照 works 经 `_append_works` 式重建卡片 + 设回 `_loaded_pages/_current_page/_has_more/_seen_urls`。
     - **恢复滚动**：`QTimer.singleShot(0, ...)` 等布局稳定后 `vbar.setValue(int(scroll * vbar.maximum()))`（防内容高度未就绪时 setValue 失效）。
   - 命中后用户**再操作**（点其它分类 / 滚动过缓存末尾且 `has_more`）→ 走正常 `_reset_works`/`_load_next_page` 网络加载，缓存随之更新，会话无缝衔接。

> 恢复分支在 GUI 主线程同步重建卡片（现有 `_on_page_loaded` 也是主线程渲染），封面走既有 `WorkCard`/CoverLoader 异步路径。

## 6. 关键实现细节

- `_reset_works`（:437）是所有入口（切源/切分类）共用。新增恢复逻辑放 `_on_source_changed`/`_reset_works` 入口处**只在切源场景**检查（用 `_current_cat_url == 默认分类URL` 判定是「全部」视图）：
  ```python
  def _reset_works(self):
      self._clear_works()
      ... 现有重置 ...
      if self._pending_restore:      # 切源恢复标记（仅在换源+命中时置位）
          self._render_restored(self._pending_restore)
          self._pending_restore = None
          return
      for p in range(1, 1 + self._preload_ahead + 1):
          self._load_next_page(page=p)
  ```
- `_on_source_changed` 开头（切走旧源）：
  ```python
  if self._current_source is not None:
      self._save_source_snapshot()   # 含滚动比例
  ```
  切到新源后：
  ```python
  restored = self._try_restore_source_snapshot()
  if not restored:
      self._load_categories()        # 原逻辑
  ```
- 滚动比例记录：`_on_scroll`（:913）内嵌节流（首记录后 `QTimer.singleShot(500, 落盘)`），只更新当前源快照的 `scroll`。
- 分类栏恢复：命中时用快照 `cats` 重建 `_cat_buttons`（`_make_cat_button(text, cat or None, ...)`），`cat_url` 对应按钮 `setChecked(True)`、其余 `setChecked(False)`；`cat_toggle_btn` 文字按 COLLAPSED_CATEGORY_COUNT 规则刷新（复用 `_refresh_cat_buttons` 现有折叠逻辑）。
- `clear_cache` 幂等：设置页「清除搜索&发现缓存」按钮（settings_page.py:265）当前清 `_search_singleton`；本次**不改动**该按钮范围（避免影响并行任务 test）。会话缓存随进程消亡，无需 UI 清理入口。

## 7. 测试计划

- `tests/test_cache_service.py`（扩展）：
  - `get_session_cache` 返回 RedisLikeStore 且 `quota==2GB`、`persist_path is None`（不落盘）。
- `tests/test_discover_switch_cache.py`（新增，纯逻辑不依赖 GUI）：
  - 快照序列化往返：list[Work] 经 RedisLikeStore 写读后字段完整（pickle 往返）。
  - 源 LRU 逐出：写入 6 个源 → 最旧者被逐出，仅剩 5。
  - 会话缓存 `set/get/expire` 行为（用独立 RedisLikeStore 实例）。
  - `_try_restore_source_snapshot` 的命中/未命中分支（抽成静态辅助函数测试），恢复时含分类重建。
- GUI 冒烟：`gui/_smoke_discover.py` 扩展——离屏创建 DiscoverPage，注入假 session_cache：
  - 换源 A→B→A：第二次到 A 命中缓存，`_works` 与页集合立即恢复，无 `_FetchWorksTask` 新增。
  - 缓存未命中路径行为不变（正常发起抓取）。
- 回归：`pytest tests/`（当前 202 全绿基线）+ 既有 smoke（`_smoke_regression`/`_smoke_reader`/`_smoke_library`/`_smoke_discover`）。

## 8. 范围裁剪（YAGNI）

- 不做持久化换源缓存（重启即清，是需求明确的会话级）。
- 不做后台刷新/过期校验（用户明确「纯缓存，不刷新」，需要新数据时按需请求）。
- 不缓存封面图片字节（封面走既有 CoverLoader/WorkCard 异步，缓存的是 Work 元数据）。
- 不做多源会话之间的数据合并/比对（换源=切换源，不同源数据各自独立快照）。
- 不在设置页加清理入口（会话缓存随进程消亡）。
- 不改 `list_works`/`list_categories`/`Content._cache`（不触碰 2026-09-08 cache-policy 决策）。

## 9. 验收标准

1. 同一会话内：发现页从源 A（浏览某分类 C、加载到第 4 页、滚动在 60%）换到源 B 再换回 A：A 的**分类栏（选中 C）**、作品网格、已加载页数、浏览滚动位置**立即还原**，且不产生任何 `_FetchWorksTask`/`list_categories` 网络请求。
2. 恢复后再滚动越过缓存末尾且 `has_more` → 按需网络加载下一页，缓存随之更新，浏览连续。
3. 首次进入程序换源（缓存空）行为与现状一致（正常网络加载）。
4. 换源超过 5 个源时，最旧的源快照被逐出（内存不无限增长）。
5. 重启应用后换源缓存为空（内存缓存，退出即释放，关闭不卡顿、无退出清理代码）。
6. 找不到 session_cache（初始化失败）时功能禁用，全部行为回退现状。
7. `pytest tests/` 202 全绿 + 新增测试通过 + 四个 smoke 过。