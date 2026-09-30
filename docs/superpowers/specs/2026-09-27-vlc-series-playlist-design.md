# 外部播放器全集播放列表 + 惰性解析设计（vlc-series-playlist）

日期：2026-09-27
状态：用户已批准（5 项决策全部确认，见 §2）
相关 commit：`8b8f402`（多集播放列表初版，**本设计修正其契约**）、`3dba2dd`（按源缓冲/缓存预算）

## 1. 背景与问题

用户诉求：**在外部播放器（VLC）里直接点击下一集进入下一集，并按顺序排布。**

初版多集播放列表（`8b8f402`）已把多集 MRL 拼进 VLC 命令行，VLC 进程也未隐藏（只加 `CREATE_NO_WINDOW`），`Ctrl+L` 播放列表面板可用。但存在 5 个硬伤，功能实际等于没生效：

| # | 缺口 | 位置 | 后果 |
|---|---|---|---|
| A | 列表项填的是**分集页面 URL** 而非流地址（`video` 解包后被丢弃） | `video_view.py:1503-1504` | 解析型源（hanime1/bilibili 等 `play_url.selector`）在 VLC 里点开是 HTML 页面，播不出来。只有 `ep.url == m3u8` 的直链源侥幸能用 |
| B | 顺序是「当前集 + 原始 0..n 升序」 | `video_view.py:1497-1509` | 从第 5 集开播，按 N 会跳到**第 1 集** |
| C | 列表只可能含 1~3 集 | 只收 `_stream_cache` 命中集；预拉 ±1 明确「本次不进列表」 | 想跳第 10 集必须先在 App 里逐集点过（每次都 terminate 重开 VLC） |
| D | **完全没有剧集标题**（三元组 title 被 `_ep_title` 丢弃） | `external_player.py:202` | 列表里全是 `http://127.0.0.1:PORT/s/<32位hex>`，用户认不出哪条是哪一集 |
| E | `pl_prev` 命令名不存在 | `video_view.py:772` | App 内 `P` 键（上一集）**从未生效过** |

附带风险：代理 `_IDLE_TIMEOUT=600s` 空闲回收会 `stop()` 并 `_tokens.clear()` **整批清空**，暂停超 10 分钟后当前集与后续所有集一起 404。

## 2. 已确认决策

1. **列表范围**：全集所有集，**惰性解析** —— 所有集的 MRL 一次性交给 VLC（带标题、顺序正确），每集流地址在**被真正播放到时**才解析。零预请求。
2. **排列语义**：全集按第 1 集 → 最后一集顺序排列，当前集选中并从这里开始播。
3. **令牌生命周期**：**只要 VLC 还开着就永不失效**（租约制）。
4. **App 内切集**：改为指挥 VLC 切集（`pl_play`），**不重开** VLC。
5. **启动定位**：方案 A —— MRL 严格按 1..N 入列 + `--no-playlist-autostart` + 启动后 `pl_play&id=<当前集>`。

## 3. 已核实的外部事实（VLC 官方文档）

来源：`wiki.videolan.org/VLC_command-line_help`、`share/lua/http/requests/README.txt`、`share/lua/intf/modules/httprequests.lua`（VLC 3.0.x）。本机 VLC **3.0.23**（`C:\Program Files\VideoLAN\VLC\vlc.exe`）。

| 事实 | 原文/依据 | 设计影响 |
|---|---|---|
| 命令行多个流会入播放列表，**第一个先播** | `You can specify multiple streams on the commandline. They will be enqueued in the playlist. The first item specified will be played first.` | 需 `--no-playlist-autostart` 才能从非首项开播 |
| MRL 语法 `URL#[title][:chapter]`；`$N` = "name (media name as seen in the VLC playlist)" | Stream MRL syntax | 追加 `#<剧集标题>` 即可让列表显示标题 |
| fragment 若匹配 `mrl-section = mrl-title[:mrl-chapter]...`（`mrl-title = DIGIT*DIGIT`）则解释为**时间偏移** | `group__mrl.html` Fragment Query | 标题**必须以非数字开头**，否则被当成跳转秒数 |
| `?command=pl_play&id=<id>` —— 参数是 **`id`**，且 `id` 是**播放列表项 id**（非下标、非 `val`） | README：`play playlist item <id>` | 必须先读 `playlist.json` 拿 id 映射 |
| `?command=pl_previous` —— **没有 `pl_prev`** | README + lua 源码 | 修 §1 缺口 E |
| `?command=pl_next` 正确 | 同上 | 沿用 |
| `playlist.xml` / `playlist.json` 返回完整播放列表树 | README | 用于建立 `{集下标: vlc_id}` 映射 + 校验标题是否生效 |
| `--random` / `--no-random`、`--loop`、`--repeat`、`--playlist-autostart` 均为合法 CLI 选项 | wiki Documentation:Advanced_Use_of_VLC | 顺序用 `--no-random` 兜底；循环不干预 |

另核实（仓库内）：

- `media_proxy` 用 `ThreadingHTTPServer`（`framework/media_proxy.py:36,706`）→ 解析请求在处理线程内阻塞调用取流**安全**。
- `framework/content.py` **零 Qt 依赖**（`:20-39` 只有 stdlib + 同层 framework 模块）→ `fetch_video_streams` 可在代理线程安全调用。
- `_FetchStreamTask.run()`（`video_view.py:124-126`）的调用为 `fetch_video_streams(source, ep_url, quality=..., merged=True)` —— 惰性 resolver **原样复用这一行**，不新增取流逻辑。

## 4. 架构

```
video_view._play()
  ├─ media_proxy.register_series(resolver, on_play, count=N) -> key
  ├─ 构造 episodes = [(lazy_url(0), "", "第01集 xxx"), ..., (lazy_url(N-1), "", "第NN集 xxx")]
  ├─ url = lazy_url(cur)            # 仅用于定位起始项 + 分类
  └─ external_player.open_with_player(url, episodes=episodes, classify_url=真实流地址, ...)

VLC 启动参数：
  <全局选项> --no-random [--no-playlist-autostart]
  http://127.0.0.1:Q/e/<key>/0#第01集 xxx
  http://127.0.0.1:Q/e/<key>/1#第02集 xxx
  ...（严格 1..N）
        │
        └── 启动后：轮询 playlist.json → 按 uri 匹配 → pl_play&id=<cur 对应 id>
                    └─ 成功：从当前集开始播
                       失败/超时：降级为 autostart 播第 1 集（记日志，不致命）

VLC 播放到第 k 集 → GET /e/<key>/<k>
  ├─ 1. on_play(k)  → Qt 信号 → 主线程更新 _current_idx / _stream_cache / UI
  ├─ 2. resolver(k) → fetch_video_streams(...)  【按集 memo，重复请求不再回源】
  └─ 3. 302 Location: <build_url 包装后的 token URL>   → 广告过滤/UA/Referer/磁盘缓存全部照旧
```

### 4.1 media_proxy：系列注册表 + 惰性端点

新增路由 `GET /e/<key>/<idx>`，与既有 `/s/<token>`、`/c/<key>/<seg>` 并列。

```python
def register_series(self, resolver, on_play=None, count=0) -> str:
    """注册一个惰性解析系列，返回不透明 key（uuid4().hex[:12]）。
    resolver(idx) -> (video, audio, headers, ad_block)；抛异常表示该集取流失败。
    on_play(idx, video, audio) 在每次 /e/ 请求时回调（用于回填 App 状态）。
    count 为集数上限，越界返回 404。"""

def unregister_series(self, key: str) -> None: ...
def series_episode_url(self, key: str, idx: int) -> str:
    """返回绝对 URL http://127.0.0.1:<port>/e/<key>/<idx>（不含 #title）。"""
```

`/e/<key>/<idx>` 处理顺序：

1. `_series` 查表（受既有 `self._lock` 保护）→ 缺失 404。
2. `idx` 必须是 `int` 且 `0 <= idx < count` → 否则 404（拒绝一切非数字，防路径穿越）。
3. 调 `on_play(idx, video, audio)`（若已 memo 则用 memo 值）—— 让 App 知道「正在播第几集」。
4. 命中 memo → 直接 302（**重复请求不再回源**，覆盖 VLC 重试/用户重播同一项）。
5. 未命中 → 先在**锁外**过 `threading.Semaphore(4)`（`acquire(timeout=20)`，超时 503）→ 调 `resolver(idx)`：
   - 成功：写 memo → `build_url(video, headers, ad_block=...)` → `302 Location` + `Cache-Control: no-store`
   - 抛异常：**不写 memo**（允许重试）→ `502`
6. memo 结构 `{(idx): (video, audio, headers, ad_block)}`，随 `unregister_series` 一起丢弃。

`ad_block` 与 `force_proxy` 由 `video_view` 在注册时通过 resolver 闭包捕获，行为与现有单集路径完全一致。

### 4.2 media_proxy：租约制令牌生命周期

```python
def acquire_lease(self) -> str: ...      # 返回 lease id
def release_lease(self, lease_id: str) -> None: ...
```

- `self._leases: set[str]`。
- 空闲看门狗 `_start_idle_watch()` 的回收条件追加 `and not self._leases` —— **有租约时完全跳过回收**。
- `stop()` 语义不变（atexit 强制停），租约只挡看门狗。
- `external_player` 在 `Popen` 成功后 `acquire_lease()`，并起一个 daemon 线程 `proc.wait()` 阻塞等待；VLC 一退出立刻 `release_lease()`。
- 释放点：VLC 进程退出 / `_terminate_previous()` / App `atexit`。
- 开销：token 是几百字节字典项，惰性解析下每集最多 1 个，总量可忽略。

### 4.3 external_player：契约修正 + 标题 + 启动定位

**契约变更（有意破坏，向后不兼容）**：`episodes` 语义由「当前集 + 其余已缓存集」改为「**全集完整有序列表**」。

- `episodes[i]` = 系列第 `i` 集（0-based，严格播放顺序）。
- 主 MRL **不再跳过 `episodes[0]`**：整个 `episodes` 按序全部入列。
- `url` 参数仅用于**定位起始项**与 `episodes is None` 时的单集回退。
- **起始项下标判定**：`start_idx = next((i for i, e in enumerate(episodes) if e[0] == url), 0)`。取不到（`url` 不在列表内）时按 `0` 处理，等价于走 autostart。
- `video_view` 是唯一调用方（`app.py:828` 传本地文件、不传 episodes，不受影响）。

新增参数：

| 参数 | 用途 |
|---|---|
| `classify_url: str = ""` | **非空时用它做 `classify()`**。惰性 URL 是 `http://127.0.0.1:PORT/e/...`，分类不出 HLS；必须传**当前集真实流地址**，否则 ikanpp 的 30000ms 缓冲会失效。空串时行为与现在完全一致。 |

参数组装（在现有 `args = [...]` 上扩展）：

1. 全局选项：现有 7 项（`--no-video-title-show`、`--no-drop-late-frames`、`--network-caching`、`--extraintf=http`、`--http-host`、`--http-port`、`--http-password`）**全部保留**。
2. `episodes` 非空时追加 **`--no-random`**（需求是按顺序；用户 vlcrc 若存 `random=1` 会乱序）。
3. `episodes` 非空且**起始项不是 `episodes[0]`** 时追加 **`--no-playlist-autostart`**。
4. 按序追加每集 MRL = `<lazy 或代理 URL>#<标题>`。
5. **系列路径不追加 `:input-slave=`**（见 §7 非目标）。
6. `Popen` 成功后：若需要定位起始项 → 轮询 `playlist.json`（间隔 100ms，上限 3s）→ 按 `uri` 匹配起始项 → `pl_play&id=<id>`。轮询超时/解析失败 → 记日志，降级为 autostart 播第 1 集。
- `open_with_player` 在 `episodes` 非空时**主动读一次** `playlist.json`（无论握手是否成功），把结果存入模块级 `_playlist_items` 供 App 侧查询；握手失败时这份列表仍可用于建立 id 映射（只是起始定位失败）。

**标题消毒**（`_sanitize_title`）：

- 剥掉 `#`、换行、`\r`、`\t`（会破坏 MRL 解析）。
- 折叠连续空白、strip。
- 结果为空 → 用 `第{idx+1}集`。
- **必须以非数字开头**：调用方保证 `第NN集 <章节名>` 形式；`_sanitize_title` 再做一次防御——若首字符是数字，前缀 `集`。
- 不做 URL 编码（`Popen` 用 argv 数组不经 shell；`#` 之后的 fragment 不会发到服务器）。

    - 第 7 步的列表读取失败时不缓存空结果（下次调用重试）

**新增/修正的控制接口**：

```python
def player_command(command, val="", item_id=None) -> bool
    # item_id 非 None 时附加 id=<item_id> 参数（pl_play / pl_delete 需要）

def player_playlist_items(timeout: float = 2.0, refresh: bool = False) -> list[dict]
    # GET requests/playlist.json -> [{id, name, uri, current}, ...]；失败返回 []
    # refresh=False 时命中模块级缓存直接返回

def player_goto(item_id: int) -> bool          # player_command("pl_play", item_id=item_id)
def player_next() -> bool                      # pl_next
def player_previous() -> bool                  # pl_previous  ← 修正 video_view:772 的 pl_prev
def player_running() -> bool                   # _last_proc is not None and poll() is None
```

`player_playlist_items()` 的 `uri` 字段即我们传给 VLC 的 MRL（不含 `#title` fragment），`video_view` 用它匹配自己生成的 `self._series_urls[i]` 建立 `{集下标: vlc_id}`。`name` 与我们注入的标题不一致时不报错、仅作降级信号（日志 + 映射仍按 `uri` 建）。

### 4.4 video_view：resolver、状态回填、切集不重开

新增状态：`self._series_key: str = ""`、`self._vlc_item_ids: dict[int, int] = {}`、`self._series_urls: list[str] = []`（`self._series_urls[i]` = 第 `i` 集的惰性 URL，用于传 `url=` 与后续 id 匹配）。

新增信号与槽：

```python
_external_now_playing = Signal(int, str, str)   # (集下标, video, audio)
# 连接（自动/队列连接 → 在主线程执行）：
def _on_external_now_playing(self, idx, video, audio):
    self._current_idx = idx
    self._stream_cache[(self._episodes[idx].url, self._quality)] = (video, audio)
    # 刷新 UI：集号标签、选集卡片高亮、进度上下文
```

**`_build_series_playlist()` 取代 `_collect_episode_playlist()`**：

```python
def _build_series_playlist(self, real_url: str, ad_block: dict | None) -> list | None:
    """返回严格 0..N-1 顺序的 episodes 列表；不足 2 集或无 source 时返回 None。
    副作用：注册惰性系列到 media_proxy，并把 self._series_key 存下。"""
```

- 遍历 `enumerate(self._episodes)`，`i` 即集下标，`title = f"第{i+1}集 {ep.title or ''}"`（`.strip()` 后仍以 `第` 开头 → 天然满足非数字约束）。
- 同时把每集的惰性 URL 存入 `self._series_urls`（与 `episodes` 同序）。
- `_play()` 拿到列表后传：`url=self._series_urls[self._current_idx]`、`episodes=<列表>`、`classify_url=<当前集真实流地址>`。
- 惰性 resolver 闭包：
  ```python
  def _resolve(idx):
      ep = self._episodes[idx]
      video, audio = self._content.fetch_video_streams(
          self._source, ep.url, quality=self._quality, merged=True)
      if not video:
          raise ValueError(f"第{idx+1}集取流失败")
      return video, audio, self._headers_for(ep), ad_block
  ```
  —— 与 `_FetchStreamTask.run()` 完全同一条调用，不新增取流分支。
- `on_play` 回调只做一件事：`self._external_now_playing.emit(idx, video, audio)`（在代理线程 emit，槽在主线程跑，**不跨线程碰 Qt 对象**）。

**App 内切集改造**（`_select_episode` / `_on_next_ep` / `_on_prev_ep` 入口统一）：

```
若 self._external_active and self._vlc_item_ids and idx in self._vlc_item_ids:
    player_goto(self._vlc_item_ids[idx])   # 不重开 VLC
    成功 → 乐观更新 _current_idx；真实生效由 on_play 钩子最终确认
    失败 → 落回现有「重开 VLC」路径
否则: 现有行为（_load_episode → _play → open_with_player）
```

「用户在 VLC 里手动点另一集」造成的漂移**由 `on_play` 钩子自动纠正**（它推的是真实正在播的集下标），无需轮询。

**注销时机**（`_unregister_series()`）：`_stop_player()`、换源/`reload_detail()`、视图 close/Dispose、以及**新系列注册前**。注册与注销成对，`_series_key` 置空、`_series_urls`/`_vlc_item_ids` 清空。

**与 VLC 会话生命周期对齐**：若视图销毁/换源时外部播放器**仍在播放**，则**不立即注销**——先 `_terminate_previous()` 终止 VLC 再注销（终止会连带释放租约、清 token）。这条顺序保证不会出现「VLC 还在播但 `/e/` 已 404」。`media_proxy` 侧另有 `_series` 容量上限 8，超出时淘汰最旧的一支（字典插入序），作为兜底。

**`⚙「外部播放器」` 入口（`video_view.py:1763`）**：走同一条 `_build_series_playlist` 路径（此前完全不传 episodes，是缺口 C 的一部分）。

## 5. 数据流

| 场景 | 行为 |
|---|---|
| 选集态点第 5 集 | 注册系列 → MRL 1..N 入列 + `--no-playlist-autostart` → 握手 `pl_play&id=<第5集>` → 从第 5 集开始播 |
| 第 1 集开播（cur=0） | 不加 `--no-playlist-autostart`，走 VLC 原生 autostart（最稳路径，无握手） |
| VLC 里按 N | `pl_next` → 播第 6 集 → 请求 `/e/<key>/5` → 首次取流 + 302 |
| VLC 列表点第 30 集 | 同上，`/e/<key>/29` |
| App 内点「下一集」 | `pl_play&id=<下一集>`，VLC 不重启、进度不丢 |
| 暂停 30 分钟后恢复 | 租约持有中 → 看门狗不回收 → token 仍有效，直接续播 |
| App 退出 | `atexit` → `stop()`（强制）+ `_terminate_previous()` → 租约释放 |
| 用户手动拖进度到未缓冲处 | 302 目标已 memo，VLC 重请求不额外回源 |
| 某集取流失败 | `/e/` 返回 502 → VLC 报该项错误 → 用户可 N 跳过；App 侧 `_current_idx` 不被误更新（`on_play` 只在成功时带值回填） |

## 6. 错误处理

| 情况 | 处理 |
|---|---|
| `key` 未注册（VLC 还在播旧列表） | `/e/` 404。缓解：系列注册**保持到 VLC 进程退出**（`_terminate_previous` 时才注销），不随视图短暂重建注销 |
| `idx` 越界 / 非整数 | 404（`int()` 失败或 `0 <= idx < count` 不满足） |
| resolver 抛异常 | 502，不 memo（可重试） |
| 并发解析超过 4 路 | 排队；`acquire(timeout=20)` 超时返回 503 |
| `playlist.json` 读不到 / 标题对不上 | 跳过 `pl_play` 握手，降级 autostart；`_vlc_item_ids` 留空 → App 切集回落重开 |
| `pl_play` 返回 False | 记日志；App 切集路径回落重开 VLC |
| `player_command` 因 `requests` 缺失返回 False | 现有行为，全链路静默降级 |
| proxy 未启动（`proxy_url_for` 返回原 URL） | 系列 URL 本就是代理 URL，代理必然已启动；`series_episode_url` 在代理未运行时抛错，`_build_series_playlist` 捕获 → 返回 None → 退回单集 |

## 7. 非目标（本轮明确不做）

- **每集独立的 `--network-caching`**：MRL 选项在入列时固定，而流类型要等解析后才知道。整季同源同格式，用**当前集**的分类结果作为全列表基线。若将来出现混合格式剧集再单独处理。
- **系列路径的独立音轨（`:input-slave=`）**：`fetch_video_streams(..., merged=True)` 本就返回合并流（`content.py` 注释：DASH/fMP4 双流 input-slave 不可靠会黑屏），`audio` 实际恒为空。系列路径直接省略 `:input-slave=`；resolver 若返回非空 audio 则忽略并记一次 warning。**这是已知限制**，不是遗漏。
- 媒体库持久化、导出 M3U、循环模式控制（留给用户 vlcrc）、多轨/字幕切换、App 侧播放进度回读（`position_snapshot` 仍恒为 0，属既有问题，另议）。

## 8. 测试策略

**单元测试（可 mock，覆盖全部新逻辑）**

`tests/test_media_proxy_series.py`（新）
- `register_series` 返回 key；`series_episode_url` 形如 `http://127.0.0.1:<port>/e/<key>/<idx>`
- `/e/<key>/<idx>` → 302 且 `Location` 是 `build_url` 产出的 token URL
- 同一 idx 请求两次 → resolver 只被调用 1 次（memo 生效）
- resolver 抛异常 → 502，且**第二次请求会再调 resolver**（未 memo）
- `idx` 越界 / 非整数 / key 未注册 → 404，且 resolver 从未被调用
- `/e/` 会调用 `on_play(idx, ...)`
- `acquire_lease` 后空闲回收不触发；`release_lease` 后恢复触发

`tests/test_external_player_playlist.py`（**改写既有 8 例的契约断言** + 新增）
- MRL 顺序 == `episodes` 完整顺序（`episodes[0]` **不再被跳过**）
- 每条 MRL 带 `#第NN集 ...`；标题含 `#`/换行被消毒；纯数字标题被加前缀
- `episodes` 非空 → 有 `--no-random`；`episodes is None` → **无** `--no-random`（`len(args)==9` 的既有用例继续成立）
- 起始项 == `episodes[0]` → 无 `--no-playlist-autostart`；否则有
- 系列路径**不出现** `:input-slave=`
- `classify_url` 非空时用它分类（断言 `--network-caching` 取自真实流地址）
- `player_command("pl_play", item_id=7)` 请求里带 `id=7` 而非 `val=7`
- `player_playlist_items()` 解析 `playlist.json`；失败返回 `[]`
- `Popen` 成功 → `acquire_lease`；`_terminate_previous` → `release_lease`

`tests/test_video_series_playlist.py`（新）
- `_build_series_playlist` 产出严格 `0..N-1` 顺序、标题为 `第NN集 ...`、不足 2 集返回 None，且 `self._series_urls` 与 `episodes` 同序
- resolver 闭包用 `merged=True` 且传对应 `ep.url`
- `_on_external_now_playing` 更新 `_current_idx` 并写 `_stream_cache`
- App 切集：有 `_vlc_item_ids` → 发 `pl_play` 且**不调用** `open_with_player`；无映射或失败 → 回落重开
- `_vlc_item_ids` 由 `player_playlist_items()` 的 `uri` 匹配 `self._series_urls` 建立
- **顺序锁定**：视图销毁/换源且外部仍在播放时，**先 terminate 再注销**（断言注销发生在 terminate 之后）
- `video_view` 的 `P` 键发的是 `pl_previous`（回归锁定 §1 缺口 E）

**手动 GUI 验收（单元测试无法证明 VLC 行为，必须实测）**

1. 打开一个 ≥10 集的剧集 → `Ctrl+L` → 列表显示 `第01集 …` 到 `第NN集 …` 真实标题，顺序正确
2. 从第 5 集开播 → 确认**从第 5 集开始播**（不是第 1 集）
3. 列表里点第 30 集 → 能播；按 N → 第 31 集；按 P → 回到第 30 集
4. ikanpp 源：确认第 2 集及之后仍走 30s 缓冲（无新卡顿）
5. 暂停 15 分钟后恢复 → 不断流
6. App 内点「下一集」→ VLC 不重启、不闪窗、进度不丢
7. 代理日志确认 `/e/` 每集只触发一次上游取流（memo 生效）

## 9. 风险与缓解

| 风险 | 缓解 |
|---|---|
| `#title` 未按预期显示在 Qt 列表 | `playlist.json` 的 `name` 字段可**自校验**：对不上就走降级并记日志；GUI 验收第 1 条会立刻暴露 |
| 启动握手竞态（VLC 尚未就绪） | 轮询 3s + 降级 autostart，最坏结果是播第 1 集（不致命、不崩） |
| 惰性解析把代理线程占满 | `Semaphore(4)` + 20s 超时；memo 让重复点击零成本 |
| 租约导致 token 永不回收 | token 仅在「真正播过的集」产生，每项几百字节；VLC 进程退出即释放 |
| 改契约破坏既有 8 个测试 | 这些测试锁定的正是缺陷契约（当前集优先、跳过 `episodes[0]`），必须随契约一起改写；改写前后逐条 review |
| 某源剧集数上千 | MRL 数量 = 集数；1000 条 argv 远低于 Windows 命令行 32KB 限制（每条约 60 字节 ≈ 60KB，**超限**）→ 需在 §10 处理 |

## 10. 命令行长度上限（必须处理）

Windows CreateProcess 命令行上限 **32767 字符**。每条系列 MRL 约 `http://127.0.0.1:PORT/e/<12hex>/<idx>#第NN集 <标题>` ≈ 50~80 字节（含标题）。因此：

- 集数 ≤ ~350：安全。
- 集数 > ~350：**超限，`Popen` 会抛 `OSError`**，被现有 `except Exception` 吞掉 → 静默降级到 `webbrowser.open(url)`，用户看到的是「已在浏览器中打开」，非常难排查。

处理（本次一并实现，不留坑）：

1. `open_with_player` 在拼 args **之前**估算总长度（`sum(len(a) for a in args) + 固定开销`）；
2. 超限时按「保留当前集及其后连续 N 集」的窗口截断（从当前集往后优先，保证「下一集」永远可用），并在返回的提示串里说明「列表已截断」；
3. 截断窗口大小由常量 `_SERIES_MAX_MRL` 控制（保守取 300）；
4. 补一条单测锁定「超长剧集不会让 `Popen` 抛异常」。

这同时缓解了「上千集剧集一次性 enqueue 数千项」对 VLC 自身 playlist 的压力。

## 11. 验收标准

- [ ] `Ctrl+L` 播放列表显示全集真实剧集标题，顺序为第 1 集 → 最后一集
- [ ] 从第 N 集开播，实际从第 N 集开始播
- [ ] 列表内点击任意集能正常播放该集
- [ ] `N` = 下一集、`P` = 上一集，均生效
- [ ] ikanpp 源第 2 集及之后仍无卡顿（30s 缓冲未被惰性 URL 破坏）
- [ ] 暂停 >10 分钟恢复不断流
- [ ] App 内「上一集/下一集」与选集卡片不重开 VLC
- [ ] 用户在 VLC 内手动跳集后，App 侧集号同步正确
- [ ] 上千集剧集不触发命令行超限，有明确截断提示
- [ ] 新增/改写单测全绿；全量 `pytest` 0 失败（沿用既有 2 个 ignore）
- [ ] 密钥扫描通过；无关文件（`sources/fanqie.json.bak-fanqie-categories`）不入库
