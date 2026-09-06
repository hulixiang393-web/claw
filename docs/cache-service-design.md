# Redis 风格缓存机制设计（cache-service）

> 日期：2026-09-06
> 状态：已验证设计（brainstorming 逐节确认）

## 一、背景与目标

用户诉求：
1. **书架缓存（≤3G）**：缓存书架书籍封面 + 「当前阅读书籍 → 当前章节 + 后面三章」预加载（不满三章按实际，只有一章就只缓存一章）。
2. **发现/搜索缓存（≤10G）**：显著提升整体加载速度。
3. **搜索页数放开**：取消 `constraints.search.max_pages` 作为搜索限制——有多少加载多少，翻到站点页尾由「连续空页」自然停；**不设默认页数限量**（仅保留引擎级硬保护 `_SEARCH_MAX_PAGES_HARD_CAP=2000` 防分页模板失效死循环，绝非搜索限制）。GUI 端动态分批渲染（`_append_displayed_batch`/`_render_all_remaining` 逐批、`_pump_visible_covers` 视口懒加载封面），保证有多少显示多少、但不一次全加载、首屏不卡顿。发现页已按页抓取 + 滚动 80% 增量加载，无需改。
4. **手动清除缓存**。
5. **使用 Redis 编程范式**（键值 + TTL + LRU + 配额）。

用户已确认的关键决策：
- **Windows 无官方 Redis** → Python 嵌入式（自研 Redis 风格服务，内存 dict + 定时/退出落盘 pickle-gz）。
- **磁盘持久化**：重启后缓存仍命中。
- **视频不做额外缓存**：维持现有内存态 `_video_html_cache`/`_ytdlp_stream_cache`，不动 VLC 播放链路。
- **触发时机**：进入阅读器时后台线程预加载当前章+后三章；书架列表时后台预加载所有封面。
- **搜索放开到全部页**：搜到站底自然停（空页提前停止），无限滚动。
- **TTL 分档**：封面/详情页永久（LRU 自动淘汰）；搜索/发现 24h；章节正文 7 天。
- **全路径注入**：封面 get_bytes、页面 get_text、章节正文、搜索、发现 五条路径。

## 二、架构

新增 `framework/cache_service.py`（纯 Python，零第三方依赖）：

```
            cache_service.CacheService  (base 类，Redis 风格 API)
              ├── RedisLikeStore          # dict + LRU + TTL + 字节配额
              │      ├── get(key) → val | None
              │      ├── set(key, val, ttl=None)
              │      ├── hset/hget/hgetall  (搜索/发现在用)
              │      ├── ttl/expire/delete
              │      ├── scan(pattern)
              │      ├── _touch_lru / _evict_until_under_quota()   # LRU 驱逐
              │      └── _save()/_load()   # pickle-gz 单文件，写入计数 + 定时 + 退出
              │
              ├── ShelfCache   (Quota=3G)    # cover:{source}:{url} / page:... / body:...
              └── SearchCache  (Quota=10G)   # search:{source}:{query} / list:{source}:{url}
```

> 2026-09-06 配额调整：书架缓存 **3G**，发现/搜索缓存 **10G**（搜索放开大量结果缓存是重点）。

两个池实例由 `cache_service.get_shelf_cache()` / `get_search_cache()` 单例提供。

### 键命名

| 数据 | 键 | 值 | TTL | 池 |
|---|---|---|---|---|
| 封面字节 | `cover:{source_id}:{abs_url}` | bytes | 永久 | shelf 3G |
| 页面文本（详情/目录/章页/发现页） | `page:{source_id}:{abs_url}` | str | 永久 | shelf 3G |
| 章节正文 | `body:{source_id}:{abs_url}` | str | 7d | shelf 3G |
| 漫画页图 URL 列表 | `pages:{source_id}:{abs_url}` | json list | 7d | shelf 3G |
| 搜索结果 | `search:{source_id}:{norm_query}` | json list | 24h | search 10G |
| 发现列表 | `list:{source_id}:{abs_url}` | json list | 24h | search 10G |

TTL 语义：永久=不设过期，靠 LRU 配额淘汰；有限 TTL 到期后 get 返回 None、惰性删除。

### 配额与驱逐

- 每实例维护 `_bytes_used` + 双向链表/OrderedDict LRU（插入序=访问序，命中 move_to_end）。
- `set` 后若 `_bytes_used > quota` → 从最旧（队首）逐出直到低于 90% 阈值（防抖）。
- `_bytes_used` = 键长 + 值长 + 固定开销估算（每 key 记 ~200B dict 开销近似）。
- 落盘：`cache/redis_shelf.gz` / `cache/redis_search.gz`（pickle-gz）；写密集时节流（≥500 写或 ≥60s 才存）；进程退出 atexit 落盘。

## 三、注入点

### 3.1 HttpClient 缓存（framework/http.py）

`get_text()` / `get_bytes()`：在发起网络请求前查询缓存，命中直接返回。
- 需区分「可缓存 GET」与「不可缓存 POST/动态」。只对无 query 突变、GET、且带 `Cache-Control` 可控的请求生效。
- **关键限制**：不是所有 get_text 都适合缓存——某些站 URL 带时效签名（eugenemakedraw CDN 等）。因此**只用源配置显式开启**：`transports.cache = true` 且 TTL 分档。默认关，逐源开启（hciyuan 等慢站开）。
- 实现：`HttpClient.get_text` 内插桩，若 `self._nullable_cache` 注入则查询命中。**不改 HttpClient 构造签名**，缓存实例由 `SettingsReader`/引导注入（见 §3.4）。
- **与 §3.2 content 层显式缓存的关系**：HttpClient 层是「安全 HTTP 响应缓存」（仅显式开启的源），content 层是「业务数据缓存」（详情/章节/封面，keyword 恒可缓存）。两者可共存：content 层命中直接返回，未命中时底层 HttpClient 若该源开启缓存也可能命中——双层防重复。

> 注：为避免多层重复 TTL 语义混乱，P0 阶段 HttpClient 层缓存**只覆盖显式开启的源**（hciyuan 等慢站），content 层显式缓存覆盖全部源。二者独立不冲突。

#### 3.2 Content 章节/详情/封面（framework/content.py）

- `fetch_detail()`：`page:` 键（TTL 永久）命中免整页下载——**已有 `_detail_html_cache` 内存层，Redis 作为其二级持久层**（内存 TTL 5min 优先，miss 再查 Redis）。
- `fetch_chapter()`（novel）/ `fetch_comic_pages()`（comic）：命中 `body:`/`pages:` 二级缓存，miss 则抓取后写入。
- `_fetch_cover()`：命中 `cover:` 二级缓存。
- 预加载入口：新增 `Content.precache_chapters(source, url, current_idx, ahead=3)`——进入阅读器时后台线程调用，抓 current_idx 及后 3 章正文写入 Redis。不满三章按实际；只有一章就只缓存一章。

### 3.3 Search/Discovery 缓存 + 页数放开（framework/search.py、discovery.py）

- `search.py _search_html`：结果合并后写 `search:{source}:{query}`（24h TTL）；下次同 query 命中直接回。
- `discovery.py`：发现列表 `list:` 缓存。
- **页数放开**：`constraints.search.max_pages/max_results` 改为**不限制**（或设极大软上限如 99999，避免 int 边界）。`_search_html` 已有「连续空页提前停发下一波」逻辑（search.py:268-290），放开后自然搜到站底。搜索结果合并去重不变。
- GUI `search_page.py`：现有 `_render_all_remaining`（分批 QTimer 渲染）+ `_pump_visible_covers`（视口懒加载封面）已满足「不限页数 + 10s 首批 + 滚动不卡」。验证：现有 6 并发 + 分批已有基线。需测放开后大批量（如 500+ 条）仍流畅。

### 3.4 引导注入

默认提供 `framework/cache_loader.py`（或复用 settings_manager）：应用启动时构造 ShelfCache/SearchCache 并注入 HttpClient + Content。**注入失败/禁用时静默降级为不缓存**（保证老路径可用）。

## 四、手动清除 + 配额管理

设置页（`gui/settings_page.py`）新增「缓存管理」区：
- 显示当前用量：书架缓存（3G）、搜索&发现缓存（10G）各显示已用 GB。
- **独立清除按钮（分开管理）**：
  - 「清除书架缓存」→ `get_shelf_cache().clear()`（清封面 + 详情 + 章节 + 漫画页图）。
  - 「清除搜索&发现缓存」→ `get_search_cache().clear()`（搜索 + 发现同池 10G）。
- 每个清除按钮清完立即 `flush_checked()` 落盘（持久化清除状态，重启后不复活）。

数据目录：`data/cache/redis_{shelf,search}.gz`（随 data/ 走 settings）。

## 五、错误处理与降级

- 落盘/读盘失败：catch OSError/UnpicklingError → 空缓存继续运行（不崩溃）。
- 配额配额超标写失败：LRU 仍可驱逐，极端超限（单 key 大于配额）跳过缓存。
- Redis 实例懒加载：首次访问才构造，构造失败静默禁用（`get_shelf_cache()` 返回 None → 调用方跳过查询）。
- 线程安全：所有 store 方法加 `threading.Lock`（Content/搜索多线程并发）。

## 六、测试

- 单元（`tests/test_cache_service.py`）：
  - set/get/ttl 过期/hset/hgetall/delete/scan
  - LRU 驱逐顺序（配额小池验证）+ 超配额跳过
  - 落盘 → 重建 → 恢复数据（不同 TTL 保真）
  - 注入降级（缓存 None 时主路径不崩）
- 集成：hciyuan 详情页二次打开命中（page:）；同词重搜命中（search:）
- 回归：`pytest tests` 全过；hciyuan 第一集修复不破坏
- 性能：搜索放开后 500+ 条结果 10s 首批 + 滚动不卡（GUI 冒烟或手动）

## 七、范围与分期

- **P0（缓存核心 + 注入）**：cache_service.py + HttpClient/Content 注入 + 搜索/发现缓存 + 手动清除
- **P1（预加载）**：阅读器进入时当前章+后三章后台预加载 + 书架封面预加载
- **P2（搜索放开）**：max_pages/max_results 放开 + GUI 大批量懒加载验证

每次提交前跑 `pytest tests` 全过。