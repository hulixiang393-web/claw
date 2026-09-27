# VLC 外部播放器全集播放列表 + 惰性解析 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 VLC 的播放列表显示全集真实剧集标题（严格第 1 集→最后一集），每集流地址在被播到时才解析，并支持 App 内切集不重开 VLC。

**Architecture:** `media_proxy` 新增「惰性系列注册表 + `GET /e/<key>/<idx>` 端点」：VLC 命令行一次性收到全集 MRL（`http://127.0.0.1:PORT/e/<key>/<idx>#第NN集 标题`），真正播到第 k 集才在代理线程内调 `resolver(k)` 取流、memo 后 302 到既有 `/s/<token>` 通道。`external_player` 改为「全集按序入列 + 标题 fragment + `--no-random`」，从非首集开播时用 `--no-playlist-autostart` + 后台轮询 `playlist.json` + `pl_play&id=<id>` 定位。`video_view` 装配系列、把 `/e/` 回调经 Qt 信号回填当前集状态，并把 App 内上一集/下一集/选集改为 `pl_play` 指挥 VLC。

**Tech Stack:** Python 3 / PySide6（`Signal` 跨线程队列投递）/ `ThreadingHTTPServer` / `requests` / `pytest`（`tests/conftest.py` 提供 session 级 `_qapp` 离屏 fixture）

## Global Constraints

- 设计规格：`docs/superpowers/specs/2026-09-27-vlc-series-playlist-design.md`（已批准，commit `735fff8`）。本计划是它的实施分解。
- 契约变更（有意破坏）：`open_with_player(episodes=...)` 的 `episodes` 语义改为「**全集完整有序列表**」，`episodes[i]` = 第 `i` 集（0-based），**`episodes[0]` 不再被跳过**。`url` 参数只用于定位起始项 + `classify_url` 缺省时的分类。
- 唯一调用方是 `gui/pages/reader/video_view.py`；`app.py:828` 传本地文件且不传 `episodes`，行为不变。
- 系列路径**不追加** `:input-slave=`（`fetch_video_streams(..., merged=True)` 返回合并流，DASH/fMP4 双流 input-slave 会黑屏）。
- MRL 标题 fragment 必须以非数字开头，否则 VLC 按 `mrl-title` 时间偏移解析（纯数字 → 跳转秒数）。调用方一律用 `第NN集 <章节名>` 形式，`_sanitize_title` 再做一次防御。
- 惰性 resolver 必须是 `fetch_video_streams(source, ep.url, quality=self._quality, merged=True)` —— 与 `_FetchStreamTask.run()`（`gui/pages/reader/video_view.py:124-126`）完全同一条调用，不新增取流分支。
- resolver 在 `media_proxy` 的请求处理线程内被调用（`ThreadingHTTPServer`，`framework/media_proxy.py:706`）。`framework/content.py` 无 Qt 依赖，故可直接调用；**任何 Qt 对象只能在信号槽里碰**。
- 命令行上限：Windows `CreateProcess` 32767 字符。裁剪常量 `_SERIES_MAX_MRL = 300`、`_SERIES_MAX_CMD = 30000`。**正常路径全集按 1..N 严格入列**（当前集在列内中段，由启动握手定位）；仅当全集超上限时才降级为「当前集往后」的窗口。
- 相关既有行为不得回归：ikanpp 的 `media.hls.network_caching_ms = 30000`（`sources/ikanpp.json`）必须仍生效 → 系列路径的 `--network-caching` 必须用**当前集真实流地址**分类。
- 提交信息不带 `Co-Authored-By` / `Signed-off-by`；每个 Task 结束一次提交。
- PowerShell 无 heredoc：需要多行提交信息时先写临时文件再 `git commit -F <file>`。
- 无关未跟踪文件 `sources/fanqie.json.bak-fanqie-categories` **不得入库**（每次 `git add` 只点名单文件）。
- 全量测试命令：`python -m pytest --ignore=tests/test_comic_scroll_anchor.py --ignore=tests/test_comic_view_referer.py -q -p no:randomly`

## 与已批准规格的偏差（实施前已确认，实施时按本计划执行）

1. **`register_series` 多一个关键字参数 `force_proxy`**（规格 §4.1 签名未含）。原因：`build_url(target, headers, ad_block, force_proxy)` 需要它，而 `video_view._force_proxy_enabled()`（`video_view.py:1458`）已存在；规格 §4.1 的示例调用漏传会导致 hanime1 源丢失 `force_proxy`。这是加参数，不破坏规格语义。
2. **不终止外部 VLC**。规格 §4.4 要求「视图销毁/换源时先 `_terminate_previous()` 再注销」。这会让「离开视频页 → 正在播的 VLC 被杀掉」，与现有 UX（`stop_playback` 只清 `_external_active`，VLC 继续播）冲突。改为：**只要 VLC 还活着就保留系列注册**（VLC 仍可能请求 `/e/`），仅在 (a) 注册新系列前、(b) `shutdown_video()`（App 退出）、(c) `player_running()` 为假时注销。代理侧 `_SERIES_MAX = 8` 的 FIFO 淘汰兜底泄漏。
3. **剧集标题不重复加集号**。规格 §4.4 写 `f"第{i+1}集 {ep.title or ''}"`，而多数源的 `Chapter.title` 本身就是「第1集 章节名」→ 播放列表会显示「第1集 第1集 章节名」。改为：源标题非空就用源标题，为空才用「第NN集」兜底。非数字开头的防御仍在 `_sanitize_title` 里。
4. **握手映射由 `open_with_player` 的 `on_playlist_ready` 回调回传**，而不是让 `video_view` 自己去轮询 `player_playlist_items()`——后者会在 GUI 主线程上阻塞最多 3s（规格 §4.4 未指定由谁轮询，此处选不卡 UI 的方案）。

## File Structure

| 文件 | 责任 |
|---|---|
| `framework/media_proxy.py` | 新增：系列注册表（`register_series`/`unregister_series`/`series_episode_url`）、`/e/<key>/<idx>` 路由、租约（`acquire_lease`/`release_lease`/`_idle_expired`）。既有 `_tokens`/`/s/`、`/c/`、`_forward*` 一行不动。 |
| `framework/external_player.py` | 新增：标题消毒与 MRL 组装、系列裁剪、VLC 控制面（`player_command(item_id=)`、`player_playlist_items`、`player_goto/next/previous/running`）、租约接线、后台握手线程。 |
| `gui/pages/reader/video_view.py` | 新增：`_series_key`/`_series_urls`/`_vlc_item_ids` 状态、两个信号、`_build_series_playlist`、`_on_external_now_playing`、`_try_external_goto`、`_unregister_series`；改：`_play` 传系列、`_select_episode` cutover、`_handle_key` 的 `pl_prev`→`pl_previous`、`_open_external` 接入系列。 |
| `tests/test_media_proxy_series.py` | 新建：租约 + 系列注册表 + `/e/` 端点（真实本地 HTTP 请求）。 |
| `tests/test_vlc_player_command.py` | 新建：VLC 控制面（mock `requests`）。 |
| `tests/test_external_player_playlist.py` | **改写**既有 8 例的契约断言 + 新增标题/顺序 flag/裁剪/租约/握手。 |
| `tests/test_video_series_playlist.py` | 新建：系列装配、状态回填、切集不重开、注销顺序。 |

---

### Task 1: media_proxy 租约制令牌生命周期

**Files:**
- Modify: `framework/media_proxy.py:443-464`（`__init__` 加 `_leases`）、`framework/media_proxy.py:635-643`（`_start_idle_watch`）、新增方法放在 `acquire/release` 段
- Test: `tests/test_media_proxy_series.py`（新建）

**Interfaces:**
- Consumes: 无
- Produces:
  - `MediaProxy.acquire_lease() -> str` — 返回 lease id 并登记
  - `MediaProxy.release_lease(lease_id: str) -> None` — 幂等移除
  - `MediaProxy._idle_expired() -> bool` — 看门狗回收判据（`self._server is not None and 超时 and not 有租约`）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_media_proxy_series.py`：

```python
# -*- coding: utf-8 -*-
"""media_proxy 租约 + 惰性系列端点：VLC 存活期间看门狗不得回收代理。"""
import threading
import time

import pytest
import requests

import framework.media_proxy as mp
from framework.media_proxy import MediaProxy


class _OffCache:
    enabled = False


@pytest.fixture
def proxy_ctx():
    """起真实代理的用例必须在收尾停掉它。

    _ensure_server() 会绑 ThreadingHTTPServer + serve_forever daemon 线程；
    不停就留到会话末尾（只靠 atexit 兜底），端口和线程会越积越多。
    与 tests/test_media_cache.py 的 proxy_ctx 同一写法。
    """
    proxy = MediaProxy(cache=_OffCache())
    yield proxy
    proxy.stop()
    proxy._leases.clear()


def test_lease_ids_unique_and_releasable():
    proxy = MediaProxy(cache=_OffCache())
    a = proxy.acquire_lease()
    b = proxy.acquire_lease()
    assert a and b and a != b
    proxy.release_lease(a)
    assert a not in proxy._leases
    assert b in proxy._leases
    # 幂等：重复释放不抛异常
    proxy.release_lease(a)
    assert b in proxy._leases


def test_idle_expired_skipped_while_leased(proxy_ctx, monkeypatch):
    """有租约时即使远超空闲超时也不判过期（VLC 暂停超 10 分钟不断流）。

    用 monkeypatch 把 _IDLE_TIMEOUT 调小、并把 _last_use 设到**刚越过**该阈值
    （`time.time() - mp._IDLE_TIMEOUT - 1`），这样断言真的证明了「判据在调用时
    读全局 _IDLE_TIMEOUT」——999 秒那个魔法数在 600s 默认值下也能过，会让这个
    补丁变成死代码。
    """
    monkeypatch.setattr(mp, "_IDLE_TIMEOUT", 30.0)
    proxy = proxy_ctx
    proxy._ensure_server()
    proxy._last_use = time.time() - mp._IDLE_TIMEOUT - 1   # 刚好越过阈值
    assert proxy._idle_expired() is True         # 无租约 → 判过期
    lid = proxy.acquire_lease()
    assert proxy._idle_expired() is False        # 有租约 → 不回收
    proxy.release_lease(lid)
    assert proxy._idle_expired() is True         # 释放后恢复回收


def test_release_lease_with_unknown_id_is_noop():
    """释放从未获取过的 id：静默无副作用（discard 语义，不抛异常）。"""
    proxy = MediaProxy(cache=_OffCache())
    proxy.release_lease("never-acquired")
    assert not proxy._leases


def test_stop_works_while_leased(proxy_ctx):
    """**整个设计依赖的属性**：持租约时 stop() 仍必须关闭代理。

    stop() 是唯一清 _tokens 的地方；若它日后变成租约感知，App 退出就会挂住。
    """
    proxy = proxy_ctx
    proxy.acquire_lease()
    proxy._ensure_server()
    assert proxy._server is not None
    proxy.stop()
    assert proxy._server is None


def test_idle_expired_false_without_server():
    """代理未起 → 不判过期（避免构造期误触发 stop）。"""
    proxy = MediaProxy(cache=_OffCache())
    proxy._last_use = time.time() - 999
    assert proxy._idle_expired() is False
```

- [ ] **Step 2: 运行测试确认失败**

```
cd D:\code\claw; python -m pytest tests/test_media_proxy_series.py -q -p no:randomly
```
Expected: FAIL —— `AttributeError: 'MediaProxy' object has no attribute 'acquire_lease'`

- [ ] **Step 3: 最小实现**

`framework/media_proxy.py`，`__init__` 内 `self._active = 0` 一行之后追加：

```python
        # 外部播放器租约：VLC 进程存活期间持有的 lease id 集合。**非空时
        # 空闲看门狗完全跳过回收** —— 否则用户暂停超 _IDLE_TIMEOUT 后
        # stop() 会 _tokens.clear()，VLC 恢复播放时全部 404。VLC 退出 /
        # 重开播放器 / App 退出时由 external_player 释放。
        self._leases: set[str] = set()
```

把 `_start_idle_watch`（当前 `framework/media_proxy.py:635-643`）替换为：

```python
    def _start_idle_watch(self) -> None:
        def _watch():
            while True:
                time.sleep(_WATCH_INTERVAL)
                if self._idle_expired():
                    self.stop()
        self._idle_watch = threading.Thread(target=_watch, daemon=True)
        self._idle_watch.start()

    def _idle_expired(self) -> bool:
        """看门狗回收判据：代理已起 + 空闲超时 + **无外部播放器租约**。"""
        with self._lock:
            if self._leases:
                return False
        return (self._server is not None
                and time.time() - self._last_use > _IDLE_TIMEOUT)

    def acquire_lease(self) -> str:
        """登记一个外部播放器租约（返回 lease id）。

        只要有租约，空闲看门狗就不会 stop() 代理、不会清 token —— 播放器
        暂停/长时间不发起请求时 URL 仍有效。成对调用 release_lease()。
        """
        lease_id = uuid.uuid4().hex
        with self._lock:
            self._leases.add(lease_id)
        return lease_id

    def release_lease(self, lease_id: str) -> None:
        """释放租约（幂等：未知 id 静默忽略）。"""
        with self._lock:
            self._leases.discard(lease_id)
```

- [ ] **Step 4: 运行测试确认通过**

```
cd D:\code\claw; python -m pytest tests/test_media_proxy_series.py -q -p no:randomly
```
Expected: 3 passed

- [ ] **Step 5: 回归既有代理测试**

```
cd D:\code\claw; python -m pytest tests/test_media_proxy_ad.py tests/test_media_proxy_force.py tests/test_media_proxy_stream_tuning.py tests/test_proxy_pool.py tests/test_system_proxy_fallback.py -q -p no:randomly
```
Expected: 全部 passed（看门狗判据重构不得影响既有转发行为）

- [ ] **Step 6: 提交**

```
git add framework/media_proxy.py tests/test_media_proxy_series.py
git commit -m "fix(proxy): 外部播放器租约——VLC 存活期间空闲看门狗不回收代理"
```

---

### Task 2: media_proxy 惰性系列注册表 + `/e/<key>/<idx>` 端点

**Files:**
- Modify: `framework/media_proxy.py`（模块常量、`__init__`、`do_GET`、`_serve_series`、三个公开方法）
- Test: `tests/test_media_proxy_series.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `MediaProxy` 单例与 `_lock`
- Produces:
  - `MediaProxy.register_series(resolver, on_play=None, count=0, force_proxy=False) -> str` — `resolver(idx) -> (video, audio, headers, ad_block)`；`on_play(idx, video, audio)`；返回 12 位 hex key；注册表容量 `_SERIES_MAX = 8`（超出淘汰最旧）
  - `MediaProxy.unregister_series(key: str) -> None`
  - `MediaProxy.series_episode_url(key: str, idx: int) -> str` — `http://127.0.0.1:<port>/e/<key>/<idx>`
  - `MediaProxy._serve_series(handler, path) -> None` — 302/404/502/503

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_media_proxy_series.py`：

```python
import requests


def _series(resolver, on_play=None, count=3, force_proxy=False):
    proxy = MediaProxy(cache=_OffCache())
    key = proxy.register_series(resolver, on_play, count=count,
                                force_proxy=force_proxy)
    urls = [proxy.series_episode_url(key, i) for i in range(count)]
    return proxy, key, urls


def test_series_url_shape():
    proxy, key, urls = _series(lambda i: ("v", "a", {}, None), count=2)
    port = proxy._server.server_address[1]
    assert urls[0] == f"http://127.0.0.1:{port}/e/{key}/0"
    assert urls[1] == f"http://127.0.0.1:{port}/e/{key}/1"
    assert len(key) == 12


def test_episode_redirects_to_token_url():
    calls = []

    def _resolve(idx):
        calls.append(idx)
        return f"https://cdn.example.com/e{idx}.m3u8", "", {"Referer": "r"}, None

    proxy, _key, urls = _series(_resolve, count=3)
    r = requests.get(urls[1], allow_redirects=False, timeout=5)
    assert r.status_code == 302
    assert r.headers["Cache-Control"] == "no-store"
    # Location 必须是 build_url 产出的 /s/<token>，不是 /e/ 本身
    assert "/s/" in r.headers["Location"]
    assert calls == [1]


def test_episode_memoized():
    calls = []

    def _resolve(idx):
        calls.append(idx)
        return f"https://cdn.example.com/e{idx}.m3u8", "", {}, None

    proxy, _key, urls = _series(_resolve, count=2)
    first = requests.get(urls[0], allow_redirects=False, timeout=5)
    second = requests.get(urls[0], allow_redirects=False, timeout=5)
    assert first.headers["Location"] == second.headers["Location"]
    assert calls == [0]          # 重复请求不再回源


def test_resolver_failure_502_and_not_memoized():
    state = {"n": 0}

    def _resolve(idx):
        state["n"] += 1
        if state["n"] == 1:
            raise ValueError("boom")
        return "https://cdn.example.com/ok.m3u8", "", {}, None

    proxy, _key, urls = _series(_resolve, count=1)
    r1 = requests.get(urls[0], allow_redirects=False, timeout=5)
    assert r1.status_code == 502
    r2 = requests.get(urls[0], allow_redirects=False, timeout=5)
    assert r2.status_code == 302  # 失败不写 memo → 允许重试
    assert state["n"] == 2


def test_bad_index_and_key_404_without_resolving():
    calls = []

    def _resolve(idx):
        calls.append(idx)
        return "https://cdn.example.com/x.m3u8", "", {}, None

    proxy, key, urls = _series(_resolve, count=3)
    port = proxy._server.server_address[1]
    for path in (f"/e/{key}/3", f"/e/{key}/-1", f"/e/{key}/abc",
                 f"/e/{key}/1/2", f"/e/deadbeef/0", "/e//0", f"/e/{key}"):
        r = requests.get(f"http://127.0.0.1:{port}{path}",
                         allow_redirects=False, timeout=5)
        assert r.status_code == 404, path
    assert calls == []


def test_on_play_called_every_request():
    seen = []
    proxy, _key, urls = _series(
        lambda i: (f"https://cdn.example.com/{i}.m3u8", "", {}, None),
        on_play=lambda idx, v, a: seen.append((idx, v, a)), count=2)
    requests.get(urls[0], allow_redirects=False, timeout=5)
    requests.get(urls[0], allow_redirects=False, timeout=5)
    requests.get(urls[1], allow_redirects=False, timeout=5)
    assert [s[0] for s in seen] == [0, 0, 1]
    assert seen[0][1] == "https://cdn.example.com/0.m3u8"


def test_unregister_series_404_after():
    calls = []
    proxy, key, urls = _series(
        lambda i: (calls.append(i) or "https://cdn.example.com/x.m3u8", "", {}, None),
        count=1)
    proxy.unregister_series(key)
    r = requests.get(urls[0], allow_redirects=False, timeout=5)
    assert r.status_code == 404
    assert calls == []


def test_series_max_evicts_oldest():
    """注册表上限 _SERIES_MAX：第 9 支挤掉最旧的一支，最旧的 404、新的照常 302。

    淘汰那个 while 若被改成 if（只挤一支而非循环到达标），本测试会红。
    """
    keys, urls = [], []
    for i in range(mp._SERIES_MAX + 1):
        k = proxy.register_series(
            lambda i: ("https://cdn.example.com/x.m3u8", "", {}, None),
            count=1)
        keys.append(k)
        urls.append(proxy.series_episode_url(k, 0))
    assert len(proxy._series) == mp._SERIES_MAX
    assert keys[0] not in proxy._series          # 最旧被淘汰
    assert keys[-1] in proxy._series            # 最新的留下
    r0 = requests.get(urls[0], allow_redirects=False, timeout=5)
    assert r0.status_code == 404
    rn = requests.get(urls[-1], allow_redirects=False, timeout=5)
    assert rn.status_code == 302


def test_episode_503_when_resolver_slots_busy(proxy_ctx, monkeypatch):
    """解析槽位占满且等待超时 → 503，且不泄漏信号量许可。"""
    proxy = proxy_ctx
    monkeypatch.setattr(mp, "_SERIES_WAIT", 0.01)
    gate = threading.Event()
    started = threading.Barrier(mp._SERIES_SEM + 1, timeout=10)

    def _slow(i):
        # 只有被占满的那一集参与 barrier/闸门。后置断言请求的是第 1 集，若也
        # 进 barrier，会**再次进入**已放行的 barrier（CPython 的 Barrier 换代后
        # 可重用，parties 重新计数且永远凑不齐）→ 卡满 timeout 后
        # BrokenBarrierError → _serve_series 的 except Exception → 502。
        if i == 0:
            started.wait()
            gate.wait(timeout=10)
        return "https://cdn.example.com/x.m3u8", "", {}, None

    key = proxy.register_series(_slow, count=2)
    url = proxy.series_episode_url(key, 0)
    busy = [threading.Thread(target=requests.get,
                             args=(url,), kwargs={"allow_redirects": False,
                                                   "timeout": 10})
            for _ in range(mp._SERIES_SEM)]
    for t in busy:
        t.start()
    try:
        started.wait()                                   # 槽位已占满
        r = requests.get(url, allow_redirects=False, timeout=5)
        assert r.status_code == 503
    finally:
        gate.set()
        for t in busy:
            t.join(timeout=10)
    # 许可未泄漏：占满的请求都完成后，**另一集**仍能解析。
    # 必须换一集（count=2 → 请求 idx 1）：同 key 同 idx 会命中 idx 0 刚写下的
    # memo，根本走不到 sem.acquire，断言再对也证明不了「许可没泄漏」。
    r2 = requests.get(proxy.series_episode_url(key, 1),
                      allow_redirects=False, timeout=5)
    assert r2.status_code == 302


def test_episode_empty_video_url_502_and_not_memoized():
    """解析成功但 video 为空 → 502，且不写 memo（下次仍重新解析）。"""
    calls = []

    def _empty(i):
        calls.append(i)
        return "", "", {}, None

    key = proxy.register_series(_empty, count=1)
    url = proxy.series_episode_url(key, 0)
    r = requests.get(url, allow_redirects=False, timeout=5)
    assert r.status_code == 502
    r2 = requests.get(url, allow_redirects=False, timeout=5)
    assert r2.status_code == 502
    assert calls == [0, 0]               # 没有被 memo 住


def test_stop_clears_series_so_memo_cannot_outlive_token(proxy_ctx):
    """stop() 必须连 _series 一起清。

    否则：已 memo 的 302 指向的 token 被 stop() 清掉 → 该集此后永久 404
    且不会重新解析（memo 一直命中一个死 token）。
    注意 stop() 之后旧 key 必然 404（注册表已清），**不是** 302；要证明
    「重新注册能恢复」得显式再注册一次。真正能区分修没修的是 stop() 后那个
    404 —— 没修的话 memo 照样命中，一样返回 302。
    """
    calls = []
    proxy = proxy_ctx
    key = proxy.register_series(
        lambda i: (calls.append(i) or "https://cdn.example.com/x.m3u8", "", {}, None),
        count=1)
    url = proxy.series_episode_url(key, 0)
    assert requests.get(url, allow_redirects=False, timeout=5).status_code == 302
    assert len(calls) == 1                       # 已 memo（302 走的是写 memo 的成功路径）

    proxy.stop()
    assert not proxy._series
    assert not proxy._tokens

    url2 = proxy.series_episode_url(key, 0)      # 新端口上的同 key
    r = requests.get(url2, allow_redirects=False, timeout=5)
    assert r.status_code == 404                  # 没修的话这里是 302（指向死 token）
    assert calls == [0]                          # 且不会重新解析

    # 「清注册表」不能留下死路：重新注册同 key 后应能正常解析
    key2 = proxy.register_series(
        lambda i: (calls.append(i) or "https://cdn.example.com/x.m3u8", "", {}, None),
        count=1)
    r2 = requests.get(proxy.series_episode_url(key2, 0),
                      allow_redirects=False, timeout=5)
    assert r2.status_code == 302
    assert calls == [0, 0]
```

- [ ] **Step 2: 运行测试确认失败**

```
cd D:\code\claw; python -m pytest tests/test_media_proxy_series.py -q -p no:randomly
```
Expected: FAIL —— `AttributeError: 'MediaProxy' object has no attribute 'register_series'`

- [ ] **Step 3: 最小实现**

3a. 模块常量，`framework/media_proxy.py` 的 `_MEDIA_HDRS` 之后追加：

```python
# 惰性系列（外部播放器全集播放列表）同时注册的最大支数。注册表是有序 dict，
# 超出时淘汰最旧的一支（兜底：App 反复换源不注销时的注册泄漏）。
_SERIES_MAX = 8
# 单集惰性解析的并发上限（避免多集同时点播把源站/反爬打爆）。
_SERIES_SEM = 4
# 单集惰性解析排队等待上限（秒）；超时回 503。
_SERIES_WAIT = 20.0
```

3b. `__init__` 内 `self._leases: set[str] = set()` 之后追加：

```python
        # 惰性系列注册表：key → {resolver, on_play, count, memo, sem}
        #   memo: {idx: (video, audio)} 按集缓存，重复请求（VLC 重试/重播）不回源
        #   sem:  该系列独占的解析并发信号量
        self._series: dict[str, dict] = {}
```

3c. `do_GET` 内，`if path.startswith("/c/"):` 分支之后、`token = path.rsplit(...)` 之前插入：

```python
            if path.startswith("/e/"):
                try:
                    proxy._serve_series(self, path)
                except Exception as exc:  # noqa: BLE001 —— 解析异常已在内部归类
                    try:
                        self.send_error(502, f"proxy error: {exc}")
                    except Exception:
                        pass
                return
```

3d. 在 `build_url` 之后新增三个公开方法与 `_serve_series`：

```python
    # ------------------------------------------------------------------ #
    # 惰性系列：外部播放器全集播放列表
    # ------------------------------------------------------------------ #
    def register_series(self, resolver, on_play=None, count: int = 0,
                        force_proxy: bool = False) -> str:
        """注册一支惰性解析系列，返回不透明 key。

        resolver(idx) -> (video, audio, headers, ad_block)；抛异常表示该集取流
        失败（/e/ 回 502 且不 memo，允许重试）。ad_block 传 None 即不过滤广告段。
        on_play(idx, video, audio) 在**每次** /e/ 请求时回调（memo 命中也回调），
        供 App 侧同步「正在播第几集」——在代理线程执行，实现方须自行跨线程。
        count 为集数上限，越界回 404。force_proxy 透传给 build_url。
        """
        self._ensure_server()
        key = uuid.uuid4().hex[:12]
        with self._lock:
            self._series[key] = {
                "resolver": resolver, "on_play": on_play, "count": int(count),
                "memo": {}, "sem": threading.Semaphore(_SERIES_SEM),
                "force_proxy": bool(force_proxy),
            }
            while len(self._series) > _SERIES_MAX:   # FIFO 兜底：淘汰最旧一支
                self._series.pop(next(iter(self._series)))
        return key

    def unregister_series(self, key: str) -> None:
        """注销系列并丢弃其 memo（幂等）。"""
        with self._lock:
            self._series.pop(key, None)

    def series_episode_url(self, key: str, idx: int) -> str:
        """第 idx 集的惰性 URL（绝对地址，不含 #标题 fragment）。"""
        self._ensure_server()
        return (f"http://127.0.0.1:{self._server.server_address[1]}"
                f"/e/{key}/{int(idx)}")

    def _serve_series(self, handler, path: str) -> None:
        """/e/<key>/<idx>：按集惰性解析 → 302 到 /s/<token>。"""
        parts = path[len("/e/"):].split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            handler.send_error(404, "bad series path")
            return
        key, raw_idx = parts[0], parts[1]
        # isdecimal 而非 isdigit：isdigit() 对上标（如 "²"）返回 True 而 int()
        # 抛 ValueError → 落到外层 502，并把客户端输入回显进 HTTP reason
        # phrase。长度上限 9 位：避免超长数字串触发 3.11+ 的 int 转换位长限制。
        if not raw_idx.isdecimal() or len(raw_idx) > 9:
            handler.send_error(404, "bad episode index")
            return
        idx = int(raw_idx)
        with self._lock:
            ser = self._series.get(key)
            if ser is None:
                handler.send_error(404, "series not found")
                return
            if not (0 <= idx < int(ser["count"])):
                handler.send_error(404, "episode out of range")
                return
            entry = ser["memo"].get(idx)
            sem = ser["sem"]
            resolver, on_play = ser["resolver"], ser["on_play"]
            force_proxy = ser["force_proxy"]
        if entry is None:
            if not sem.acquire(timeout=_SERIES_WAIT):
                handler.send_error(503, "too many concurrent resolves")
                return
            try:
                video, audio, headers, ad_block = resolver(idx)
            except Exception:  # noqa: BLE001 —— 该集取流失败：502 且**不**写 memo
                handler.send_error(502, "episode resolve failed")
                return
            finally:
                sem.release()
            if not video:
                handler.send_error(502, "empty stream url")
                return
            # memo 存 (video, audio, location)：命中时直接拿 token URL 重定向，
            # 既不回源也不用再扫 _tokens 反查。
            entry = (video, audio,
                     self.build_url(video, headers, ad_block=ad_block,
                                    force_proxy=force_proxy))
            with self._lock:
                cur = self._series.get(key)      # 解析期间可能已被注销
                if cur is not None:
                    cur["memo"][idx] = entry
        if on_play is not None:
            try:
                on_play(idx, entry[0], entry[1])
            except Exception:  # noqa: BLE001 —— 回调异常不阻断播放
                pass
        handler.send_response(302)
        handler.send_header("Location", entry[2])
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
```

3f. `stop()` 内 `self._tokens.clear()` 之前插入（**Task 2 审查裁决**：只清 token
不清注册表，已 memo 的 302 就会指向被清掉的 token，此后该集永久 404 且不会
重新解析——租约接线要到 Task 5 才存在，本任务落地时看门狗完全可能在会话中途
回收代理；顺手也把每支的信号量一并释放掉）：

```python
        self._series.clear()   # memo 一并作废（Task 2 审查）。清掉 dict 也顺带丢弃
                               # 每支的 Semaphore 对象；已在途的持有者仍会在自己
                               # 的 finally 里 release 自己那个引用。
        self._tokens.clear()
```

注：`_series` 的 `memo` 注释同步改为 `{idx: (video, audio, location)}`。
代理被 `stop()` 强制回收后 memo 里的 token 会失效 —— **原判断「只发生在 App 退出（`atexit`）时」是错的**（Task 2 审查裁决）：租约接线在 Task 5，本任务落地时没有任何东西阻止 `_idle_expired()` 在会话中途返回 True。`stop()` 已改为在 `_tokens.clear()` 旁一并 `self._series.clear()`，memo 随之消失，下次请求重新解析。

- [ ] **Step 4: 运行测试确认通过**

```
cd D:\code\claw; python -m pytest tests/test_media_proxy_series.py -q -p no:randomly
```
Expected: 全部 passed（5 个租约 + 11 个系列用例）

- [ ] **Step 5: 回归代理全组**

```
cd D:\code\claw; python -m pytest tests/ -q -p no:randomly -k "proxy or cache"
```
Expected: 全部 passed

- [ ] **Step 6: 提交**

```
git add framework/media_proxy.py tests/test_media_proxy_series.py
git commit -m "feat(proxy): 惰性系列注册表 + /e/<key>/<idx> 端点（按集解析 + memo）"
```

---

### Task 3: external_player 控制面（`pl_play&id`、`pl_previous`、播放列表读取）

**Files:**
- Modify: `framework/external_player.py:41-46`（模块状态）、`framework/external_player.py:66-82`（`_terminate_previous`）、`framework/external_player.py:229-250`（`player_command`）
- Test: `tests/test_vlc_player_command.py`（新建）

**Interfaces:**
- Consumes: 无（纯控制面，不依赖 Task 1/2）
- Produces:
  - `player_command(command: str, val: str = "", item_id=None) -> bool` — `item_id` 非 None 时附 `id=<item_id>`
  - `player_playlist_items(timeout: float = 2.0, refresh: bool = False) -> list[dict]` — `GET /requests/playlist.json`；失败返回 `[]` 且**不缓存空结果**
  - `player_goto(item_id: int) -> bool` / `player_next() -> bool` / `player_previous() -> bool` / `player_running() -> bool`
  - 模块级 `_playlist_items: list[dict]`（`_terminate_previous` 清空）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_vlc_player_command.py`：

```python
# -*- coding: utf-8 -*-
"""VLC HTTP 控制面：pl_play&id / pl_next / pl_previous / playlist.json。"""
import framework.external_player as ep


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _state(monkeypatch):
    monkeypatch.setattr(ep, "_control_state",
                        {"host": "127.0.0.1", "port": 8090, "password": "PW"})
    monkeypatch.setattr(ep, "_playlist_items", [])


def test_pl_play_sends_id_not_val(monkeypatch):
    _state(monkeypatch)
    seen = {}

    def _get(url, params=None, auth=None, timeout=None):
        seen["url"] = url
        seen["params"] = dict(params or {})
        return _Resp(200)

    monkeypatch.setattr(ep.requests, "get", _get)
    assert ep.player_command("pl_play", item_id=7) is True
    assert seen["params"] == {"command": "pl_play", "id": 7}
    assert "val" not in seen["params"]
    assert "status.xml" in seen["url"]


def test_val_still_supported(monkeypatch):
    """既有 seek/volume 走 val，签名扩展不得破坏。"""
    _state(monkeypatch)
    seen = {}
    monkeypatch.setattr(ep.requests, "get",
                        lambda url, params=None, **k: (seen.update(
                            params=dict(params or {})) or _Resp(200)))
    assert ep.player_command("seek", "+10") is True
    assert seen["params"] == {"command": "seek", "val": "+10"}


def test_previous_uses_valid_command_name(monkeypatch):
    """VLC 没有 pl_prev；上一集必须发 pl_previous。"""
    _state(monkeypatch)
    seen = []
    monkeypatch.setattr(ep.requests, "get",
                        lambda url, params=None, **k: (
                            seen.append(params["command"]) or _Resp(200)))
    assert ep.player_previous() is True
    assert seen == ["pl_previous"]


def test_playlist_items_parsed_and_cached(monkeypatch):
    _state(monkeypatch)
    payload = [{"id": 1, "name": "第01集 a", "uri": "http://x/e/k/0"},
               {"id": 2, "name": "第02集 b", "uri": "http://x/e/k/1"}]
    calls = []

    def _get(url, params=None, auth=None, timeout=None):
        calls.append(url)
        return _Resp(200, payload)

    monkeypatch.setattr(ep.requests, "get", _get)
    assert ep.player_playlist_items() == payload
    assert ep.player_playlist_items() == payload      # 命中缓存
    assert len(calls) == 1                            # 只请求了一次
    assert "playlist.json" in calls[0]
    ep.player_playlist_items(refresh=True)
    assert len(calls) == 2


def test_playlist_items_failure_returns_empty(monkeypatch):
    _state(monkeypatch)
    monkeypatch.setattr(ep.requests, "get",
                        lambda *a, **k: _Resp(500, []))
    assert ep.player_playlist_items() == []
    monkeypatch.setattr(ep.requests, "get",
                        lambda *a, **k: _Resp(200, [{"id": 3}]))
    assert ep.player_playlist_items() == [{"id": 3}]   # 空结果不缓存 → 重试


def test_command_without_control_state_returns_false(monkeypatch):
    monkeypatch.setattr(ep, "_control_state", None)
    assert ep.player_goto(1) is False
    assert ep.player_next() is False
    assert ep.player_playlist_items() == []


def test_player_running(monkeypatch):
    class _Proc:
        def __init__(self, code):
            self._code = code

        def poll(self):
            return self._code

    monkeypatch.setattr(ep, "_last_proc", None)
    assert ep.player_running() is False
    monkeypatch.setattr(ep, "_last_proc", _Proc(None))
    assert ep.player_running() is True
    monkeypatch.setattr(ep, "_last_proc", _Proc(0))
    assert ep.player_running() is False


def test_terminate_previous_clears_playlist_cache(monkeypatch):
    """换集重开：旧会话的 playlist 缓存必须清空（否则 id 映射串台）。"""
    monkeypatch.setattr(ep, "_playlist_items", [{"id": 1}])
    monkeypatch.setattr(ep, "_control_state", {"port": 1, "password": "x"})
    monkeypatch.setattr(ep, "_last_proc", None)
    ep._terminate_previous()
    assert ep._playlist_items == []
    assert ep._control_state is None
```

- [ ] **Step 2: 运行测试确认失败**

```
cd D:\code\claw; python -m pytest tests/test_vlc_player_command.py -q -p no:randomly
```
Expected: FAIL —— `AttributeError` / `TypeError: player_command() got an unexpected keyword argument 'item_id'`

- [ ] **Step 3: 最小实现**

3a. `framework/external_player.py` 的 `_control_state` 声明之后追加模块状态：

```python
# 最近一次 VLC 会话的播放列表快照（playlist.json 解析结果）。握手线程填它，
# video_view 据此建立 {集下标: vlc_id} 映射。_terminate_previous 清空：
# 旧会话的 id 对新会话无意义，留着会让 App 切集串台。
_playlist_items: list[dict] = []
```

3b. `_terminate_previous` 里的 `global` 与清空：

```python
def _terminate_previous() -> None:
    """关闭上一次拉起的播放器进程（幂等）。

    换集/重开播放器时终止旧 VLC 实例；旧进程可能已退出或句柄失效
    （用户手动关闭/播放结束），terminate 一律 try/except 包裹，不抛异常、
    不干扰本次拉起新版。同时清空 _control_state 与 _playlist_items：
    旧实例被关后其 HTTP 控制会话即失效（换集重开会重建），播放列表 id 映射
    同样对旧实例失效。
    """
    global _last_proc, _control_state, _playlist_items
    proc, _last_proc = _last_proc, None
    _control_state = None
    _playlist_items = []
    if proc is None:
        return
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001 —— 进程已退出/句柄失效：静默
        pass
```

3c. 替换文件末尾的 `player_command`，并追加控制面：

```python
def player_command(command: str, val: str = "", item_id=None) -> bool:
    """向最近一次拉起的 VLC 发送 HTTP 控制命令。

    无控制会话（浏览器 fallback / 启动失败 / 播放器已关 / requests 缺失）
    或网络失败一律静默返回 False；成功（HTTP 2xx/3xx）返回 True。GUI 键盘
    事件直接转发成 VLC 命令（pl_play / pl_pause / pl_stop / pl_next /
    pl_previous / seek +30 / volume +10…），命令名或取值由调用方给出。

    item_id 用于需要播放列表项 id 的命令（pl_play / pl_delete）——VLC 的
    pl_play 取值是**播放列表项 id**（非下标），必须先读 playlist.json 建映射。
    """
    state = _control_state
    if not state or requests is None:
        return False
    params = {"command": command}
    if val:
        params["val"] = val
    if item_id is not None:
        params["id"] = item_id
    try:
        r = requests.get(
            f"http://127.0.0.1:{state['port']}/requests/status.xml",
            params=params, auth=("", state["password"]), timeout=2,
        )
        return r.status_code < 400
    except Exception:  # noqa: BLE001 —— 网络失败/端口未起：静默 False
        return False


def player_playlist_items(timeout: float = 2.0,
                          refresh: bool = False) -> list[dict]:
    """读最近一次 VLC 会话的完整播放列表（GET /requests/playlist.json）。

    返回 [{id, name, uri, current}, ...]；无控制会话 / 网络失败 / 状态码
    >=400 / 响应非 list 一律返回 []。**失败结果不缓存**（下次调用重试）。
    refresh=True 强制重新拉（握手轮询 VLC 尚未建好列表时需要）。
    """
    global _playlist_items
    if _playlist_items and not refresh:
        return _playlist_items
    state = _control_state
    if not state or requests is None:
        return []
    try:
        r = requests.get(
            f"http://127.0.0.1:{state['port']}/requests/playlist.json",
            auth=("", state["password"]), timeout=timeout,
        )
        if r.status_code >= 400:
            return []
        items = r.json()
    except Exception:  # noqa: BLE001 —— 网络失败/未就绪/非 JSON：静默 []
        return []
    if not isinstance(items, list):
        return []
    _playlist_items = [it for it in items if isinstance(it, dict)]
    return _playlist_items


def player_goto(item_id: int) -> bool:
    """跳播播放列表项 id（VLC pl_play 的 id 是项 id，非下标）。"""
    return player_command("pl_play", item_id=item_id)


def player_next() -> bool:
    """下一项（VLC 内置 N）。"""
    return player_command("pl_next")


def player_previous() -> bool:
    """上一项（VLC 内置 P）。命令名是 pl_previous——**没有 pl_prev**。"""
    return player_command("pl_previous")


def player_running() -> bool:
    """最近拉起的播放器进程是否仍在运行（App 据此决定能否指挥 VLC 切集）。"""
    proc = _last_proc
    if proc is None:
        return False
    try:
        return proc.poll() is None
    except Exception:  # noqa: BLE001 —— 句柄失效：视为未运行
        return False
```

- [ ] **Step 4: 运行测试确认通过**

```
cd D:\code\claw; python -m pytest tests/test_vlc_player_command.py -q -p no:randomly
```
Expected: 9 passed

- [ ] **Step 5: 回归既有播放列表测试**

```
cd D:\code\claw; python -m pytest tests/test_external_player_playlist.py -q -p no:randomly
```
Expected: 8 passed（本 Task 不改 `open_with_player`，只扩控制面）

- [ ] **Step 6: 提交**

```
git add framework/external_player.py tests/test_vlc_player_command.py
git commit -m "feat(player): VLC 控制面——pl_play&id / pl_previous / playlist.json 读取"
```

---

### Task 4: external_player 系列契约（全集入列、标题、顺序 flag、classify_url、裁剪）

**Files:**
- Modify: `framework/external_player.py:15-27`（imports）、`framework/external_player.py:95-226`（`open_with_player`）
- Test: `tests/test_external_player_playlist.py`（改写既有 8 例契约断言 + 新增）

**Interfaces:**
- Consumes: 无（本 Task 自成闭环；Task 5 才接租约与握手）
- Produces:
  - `open_with_player(url, audio="", referer="", user_agent="", headers=None, ad_block=None, force_proxy=False, episodes=None, caching_ms=0, classify_url="", start_idx=-1, on_playlist_ready=None) -> str`
    —— `start_idx` 为调用方已知的当前集下标（0-based），**优先于** url 载荷
    匹配定位起始集；越界或 url 也命中不到时记 warning 退回首项。
  - `_sanitize_title(title: str, idx: int = 0) -> str`
  - `_mrl_with_title(url: str, title: str, idx: int = 0) -> str`
  - `_fit_series(episodes: list, start_idx: int, resolve, begin: int = 0) -> tuple[list[tuple[int, str]], bool]`
    —— `begin` 是 episodes 切片在整表里的起始集位，使集号兜底标题与返回下标同源。
  - `_is_local_proxy_url(target: str) -> bool`
  - 常量 `_SERIES_MAX_MRL = 300`、`_SERIES_MAX_CMD = 30000`

- [ ] **Step 1: 改写既有测试的契约断言 + 新增用例**

`tests/test_external_player_playlist.py` 里 `_FakeProc` 追加 `poll()`/`wait()`（供后续 Task 用），并把 `test_episodes_order_current_first` / `test_episodes_empty_url_skipped` / `test_force_proxy_forwarded_to_proxy` 改为新契约，另加新用例。整文件替换为：

```python
# -*- coding: utf-8 -*-
"""外部播放器：全集播放列表契约（顺序/标题/flag/分类/裁剪）+ 旧进程清理。

不真正拉起 VLC——mock _locate_vlc / proxy_url_for / subprocess.Popen，
只断言命令参数（args）组装顺序、代理覆盖与进程清理（terminate）行为。

**episodes 契约（有意破坏）**：episodes 是**全集完整有序列表**，
episodes[i] = 第 i 集（0-based），episodes[0] **不再被跳过**；
url 只用于定位起始项与分类。
"""
import framework.external_player as ep

_VLC = r"C:\Program Files\VideoLAN\VLC\vlc.exe"

_CONTROL_ARGS = [
    "--extraintf=http", "--http-host=127.0.0.1",
    "--http-port=8090", "--http-password=TESTPWD",
]


class _FakeProc:
    """模拟 Popen 返回对象：记录 args、统计 terminate 调用。"""

    def __init__(self):
        self.args = []
        self.terminated = 0
        self.waited = 0

    def terminate(self):
        self.terminated += 1

    def poll(self):
        return None

    def wait(self, timeout=None):
        self.waited += 1
        return 0


def _install(monkeypatch, proxy=None) -> list:
    """mock 播放器探测/代理/拉起；返回每次 Popen 收到的 _FakeProc 列表。"""
    if proxy is None:
        proxy = lambda u, *a, **k: "P:" + u
    procs = []

    def _popen(args, **kw):
        fp = _FakeProc()
        fp.args = list(args)
        procs.append(fp)
        return fp

    monkeypatch.setattr(ep, "_locate_vlc", lambda: _VLC)
    monkeypatch.setattr(ep, "proxy_url_for", proxy)
    monkeypatch.setattr(ep.subprocess, "Popen", _popen)
    monkeypatch.setattr(ep, "_pick_http_port", lambda: 8090)
    monkeypatch.setattr(ep.secrets, "token_hex", lambda n: "TESTPWD")
    monkeypatch.setattr(ep, "_last_proc", None)
    monkeypatch.setattr(ep, "_control_state", None)
    monkeypatch.setattr(ep, "_playlist_items", [])
    # raising=False：本任务还不实现 _start_playlist_sync（Task 5 才加），
    # 默认 raising=True 会 AttributeError 让本任务全部测试报错。
    monkeypatch.setattr(ep, "_start_playlist_sync", lambda *a, **k: None,
                        raising=False)
    return procs


def test_episodes_none_single_mrl(monkeypatch):
    """episodes=None（老调用）：只一个 MRL，不追加多余项，无系列 flag。"""
    procs = _install(monkeypatch)
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8")
    args = procs[0].args
    assert args[0] == _VLC
    assert args[1] == "--no-video-title-show"
    assert args[2] == "--no-drop-late-frames"
    assert args[3].startswith("--network-caching=")
    assert args[4:8] == _CONTROL_ARGS
    assert args[8] == "P:https://cdn.example.com/hls/a.m3u8"
    assert len(args) == 9
    assert "--no-random" not in args
    assert "--no-playlist-autostart" not in args
    assert ep._control_state == {
        "host": "127.0.0.1", "port": 8090, "password": "TESTPWD",
    }


def test_episodes_full_series_in_order_with_titles(monkeypatch):
    """全集按 0..N-1 顺序入列（**含当前集之前的集**），每条带 #第NN集 标题。

    从第 3 集开播时，第 1、2 集**仍须在列**——用户在 VLC 里可以往回选，
    App 侧切集也依赖整列的 id 映射。定位当前集是启动握手的职责。
    """
    procs = _install(monkeypatch)
    eps = [(f"https://cdn.example.com/hls/{c}.m3u8", "", f"第{i + 1}集 章节{c}")
           for i, c in enumerate("abc")]
    ep.open_with_player(eps[2][0], episodes=eps)
    args = procs[0].args
    assert args[4:8] == _CONTROL_ARGS
    assert args[8] == "--no-random"
    # 起始项是第 3 集（非首项）→ 需 no-playlist-autostart + 握手定位
    assert args[9] == "--no-playlist-autostart"
    assert args[10:] == [
        "P:https://cdn.example.com/hls/a.m3u8#第1集 章节a",
        "P:https://cdn.example.com/hls/b.m3u8#第2集 章节b",
        "P:https://cdn.example.com/hls/c.m3u8#第3集 章节c",
    ]
    # 系列路径不挂 input-slave，也不另加主 MRL
    assert not any(a.startswith(":input-slave=") for a in args)
    assert not any(a.startswith("P:") and "#" not in a for a in args)


def test_first_episode_no_autostart_flag(monkeypatch):
    """起始项 == episodes[0] → 不加 --no-playlist-autostart（走原生 autostart）。"""
    procs = _install(monkeypatch)
    eps = [("https://cdn.example.com/hls/a.m3u8", "", "第01集"),
           ("https://cdn.example.com/hls/b.m3u8", "", "第02集")]
    ep.open_with_player(eps[0][0], episodes=eps)
    args = procs[0].args
    assert "--no-playlist-autostart" not in args
    assert "--no-random" in args
    assert args[-1] == "P:https://cdn.example.com/hls/b.m3u8#第02集"


def test_episodes_empty_url_skipped(monkeypatch):
    """列表内某集缺流（URL 为空）→ 整集跳过，不影响其余集。"""
    procs = _install(monkeypatch)
    eps = [("https://cdn.example.com/hls/a.m3u8", "", "第01集"),
           ("", "", "第02集"),
           ("https://cdn.example.com/hls/c.m3u8", "", "第03集")]
    ep.open_with_player(eps[0][0], episodes=eps)
    args = procs[0].args
    assert [a for a in args if a.startswith("P:")] == [
        "P:https://cdn.example.com/hls/a.m3u8#第01集",
        "P:https://cdn.example.com/hls/c.m3u8#第03集",
    ]


def test_title_sanitized():
    assert ep._sanitize_title("第01集 标题#带井号", 0) == "第01集 标题 带井号"
    assert ep._sanitize_title("第01集\n换行\t制表", 0) == "第01集 换行 制表"
    assert ep._sanitize_title("", 4) == "第5集"          # 空 → 集号兜底
    assert ep._sanitize_title("  ", 0) == "第1集"
    assert ep._sanitize_title("123 数字开头", 0).startswith("集")
    assert not ep._sanitize_title("9abc", 0)[0].isdigit()


def test_classify_url_used_for_caching(monkeypatch):
    """classify_url 非空 → --network-caching 取自真实流地址（ikanpp 30s 保住）。"""
    procs = _install(monkeypatch)
    seen = {}

    def _classify(target):
        seen["target"] = target
        from types import SimpleNamespace
        return SimpleNamespace(kind="hls", buffer_ms=30000)

    import framework.media_tuner as mt
    monkeypatch.setattr(mt, "classify", _classify)
    eps = [("http://127.0.0.1:9/e/k/0", "", "第01集")]
    ep.open_with_player(eps[0][0], episodes=eps,
                        classify_url="https://cdn.example.com/real/x.m3u8",
                        caching_ms=30000)
    assert seen["target"] == "https://cdn.example.com/real/x.m3u8"
    assert procs[0].args[3] == "--network-caching=30000"


def test_classify_url_empty_falls_back_to_url(monkeypatch):
    """classify_url 为空 → 仍按 url 分类（老行为不变）。"""
    procs = _install(monkeypatch)
    seen = {}

    def _classify(target):
        seen["target"] = target
        from types import SimpleNamespace
        return SimpleNamespace(kind="mp4", buffer_ms=2000)

    import framework.media_tuner as mt
    monkeypatch.setattr(mt, "classify", _classify)
    ep.open_with_player("https://cdn.example.com/v/a.mp4")
    assert seen["target"] == "https://cdn.example.com/v/a.mp4"
    assert procs[0].args[3] == "--network-caching=8000"  # 走代理多一跳 → 8s 起


def test_local_proxy_episode_not_double_proxied(monkeypatch):
    """惰性 /e/ URL 已是代理 URL → 不得再包一层代理。"""
    seen = []
    procs = _install(monkeypatch, proxy=lambda u, *a, **k: (
        seen.append(u) or "P:" + u))
    eps = [("http://127.0.0.1:9/e/k/0", "", "第01集"),
           ("http://127.0.0.1:9/e/k/1", "", "第02集")]
    ep.open_with_player(eps[0][0], episodes=eps,
                        headers={"Referer": "https://x/"})
    assert seen == []          # 一层代理都没加
    assert procs[0].args[-2:] == [
        "http://127.0.0.1:9/e/k/0#第01集",
        "http://127.0.0.1:9/e/k/1#第02集",
    ]


def test_oversized_series_truncated(monkeypatch):
    """上千集不撑爆 Windows 命令行：按 _SERIES_MAX_MRL 裁剪且不抛异常。"""
    procs = _install(monkeypatch)
    eps = [(f"http://127.0.0.1:9/e/k/{i}", "", f"第{i + 1}集 很长很长很长标题")
           for i in range(1200)]
    assert len(ep.open_with_player(eps[0][0], episodes=eps)) > 0
    args = procs[0].args
    mrl_count = sum(1 for a in args if "/e/k/" in a)
    assert mrl_count <= ep._SERIES_MAX_MRL
    assert sum(len(a) for a in args) < 32000


def test_fit_series_full_series_kept_when_it_fits():
    """装得下 → 全集 0..N-1 入列，当前集之前的集**也在列内**。"""
    eps = [(f"u{i}", "", f"第{i + 1}集") for i in range(10)]
    items, truncated = ep._fit_series(eps, 4, lambda u, a: u)
    assert truncated is False
    assert [i for i, _ in items] == list(range(10))   # 全集，不是 4..9
    assert items[0][0] == 0 and len(items) == 10


def test_fit_series_degrades_to_forward_window_on_overflow():
    """装不下 → 降级为「当前集往后」的窗口；超长剧集选很靠后的集也能播。"""
    long_title = "标题很长很长很长很长很长很长很长很长很长很长很长"
    many = [(f"u{i}", "", f"第{i + 1}集 {long_title}") for i in range(400)]
    items, trunc = ep._fit_series(many, 0, lambda u, a: u)
    assert trunc is True
    assert len(items) < 400 and items[0][0] == 0
    assert len(items) <= ep._SERIES_MAX_MRL
    # 当前集在已解析范围之外 → 必须以当前集为首重新取窗口，且不丢当前集
    items2, trunc2 = ep._fit_series(many, 390, lambda u, a: u)
    assert trunc2 is True
    assert items2[0][0] == 390
    assert all(i >= 390 for i, _ in items2)


def test_fit_series_budget_caps_respected():
    """两个上限都真实生效：条数上限与总字符上限各自能触发截断。"""
    # 条数上限
    many = [(f"u{i}", "", f"第{i + 1}集") for i in range(ep._SERIES_MAX_MRL + 50)]
    items, trunc = ep._fit_series(many, 0, lambda u, a: u)
    assert trunc is True and len(items) == ep._SERIES_MAX_MRL
    # 字符上限：条数很少但每条超长
    fat = [(f"u{i}", "", "标" * 20000) for i in range(10)]
    items2, trunc2 = ep._fit_series(fat, 0, lambda u, a: u)
    assert trunc2 is True and len(items2) < 10


def test_fit_series_keeps_at_least_one_oversized_item():
    """单条就超字符上限也必须保留（当前集能播 > 命令行长）。"""
    fat = [("u0", "", "标" * (ep._SERIES_MAX_CMD + 100))]
    items, trunc = ep._fit_series(fat, 0, lambda u, a: u)
    assert len(items) == 1 and trunc is False


def test_fit_series_resolves_non_local_urls_only():
    """非本机 URL 才经 resolve 包代理；本机 /e/ URL 原样（不二次代理）。"""
    seen = []
    eps = [("http://127.0.0.1:9/e/k/0", "", "第01集"),
           ("https://cdn.example.com/b.m3u8", "", "第02集")]
    items, _ = ep._fit_series(eps, 0, lambda u, a: seen.append(u) or "P:" + u)
    assert seen == ["https://cdn.example.com/b.m3u8"]
    assert items[0][1] == "http://127.0.0.1:9/e/k/0#第01集"
    assert items[1][1] == "P:https://cdn.example.com/b.m3u8#第02集"


def test_second_call_terminates_previous(monkeypatch):
    """再次打开会先 terminate 上一次的 Popen，当前实例不受影响。"""
    procs = _install(monkeypatch)
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8",
                        episodes=[("https://cdn.example.com/hls/a.m3u8", "", "A"),
                                  ("https://cdn.example.com/hls/b.m3u8", "", "B")])
    ep.open_with_player("https://cdn.example.com/hls/b.m3u8",
                        episodes=[("https://cdn.example.com/hls/b.m3u8", "", "B")])
    assert len(procs) == 2
    assert procs[0].terminated == 1
    assert procs[1].terminated == 0


def test_terminate_race_safe(monkeypatch):
    """旧进程已自行退出 → terminate 抛异常被吞，不影响第二次拉起（幂等）。"""
    procs = _install(monkeypatch)
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8")
    procs[0].terminate = lambda *a, **k: (_ for _ in ()).throw(OSError("gone"))
    ep.open_with_player("https://cdn.example.com/hls/b.m3u8")
    assert len(procs) == 2
    assert procs[1].args[-1] == "P:https://cdn.example.com/hls/b.m3u8"


def test_terminate_clears_control_state(monkeypatch):
    """关旧播放器时清空控制会话：新开后会按新端口/密码重建。"""
    procs = _install(monkeypatch)
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8")
    assert ep._control_state is not None
    monkeypatch.setattr(ep, "_pick_http_port", lambda: 8091)
    monkeypatch.setattr(ep.secrets, "token_hex", lambda n: "NEWPWD")
    ep.open_with_player("https://cdn.example.com/hls/b.m3u8")
    assert procs[0].terminated == 1
    assert procs[1].args[7] == "--http-password=NEWPWD"
    assert ep._control_state["port"] == 8091
    assert ep._control_state["password"] == "NEWPWD"


def test_force_proxy_forwarded_to_proxy(monkeypatch):
    """force_proxy=True → 主 MRL + 音频轨各一次（系列路径 URL 不再二次代理）。"""
    seen = []
    procs = _install(monkeypatch, proxy=lambda u, *a, **k: (
        seen.append(k.get("force_proxy")) or "P:" + u))
    ep.open_with_player(
        "https://cdn.example.com/hls/a.m3u8",
        audio="https://cdn.example.com/hls/a-a.m3u8",
        headers={"Referer": "https://fake.example/"},
        episodes=[("https://cdn.example.com/hls/a.m3u8",
                   "https://cdn.example.com/hls/a-a.m3u8", "第1集")],
        force_proxy=True,
    )
    assert seen == [True, True]


def test_force_proxy_default_false(monkeypatch):
    """不传 force_proxy（默认）→ 与旧行为一致：无强制代理标志。"""
    seen = []
    procs = _install(monkeypatch, proxy=lambda u, *a, **k: (
        seen.append(k.get("force_proxy")) or "P:" + u))
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8",
                        headers={"Referer": "https://fake.example/"})
    assert seen == [False]
```

- [ ] **Step 2: 运行测试确认失败**

```
cd D:\code\claw; python -m pytest tests/test_external_player_playlist.py -q -p no:randomly
```
Expected: FAIL —— `AttributeError: module 'framework.external_player' has no attribute '_sanitize_title'`（以及 `open_with_player() got an unexpected keyword argument 'classify_url'`）

- [ ] **Step 3: 最小实现**

3a. `framework/external_player.py` 顶部 imports（`import os` 之前）追加：

```python
import re
import threading
import time
```

并在模块常量区（`_VLC_CANDIDATES` 之前）追加：

```python
# 系列播放列表的裁剪上限。Windows CreateProcess 命令行硬上限 32767 字符；
# 集数上千时一次性 enqueue 数千项既撑爆命令行也给 VLC 自身 playlist 增压。
# 正常路径仍按 1..N 全集入列（当前集在列内中段，由启动握手定位）；
# 仅当全集超这两项上限时，才降级为「当前集往后」的窗口。
_SERIES_MAX_MRL = 300
_SERIES_MAX_CMD = 30000
```

3b. 新增 4 个模块级函数（放在 `_pick_http_port` 之后、`open_with_player` 之前）：

```python
def _is_local_proxy_url(target: str) -> bool:
    """是否本机 media_proxy 的 URL（/s/<token> 或 /e/<key>/<idx>）。

    惰性系列每集 URL 本就是代理地址，**不能再包一层代理**（否则多一跳转发
    + 双重广告过滤）。VLC 只访问回环，故按前缀判定即可。
    """
    t = (target or "").lower()
    return t.startswith("http://127.0.0.1:") or t.startswith("http://localhost:")


def _sanitize_title(title: str, idx: int = 0) -> str:
    """MRL #title 片段消毒。

    - 剥掉 #（会截断 MRL）、\\r\\n\\t（破坏 argv 解析）
    - 折叠连续空白
    - 空 → 「第{idx+1}集」
    - **首字符是数字时加「集」前缀**：VLC 的 fragment 若匹配
      mrl-title=DIGIT*DIGIT 会被解释为**时间偏移**（跳到第 N 秒）而非标题
    """
    t = re.sub(r"[#\r\n\t]+", " ", str(title or ""))
    t = " ".join(t.split()).strip()
    if not t:
        t = f"第{idx + 1}集"
    if t[0].isdigit():
        t = "集" + t
    return t


def _mrl_with_title(url: str, title: str, idx: int = 0) -> str:
    """带显示标题的 MRL（URL#[title]）。"""
    return f"{url}#{_sanitize_title(title, idx)}"


def _fit_series(episodes: list, start_idx: int,
                resolve) -> tuple[list[tuple[int, str]], bool]:
    """裁剪系列列表，返回 ([(集下标, MRL), ...], 是否截断)。

    **正常路径：全集按第 1 集 → 最后一集严格顺序入列。** 当前集往往位于
    列表中段——正因如此才需要启动握手：按 MRL 匹配到当前集的 vlc_id 后
    `pl_play&id` 定位。若这里只取「当前集往后」的窗口，当前集恒为首项，
    握手就失去意义，且第 1..start_idx-1 集在 VLC 里再也选不到。

    仅当全集超出 _SERIES_MAX_MRL 条数或 _SERIES_MAX_CMD 总字符数时，才降级为
    「从当前集往后」的窗口（此时当前集恒为窗口首项，握手依然找得到）。

    resolve(url, audio) -> play_url：非本机代理 URL 才经它包一层代理
    （惰性 /e/ URL 原样使用）。**先解析再计长**——代理 URL 比原 URL 长，
    先计长会低估命令行占用。空 URL 的集整条跳过。至少保留 1 条
    （单条超长也不丢，保证「当前集能播」优先于命令行长度）。
    """
    def _mrl_of(idx: int, entry) -> str:
        ep_play = entry[0] if _is_local_proxy_url(entry[0]) else resolve(
            entry[0], entry[1] if len(entry) > 1 else "")
        return _mrl_with_title(ep_play,
                               entry[2] if len(entry) > 2 else "", idx)

    items: list[tuple[int, str]] = []
    used = 0
    truncated = False
    for idx, entry in enumerate(episodes):
        if not entry or not entry[0]:
            continue
        mrl = _mrl_of(idx, entry)
        if items and (len(items) >= _SERIES_MAX_MRL
                      or used + len(mrl) > _SERIES_MAX_CMD):
            truncated = True
            break
        items.append((idx, mrl))
        used += len(mrl)

    if truncated and start_idx > 0:
        # 全集装不下 → 降级为「当前集往后」的窗口。items 自身已按两个上限
        # 截断，故其下标 >= start_idx 的后缀必然仍满足上限，无需重新解析。
        window = [(i, m) for i, m in items if i >= start_idx]
        if window:
            return window, True
        # 当前集落在已解析范围之外（超长剧集选了很靠后的集）→ 必须重新取
        # 窗口，否则当前集根本不在列表里、无法播放。
        sub, _ = _fit_series(episodes[start_idx:], 0, resolve)
        return [(i + start_idx, m) for i, m in sub], True

    return items, truncated
```

3c. `open_with_player` 签名与文档替换（`framework/external_player.py:95-123`）：

```python
def open_with_player(url: str, audio: str = "", referer: str = "",
                     user_agent: str = "", headers: dict | None = None,
                     ad_block: dict | None = None,
                     force_proxy: bool = False,
                     episodes: list | None = None,
                     caching_ms: int = 0, classify_url: str = "",
                     start_idx: int = -1,
                     on_playlist_ready=None) -> str:
    """用外部播放器打开媒体地址。

    url      媒体直链（单流）。episodes 非空时它只用于**定位起始项**与分类，
             实际入列的 MRL 全部来自 episodes。
    audio    DASH 音频轨地址（非空时以 input-slave 挂入；系列路径忽略）
    referer / user_agent / headers  防盗链透传。referer/user_agent 是兼容旧
             调用的便捷参数；headers 提供完整头（含 Cookie 等）。任何防盗链
             头存在时走本地代理（VLC 无法设置 UA，只有代理能根治）。HLS 流
             一律走本地代理（含广告段过滤），即使无防盗链头也让代理剔除广告段。
    ad_block 源 ad_block 配置，非空时代理转发 m3u8 会剔除广告段。
    force_proxy 代理转发时强制走系统代理会话（跳过直连探测）。
    episodes **全集完整有序播放列表** list[tuple[url, audio, title]]：
             episodes[i] = 第 i 集（0-based，严格播放顺序），**episodes[0]
             不会被跳过**。每条 MRL 追加 `#<消毒后的标题>` 供播放列表显示；
             整表加 `--no-random` 保证顺序；起始项（url 命中项）非首项时加
             `--no-playlist-autostart` 并在后台握手 `pl_play&id=<id>` 定位。
             列表项 URL 为空则跳过该集。None → 与旧行为完全一致。
    classify_url 非空时用它做缓冲分类。惰性系列 URL 是
             http://127.0.0.1:PORT/e/... 分类不出 HLS，必须传**当前集真实流
             地址**，否则按连接限速的源（ikanpp 需 30000ms）缓冲退化卡顿。
    on_playlist_ready 握手线程拿到 {集下标: vlc_id} 映射后的回调（后台线程
             执行，调用方须自行跨线程）。失败/超时不调用。

    附加能力：VLC 分支启动时附带 HTTP 控制接口（--extraintf=http），GUI
    键盘事件可经 player_command() 转成 VLC 控制命令（播放/暂停/进退等）。
    """
```

3d. 分类入参替换（`framework/external_player.py:164-167`）：

```python
        try:
            from .media_tuner import classify as _classify
            # 分类对象：系列路径必须用**真实流地址**（惰性 /e/ URL 判不出类型）
            _profile = _classify(classify_url or url)
            _caching = max(_profile.buffer_ms, 5000) if _profile.kind == "hls" else _profile.buffer_ms
            # 经本地代理转发（防盗链头）多一跳、更抖，缓冲再加大抗卡顿
            if play_url != url or _is_local_proxy_url(url):
                _caching = max(_caching, 8000)
        except Exception:  # noqa: BLE001
            _caching = 5000
```

3e. `play_url` 计算替换（`framework/external_player.py:157`）：

```python
        if _is_local_proxy_url(url):
            # 惰性系列：url 已是代理 URL，不能再包一层
            play_url, audio_url = url, ""
        else:
            play_url, audio_url = _resolve(url, audio)
```

3f. MRL 组装段整体替换（`framework/external_player.py:198-208`，即 `args.append(play_url)` 到 episodes 循环结束）：

```python
        window: list[tuple[int, str]] = []
        start_idx = 0
        truncated = False
        if episodes:
            # 系列：全集按序入列（episodes[0] 也在列内），不另加主 MRL。
            # 起始项 = url 命中的集；取不到（url 不在列表内）按 0 处理。
            start_idx = next(
                (i for i, e in enumerate(episodes) if e and e[0] == url), 0)
            args.append("--no-random")
            if start_idx > 0:
                # 非首项开播：先禁止 autostart，再由握手 pl_play 定位
                args.append("--no-playlist-autostart")
            window, truncated = _fit_series(episodes, start_idx,
                                           lambda u, a: _resolve(u, a)[0])
            for _idx, mrl in window:
                args.append(mrl)
        else:
            args.append(play_url)
            if audio_url:
                args.append(f":input-slave={audio_url}")
```

3g. `Popen` 成功后的返回段替换（`framework/external_player.py:215-222`）：

```python
            _last_proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, **no_window_kwargs()  # Windows 静默，不弹控制台窗口
            )
            _control_state = {
                "host": "127.0.0.1", "port": port, "password": http_password,
            }
            if episodes and window:
                # 后台握手：轮询 playlist.json 建 {集下标: vlc_id} 映射，
                # 非首项开播时顺带 pl_play 定位。必须在后台线程（VLC 建列表
                # 需时，主线程会卡 UI）。整个 window 传下去，映射才覆盖
                # 列表内每一集（App 侧切集依赖它）。
                _start_playlist_sync(start_idx, window, on_playlist_ready)
            if truncated:
                return f"已用外部播放器打开（列表已截断，共 {len(window)} 集）"
            return "已用外部播放器打开"
```

注：`window` 是入列的 [(集下标, MRL), ...] 全体——正常路径下第 1 集到最后
一集全在列内，当前集位于中段，由握手按 uri 匹配定位；降级窗口下当前集是
`window` 首项。`episodes` 为空列表时 `window` 为空 → 不握手。

- [ ] **Step 4: 运行测试确认通过**

```
cd D:\code\claw; python -m pytest tests/test_external_player_playlist.py tests/test_vlc_player_command.py -q -p no:randomly
```
Expected: 全部 passed

- [ ] **Step 5: 回归视频视图既有测试**

```
cd D:\code\claw; python -m pytest tests/test_video_next_ep.py tests/test_hls_throughput_tuning.py -q -p no:randomly
```
Expected: 全部 passed（Task 4 未改 video_view，缓冲调优测试必须原样通过）

- [ ] **Step 6: 提交**

```
git add framework/external_player.py tests/test_external_player_playlist.py
git commit -m "feat(player): 外部播放器全集播放列表契约——顺序/标题/分类/命令行裁剪"
```

---

### Task 5: external_player 租约接线 + 后台握手 + 映射回调

**Files:**
- Modify: `framework/external_player.py:41-46`（模块状态加 `_lease_id`）、`framework/external_player.py:66-82`（`_terminate_previous`）、`framework/external_player.py:215-222`（Popen 成功后接线）、文件末尾追加握手与租约函数
- Test: `tests/test_external_player_playlist.py`（追加）

**Interfaces:**
- Consumes: Task 3 的 `player_playlist_items(refresh=True)`、`player_goto`；Task 4 的 `_start_playlist_sync` 调用点与 `on_playlist_ready` 参数；Task 1 的 `MediaProxy.acquire_lease` / `release_lease`
- Produces:
  - `open_with_player(..., on_playlist_ready=None)` 会在后台线程以 `{集下标: vlc_id}` 字典调用一次 `on_playlist_ready`
  - `_start_playlist_sync(start_idx: int, series: list[tuple[int, str]], on_playlist_ready) -> None`（起 daemon 线程）
  - `_handshake_worker(start_idx: int, series: list[tuple[int, str]], on_playlist_ready) -> None`
  - `_item_matches(item: dict, mrl: str) -> bool`
  - `_acquire_proxy_lease() -> str` / `_release_proxy_lease(lease_id: str) -> None` / `_watch_proc(proc, lease_id) -> None`
  - 模块级 `_lease_id: str`
  - 常量 `_HANDSHAKE_TIMEOUT = 3.0`、`_HANDSHAKE_INTERVAL = 0.1`

**租约成对释放（硬性验收，Task 1 审查裁决）**：`MediaProxy._leases` 无 TTL、无持有者存活检查（Task 1 计划的判据），而 `stop()` 是唯一清 `_tokens` 的地方。漏一次 release → 空闲看门狗在整个 App 会话内永久失效、回环端口一直被绑、`_tokens` 无界增长（影响面含非 VLC 播放）。因此本任务必须满足全部三条，**每条都要有回归测试**：
  1. **VLC 进程退出**（自然退出或被关）→ `_watch_proc` 观察线程释放。
  2. **播放器重启**（`_terminate_previous` 换源/重开）→ 旧租约先释放再启新租约，不得泄漏旧 id。
  3. **App 退出**（`shutdown_video()`）→ 释放后 `MediaProxy.stop()` 仍能正常关闭（持租约也不阻塞关闭）。
  另外：**必须在构造任何代理 URL 之前获取租约**（`acquire_lease()` 不保证代理已起，不能当作「代理在线且受保护」的断言）。
  回归测试（本任务 Step 1 已写全，名字以此为准）：
  - 验收 1「VLC 进程退出」→ `test_watch_proc_releases_lease_on_exit`
  - 验收 2「播放器重启」→ `test_terminate_previous_releases_old_lease_before_new`
  - 拉起即取租约 → `test_open_acquires_lease_and_watches_proc`
  - 验收 3「App 退出」→ 见 Task 6 `shutdown_video` 用例（Task 5 不引入 App 退出路径）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_external_player_playlist.py`：

```python
def test_item_matches_exact_and_fragment():
    assert ep._item_matches({"uri": "http://x/e/k/3#第4集"}, "http://x/e/k/3")
    assert ep._item_matches({"uri": "http://x/e/k/3"}, "http://x/e/k/3#第4集")
    assert not ep._item_matches({"uri": "http://x/e/k/4"}, "http://x/e/k/3")
    assert not ep._item_matches({}, "http://x/e/k/3")


def test_handshake_goto_current_episode(monkeypatch):
    """非首集开播：握手轮询到起始项后发 pl_play&id=<该集 id>。"""
    monkeypatch.setattr(ep, "_control_state",
                        {"host": "127.0.0.1", "port": 8090, "password": "PW"})
    got = []
    calls = {"n": 0}

    def _items(timeout=2.0, refresh=False):
        calls["n"] += 1
        if calls["n"] < 2:      # 第一次 VLC 还没建好列表
            return []
        return [{"id": 11, "uri": "http://x/e/k/0"},
                {"id": 12, "uri": "http://x/e/k/1"}]

    monkeypatch.setattr(ep, "player_playlist_items", _items)
    monkeypatch.setattr(ep, "player_goto", lambda i: got.append(i) or True)
    ready = {}
    series = [(0, "http://x/e/k/0#第1集"), (1, "http://x/e/k/1#第2集")]
    ep._handshake_worker(1, series, lambda m: ready.update(m))
    assert got == [12]
    assert ready == {0: 11, 1: 12}


def test_handshake_first_episode_no_goto(monkeypatch):
    """起始项就是第 0 集 → 只建映射，不发 pl_play（VLC 原生 autostart 已对）。"""
    monkeypatch.setattr(ep, "player_playlist_items",
                        lambda timeout=2.0, refresh=False: [
                            {"id": 21, "uri": "http://x/e/k/0"}])
    got = []
    monkeypatch.setattr(ep, "player_goto", lambda i: got.append(i) or True)
    ready = {}
    ep._handshake_worker(0, [(0, "http://x/e/k/0#第1集")],
                        lambda m: ready.update(m))
    assert got == []
    assert ready == {0: 21}


def test_handshake_timeout_does_not_raise(monkeypatch):
    """VLC 一直没就绪 → 超时降级（不抛、不阻塞调用方）。"""
    monkeypatch.setattr(ep, "_HANDSHAKE_TIMEOUT", 0.05)
    monkeypatch.setattr(ep, "_HANDSHAKE_INTERVAL", 0.01)
    monkeypatch.setattr(ep, "player_playlist_items",
                        lambda timeout=2.0, refresh=False: [])
    got = []
    monkeypatch.setattr(ep, "player_goto", lambda i: got.append(i) or True)
    ep._handshake_worker(1, [(1, "http://x/e/k/1")], lambda m: got.append(m))
    assert got == []


def test_open_acquires_lease_and_watches_proc(monkeypatch):
    """拉起成功后：登记代理租约 + 起等待线程；_terminate_previous 释放租约。"""
    procs = _install(monkeypatch)
    lease = {"n": 0, "released": []}
    monkeypatch.setattr(ep, "_acquire_proxy_lease",
                        lambda: lease["n"] += 1 or f"L{lease['n']}")
    monkeypatch.setattr(ep, "_release_proxy_lease",
                        lambda lid: lease["released"].append(lid))
    monkeypatch.setattr(ep, "_lease_id", "")
    ep.open_with_player("https://cdn.example.com/hls/a.m3u8")
    assert lease["n"] == 1
    assert ep._lease_id == "L1"
    ep._terminate_previous()
    assert lease["released"] == ["L1"]
    assert ep._lease_id == ""


def test_watch_proc_releases_lease_on_exit(monkeypatch):
    """VLC 自行退出（用户关窗口）→ 等待线程立即释放租约。"""
    released = []
    monkeypatch.setattr(ep, "_release_proxy_lease",
                        lambda lid: released.append(lid))
    monkeypatch.setattr(ep, "_lease_id", "LX")

    class _P:
        def wait(self, timeout=None):
            return 0

    ep._watch_proc(_P(), "LX")
    assert released == ["LX"]
    assert ep._lease_id == ""


def test_terminate_previous_releases_old_lease_before_new(monkeypatch):
    """验收 2：播放器重启必须先释放**旧**租约，且旧 id 不残留。

    顺序反了（旧租约被新租约覆盖后才释放）就会永久泄漏一个 lease id →
    空闲看门狗在整个 App 会话内失效。
    """
    released = []
    monkeypatch.setattr(ep, "_release_proxy_lease",
                        lambda lid: released.append(lid))

    class _P:
        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

    old = _P()
    monkeypatch.setattr(ep, "_last_proc", old)
    monkeypatch.setattr(ep, "_lease_id", "OLD")
    ep._terminate_previous()
    assert released == ["OLD"]
    assert ep._lease_id == ""
    assert old.terminated is True

    # 换新实例：新租约必须在旧租约已释放之后才登记
    lease = {"n": 0}
    procs = _install(monkeypatch)
    monkeypatch.setattr(ep, "_acquire_proxy_lease",
                        lambda: lease["n"] += 1 or f"NEW{lease['n']}")
    ep.open_with_player("https://cdn.example.com/hls/b.m3u8")
    assert released == ["OLD"]        # 拉新实例不释放别人的租约
    assert ep._lease_id == "NEW1"
    ep._terminate_previous()
    assert released == ["OLD", "NEW1"]
    assert ep._lease_id == ""


def test_start_playlist_sync_runs_in_background(monkeypatch):
    """_start_playlist_sync 不阻塞调用方（握手在线程里跑）。"""
    import threading as _th
    seen = {}

    def _fake(mrl, start_idx, cb):
        seen["thread"] = _th.current_thread() is not _th.main_thread()
        seen["args"] = (mrl, start_idx, cb)

    monkeypatch.setattr(ep, "_handshake_worker", _fake)
    ep._start_playlist_sync(2, [(0, "m0"), (1, "m1"), (2, "m2")], None)
    import time as _t
    for _ in range(50):
        if seen:
            break
        _t.sleep(0.02)
    assert seen["thread"] is True
    assert seen["args"] == ("m", 2, None)
```

- [ ] **Step 2: 运行测试确认失败**

```
cd D:\code\claw; python -m pytest tests/test_external_player_playlist.py -q -p no:randomly
```
Expected: FAIL —— `AttributeError: module 'framework.external_player' has no attribute '_item_matches'`

- [ ] **Step 3: 最小实现**

3a. 模块常量（`_SERIES_MAX_CMD` 之后）追加：

```python
# 启动握手：等 VLC 建好播放列表（读 playlist.json）并按 uri 匹配起始项。
# 超时/解析失败即降级为 autostart 播第 1 集——不致命。
_HANDSHAKE_TIMEOUT = 3.0
_HANDSHAKE_INTERVAL = 0.1
```

3b. `_control_state` 之后追加模块状态：

```python
# 当前 VLC 会话持有的 media_proxy 租约 id（"" = 无）。租约让空闲看门狗在
# VLC 存活期间不回收代理、不清 token（否则暂停超 10 分钟恢复即 404）。
_lease_id = ""
```

3c. `_terminate_previous` 内同时释放租约（替换 Task 3 改好的版本）：

```python
def _terminate_previous() -> None:
    """关闭上一次拉起的播放器进程（幂等）。

    换集/重开播放器时终止旧 VLC 实例；旧进程可能已退出或句柄失效
    （用户手动关闭/播放结束），terminate 一律 try/except 包裹，不抛异常、
    不干扰本次拉起新版。同时清空 _control_state 与 _playlist_items：
    旧实例被关后其 HTTP 控制会话即失效（换集重开会重建），播放列表 id 映射
    同样对旧实例失效；并释放旧实例的代理租约。
    """
    global _last_proc, _control_state, _playlist_items, _lease_id
    proc, _last_proc = _last_proc, None
    _control_state = None
    _playlist_items = []
    lid, _lease_id = _lease_id, ""
    _release_proxy_lease(lid)
    if proc is None:
        return
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001 —— 进程已退出/句柄失效：静默
        pass
```

3d. `Popen` 成功后、`_control_state` 写入之后接线（在 Task 4 的 3g 之前插入）：

```python
            global _lease_id
            _lease_id = _acquire_proxy_lease()
            threading.Thread(target=_watch_proc, args=(_last_proc, _lease_id),
                             daemon=True).start()
```

3e. 文件末尾追加握手与租约实现：

```python
# ---------------------------------------------------------------------- #
# 代理租约：VLC 存活期间阻止看门狗回收
# ---------------------------------------------------------------------- #
def _acquire_proxy_lease() -> str:
    """向 media_proxy 登记租约（代理不可用返回 ""，静默降级）。"""
    try:
        from .media_proxy import MediaProxy

        return MediaProxy.instance().acquire_lease()
    except Exception:  # noqa: BLE001 —— 代理未起/异常：租约非必需
        return ""


def _release_proxy_lease(lease_id: str) -> None:
    """释放租约（幂等；空 id 直接返回）。"""
    if not lease_id:
        return
    try:
        from .media_proxy import MediaProxy

        MediaProxy.instance().release_lease(lease_id)
    except Exception:  # noqa: BLE001
        pass


def _watch_proc(proc, lease_id: str) -> None:
    """阻塞等播放器退出，退出即释放租约（用户手动关窗口的路径）。"""
    global _lease_id
    try:
        proc.wait()
    except Exception:  # noqa: BLE001 —— 句柄失效：仍需释放
        pass
    _release_proxy_lease(lease_id)
    if _lease_id == lease_id:
        _lease_id = ""


# ---------------------------------------------------------------------- #
# 启动握手：读 playlist.json 建 {集下标: vlc_id} 映射 + 定位起始集
# ---------------------------------------------------------------------- #
def _item_matches(item: dict, mrl: str) -> bool:
    """playlist.json 的一项是否对应我们给的 MRL。

    VLC 报告的 uri 可能带 #title fragment，也可能不带 → 两种都算命中。
    """
    uri = str((item or {}).get("uri") or "")
    if not uri:
        return False
    return uri == mrl or uri.startswith(mrl + "#")


def _start_playlist_sync(start_idx: int, series: list[tuple[int, str]],
                         on_playlist_ready=None) -> None:
    """后台起线程做握手（绝不阻塞调用方——GUI 主线程会卡 UI）。"""
    threading.Thread(
        target=_handshake_worker,
        args=(start_idx, series, on_playlist_ready),
        daemon=True,
    ).start()


def _handshake_worker(start_idx: int, series: list[tuple[int, str]],
                      on_playlist_ready=None) -> None:
    """轮询 playlist.json 直到起始项出现：建映射 +（非首集）pl_play 定位。

    series 是入列的 [(集下标, MRL), ...]（全集 1..N，或降级后的当前集往后窗口）。
    映射**按 uri 匹配**得出，不做下标推算：VLC 列表顺序未必等于入列顺序，
    且下标推算只在「窗口从当前集起」时成立——全集入列时会整体错位。
    超时即降级：VLC 按 --no-playlist-autostart 行为播放，App 侧映射留空 →
    切集回落到重开 VLC。
    """
    deadline = time.monotonic() + _HANDSHAKE_TIMEOUT
    bases = [(idx, mrl.split("#", 1)[0]) for idx, mrl in (series or [])]
    while True:
        items = player_playlist_items(timeout=1.0, refresh=True)
        if items:
            mapping: dict[int, int] = {}
            start_id = None
            for item in items:
                iid = item.get("id")
                if not isinstance(iid, int):
                    continue
                for idx, base in bases:
                    if _item_matches(item, base):
                        mapping[idx] = iid
                        if idx == start_idx:
                            start_id = iid
                        break
            if on_playlist_ready is not None and mapping:
                try:
                    on_playlist_ready(mapping)
                except Exception:  # noqa: BLE001 —— 回调异常不阻断定位
                    pass
            if start_id is not None:
                if start_idx > 0:
                    player_goto(start_id)   # 失败即降级 autostart，不重试
                return                      # 首集：原生 autostart 已对
        if time.monotonic() >= deadline:
            return
        time.sleep(_HANDSHAKE_INTERVAL)
```

- [ ] **Step 4: 运行测试确认通过**

```
cd D:\code\claw; python -m pytest tests/test_external_player_playlist.py tests/test_vlc_player_command.py -q -p no:randomly
```
Expected: 全部 passed

- [ ] **Step 5: 回归代理与调优测试**

```
cd D:\code\claw; python -m pytest tests/test_media_proxy_series.py tests/test_hls_throughput_tuning.py -q -p no:randomly
```
Expected: 全部 passed

- [ ] **Step 6: 提交**

```
git add framework/external_player.py tests/test_external_player_playlist.py
git commit -m "feat(player): 代理租约接线 + 后台握手（playlist.json 映射与起始集定位）"
```

---

### Task 6: video_view 惰性系列装配 + 正在播状态回填

**Files:**
- Modify: `gui/pages/reader/video_view.py:297-301`（类级信号）、`gui/pages/reader/video_view.py:352` 附近（`__init__` 状态）、`gui/pages/reader/video_view.py:1417-1457`（`_play`）、`gui/pages/reader/video_view.py:1483-1512`（`_collect_episode_playlist` 整段替换）
- Test: `tests/test_video_series_playlist.py`（新建）

**Interfaces:**
- Consumes: Task 2 的 `MediaProxy.register_series/resolver/on_play/count/force_proxy`、`series_episode_url`、`unregister_series`；Task 4/5 的 `open_with_player(..., episodes=, classify_url=, on_playlist_ready=)`
- Produces:
  - 类级信号 `_external_now_playing = Signal(int, str, str)`（集下标, video, audio）
  - 类级信号 `_vlc_playlist_ready = Signal(object)`（`{集下标: vlc_id}`）
  - 状态 `self._series_key: str`、`self._series_urls: list[str]`、`self._vlc_item_ids: dict[int, int]`
  - `VideoView._on_external_now_playing(idx, video, audio) -> None`
  - `VideoView._on_vlc_playlist_ready(mapping: dict) -> None`
  - `VideoView._build_series_playlist(video, audio, hdrs, ad_block, force_proxy) -> list | None`
  - `VideoView._unregister_series() -> None`
  - `VideoView._build_series_playlist` 的返回值即传给 `open_with_player` 的 `episodes`（**全集严格 0..N-1 顺序**）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_video_series_playlist.py`：

```python
# -*- coding: utf-8 -*-
"""video_view 惰性系列播放列表：装配顺序、resolver 契约、状态回填、注销。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import framework.external_player as ep           # noqa: E402
import framework.media_proxy as mp               # noqa: E402


def _video_detail(n):
    from framework.content import Chapter, Detail

    return Detail(source_id="demo", content_type="video",
                  url="http://example.com/v/1", title="作品", cover="",
                  chapters=[Chapter(f"第{i + 1}集 标题{i}", f"http://e/{i}",
                                    cover="") for i in range(n)])


def _view(detail, n):
    from gui.pages.reader.video_view import VideoView

    view = VideoView(object())
    view._source = None
    view._detail = detail
    view._episodes = detail.chapters
    view._current_idx = n
    return view


class _FakeProxy:
    """最小 MediaProxy 替身：记录 register/unregister 参数。"""

    def __init__(self, port=9999):
        self.port = port
        self.registered = []
        self.unregistered = []
        self._n = 0

    def register_series(self, resolver, on_play=None, count=0,
                        force_proxy=False):
        self._n += 1
        key = f"k{self._n}"
        self.registered.append({
            "key": key, "resolver": resolver, "on_play": on_play,
            "count": count, "force_proxy": force_proxy,
        })
        return key

    def series_episode_url(self, key, idx):
        return f"http://127.0.0.1:{self.port}/e/{key}/{idx}"

    def unregister_series(self, key):
        self.unregistered.append(key)


class _FakeContent:
    def __init__(self, video="https://cdn/v.m3u8"):
        self.calls = []
        self._video = video

    def fetch_video_streams(self, source, url, quality="best", merged=False):
        self.calls.append((url, quality, merged))
        return self._video, ""


def _install_proxy(monkeypatch, proxy):
    monkeypatch.setattr(mp.MediaProxy, "instance", staticmethod(lambda: proxy))


def test_build_series_playlist_full_order(_qapp, monkeypatch):
    detail = _video_detail(5)
    view = _view(detail, 2)
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    view._source = object()
    out = view._build_series_playlist("https://cdn/cur.m3u8", "",
                                     {"Referer": "r"}, {"enabled": True}, True)
    assert out is not None
    assert len(out) == 5
    assert out[0][0] == "http://127.0.0.1:9999/e/k1/0"
    assert out[4][0] == "http://127.0.0.1:9999/e/k1/4"
    assert out[0][1] == ""                       # 系列路径不挂 input-slave
    # 源标题已含集号 → 不重复加前缀（规格 §4.4 的 f"第{i+1}集 {title}" 会
    # 产生「第1集 第1集 标题0」，此处按修正后的实现断言）
    assert out[0][2] == "第1集 标题0"
    assert view._series_key == "k1"
    assert view._series_urls == [u for u, _a, _t in out]
    # 注册参数：全集数 + force_proxy 透传
    assert proxy.registered[0]["count"] == 5
    assert proxy.registered[0]["force_proxy"] is True


def test_series_title_falls_back_to_episode_no(_qapp, monkeypatch):
    """源章节无标题 → 用「第NN集」兜底（1-based）。"""
    from framework.content import Chapter

    detail = _video_detail(0)
    detail.chapters = [Chapter("", "http://e/0", cover=""),
                       Chapter("   ", "http://e/1", cover="")]
    view = _view(detail, 0)
    _install_proxy(monkeypatch, _FakeProxy())
    view._source = object()
    out = view._build_series_playlist("v", "", {}, None, False)
    assert [t for _u, _a, t in out] == ["第1集", "第2集"]


def test_build_series_playlist_needs_two_episodes(_qapp, monkeypatch):
    _install_proxy(monkeypatch, _FakeProxy())
    view = _view(_video_detail(1), 0)
    view._source = object()
    assert view._build_series_playlist("v", "", {}, None, False) is None
    assert view._series_key == ""


def test_series_resolver_uses_merged_true(_qapp, monkeypatch):
    """resolver 复用 _FetchStreamTask 同一条调用：merged=True + 对应 ep.url。"""
    view = _view(_video_detail(3), 0)
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    view._source = "SRC"
    view._content = _FakeContent()
    view._quality = "best"
    view._build_series_playlist("v", "", {"Referer": "r"}, None, False)
    resolver = proxy.registered[0]["resolver"]
    assert resolver(1) == ("https://cdn/v.m3u8", "", {"Referer": "r"}, None)
    assert view._content.calls == [("http://e/1", "best", True)]


def test_series_resolver_raises_when_empty(_qapp, monkeypatch):
    """取流为空 → 抛异常（/e/ 回 502 且不 memo，允许重试）。"""
    view = _view(_video_detail(2), 0)
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    view._source = "SRC"
    view._content = _FakeContent(video="")
    view._build_series_playlist("v", "", {}, None, False)
    try:
        proxy.registered[0]["resolver"](0)
    except ValueError:
        pass
    else:
        raise AssertionError("空流必须抛异常")


def test_on_play_emits_signal_not_qt_from_proxy_thread(_qapp, monkeypatch):
    """on_play 只 emit 信号（代理线程执行），槽在主线程跑。"""
    view = _view(_video_detail(2), 0)
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    view._source = "SRC"
    view._content = _FakeContent()
    seen = []
    view._external_now_playing.connect(lambda i, v, a: seen.append((i, v, a)))
    view._build_series_playlist("v", "", {}, None, False)
    proxy.registered[0]["on_play"](1, "https://cdn/1.m3u8", "")
    from PySide6.QtCore import QCoreApplication
    QCoreApplication.processEvents()
    assert seen == [(1, "https://cdn/1.m3u8", "")]


def test_on_external_now_playing_updates_state(_qapp, monkeypatch):
    """槽：更新 _current_idx + 写 _stream_cache + 刷新卡片/菜单高亮。"""
    view = _view(_video_detail(4), 0)
    view._quality = "best"
    view._content = _FakeContent()
    view._current_now = None
    view._paint_card_selection = lambda: None
    view._refresh_ep_menu = lambda: None
    emitted = []
    view.episode_changed.connect(lambda a: emitted.append(a))
    view._on_external_now_playing(2, "https://cdn/2.m3u8", "")
    assert view._current_idx == 2
    assert view._stream_cache[("http://e/2", "best")] == ("https://cdn/2.m3u8", "")
    assert view._current_play == "https://cdn/2.m3u8"
    assert emitted and emitted[0][2] == "http://e/2"


def test_on_external_now_playing_ignores_bad_idx(_qapp):
    view = _view(_video_detail(2), 0)
    view._on_external_now_playing(9, "v", "")
    assert view._current_idx == 0


def test_playlist_ready_slot_writes_item_ids(_qapp, monkeypatch):
    view = _view(_video_detail(3), 0)
    seen = []
    view._vlc_playlist_ready.connect(lambda m: seen.append(m))
    view._on_vlc_playlist_ready({0: 5, 1: 6, 2: 7})
    from PySide6.QtCore import QCoreApplication
    QCoreApplication.processEvents()
    assert seen == [{0: 5, 1: 6, 2: 7}]


def test_unregister_series_idempotent(_qapp, monkeypatch):
    view = _view(_video_detail(3), 0)
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    view._series_key = "k9"
    view._series_urls = ["a", "b", "c"]
    view._vlc_item_ids = {0: 1}
    view._unregister_series()
    view._unregister_series()
    assert proxy.unregistered == ["k9"]
    assert view._series_key == "" and view._series_urls == []
    assert view._vlc_item_ids == {}


def test_rebuild_unregisters_previous_series(_qapp, monkeypatch):
    """注册新系列前先注销旧系列（注册表不泄漏）。"""
    view = _view(_video_detail(3), 0)
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    view._source = "SRC"
    view._content = _FakeContent()
    view._build_series_playlist("v", "", {}, None, False)
    view._build_series_playlist("v", "", {}, None, False)
    assert proxy.unregistered == ["k1"]
    assert view._series_key == "k2"
```

- [ ] **Step 2: 运行测试确认失败**

```
cd D:\code\claw; python -m pytest tests/test_video_series_playlist.py -q -p no:randomly
```
Expected: FAIL —— `AttributeError: 'VideoView' object has no attribute '_build_series_playlist'`

- [ ] **Step 3: 最小实现**

3a. 类级信号（`gui/pages/reader/video_view.py:301` 之后）追加：

```python
    # 外部播放器正在播第几集（由 media_proxy 的 /e/ 钩子在**代理线程** emit，
    # 自动连接 → 队列投递到主线程执行槽，绝不跨线程碰 Qt 对象）
    _external_now_playing = Signal(int, str, str)   # (集下标, video, audio)
    # 握手线程建好的 {集下标: vlc_id} 映射（同上：后台 emit，主线程消费）
    _vlc_playlist_ready = Signal(object)
```

3b. `__init__` 内 `self._external_active = False` 一行之后追加：

```python
        # 惰性系列外播：_series_key = media_proxy 注册表 key；_series_urls[i]
        # = 第 i 集惰性 URL（与传给 open_with_player 的 episodes 同序，用于
        # 与 playlist.json 的 uri 建映射）；_vlc_item_ids = {集下标: vlc_id}，
        # 有它才能 App 内切集指挥 VLC 而不重开进程
        self._series_key = ""
        self._series_urls: list[str] = []
        self._vlc_item_ids: dict[int, int] = {}
        self._external_now_playing.connect(self._on_external_now_playing)
        self._vlc_playlist_ready.connect(self._on_vlc_playlist_ready)
```

3c. 槽函数（放在 `_notify_last_episode` 之后、`_collect_episode_playlist` 之前）：

```python
    def _on_external_now_playing(self, idx: int, video: str, audio: str) -> None:
        """VLC 实际开始播第 idx 集（代理 /e/ 钩子经信号投递到主线程）。

        这是「App 侧集号」的**唯一真实来源**：用户在 VLC 内手动点另一集、
        或 App 切集成功后，真实生效的集都以这里为准（纠正乐观更新的漂移）。
        """
        if not (0 <= idx < len(self._episodes)):
            return
        self._current_idx = idx
        ep = self._episodes[idx]
        self._stream_cache[(ep.url, self._quality)] = (video, audio)
        self._current_play = video
        self._current_audio = audio
        self._current_title = ep.title or ""
        self.episode_changed.emit((self._detail, ep.title, ep.url))  # 进度记忆
        self._paint_card_selection()
        self._refresh_ep_menu()

    def _on_vlc_playlist_ready(self, mapping: dict) -> None:
        """握手线程回调 → 写 _vlc_item_ids（供 App 内切集不重开）。"""
        try:
            self._vlc_item_ids = {int(k): int(v) for k, v in (mapping or {}).items()}
        except (TypeError, ValueError):
            self._vlc_item_ids = {}

    def _unregister_series(self) -> None:
        """注销惰性系列并清映射（_series_key 为空 → 幂等无操作）。"""
        if not self._series_key:
            return
        key, self._series_key = self._series_key, ""
        self._series_urls = []
        self._vlc_item_ids = {}
        try:
            from framework.media_proxy import MediaProxy

            MediaProxy.instance().unregister_series(key)
        except Exception:  # noqa: BLE001 —— 代理不可用：本地状态已清
            pass
```

3d. `_build_series_playlist` 替换整个 `_collect_episode_playlist`（`gui/pages/reader/video_view.py:1483-1512`）：

```python
    def _build_series_playlist(self, video: str, audio: str, hdrs: dict,
                               ad_block: dict, force_proxy: bool) -> list | None:
        """注册惰性系列，返回**全集严格 0..N-1 顺序**的 episodes 列表。

        每集只给一个惰性 URL（http://127.0.0.1:PORT/e/<key>/<idx>）与标题，
        流地址**不在这里取**——VLC 真正播到该集时由 media_proxy 调 resolver
        再取（零预请求）。注册与注销成对：本次注册前先注销旧系列。

        不足 2 集 / 无 source / 代理不可用 → None（退回单集播放）。
        副作用：写 self._series_key 与 self._series_urls。
        """
        if self._source is None or not (0 <= self._current_idx < len(self._episodes)):
            return None
        if len(self._episodes) < 2:
            return None
        self._unregister_series()   # 新系列注册前先注销旧系列
        from framework.media_proxy import MediaProxy

        try:
            proxy = MediaProxy.instance()
        except Exception:  # noqa: BLE001 —— 代理起不来 → 退回单集
            return None

        def _resolve(idx: int):
            """惰性取流：与 _FetchStreamTask.run() 完全同一条调用。"""
            ep = self._episodes[idx]
            v, a = self._content.fetch_video_streams(
                self._source, ep.url, quality=self._quality, merged=True)
            if not v:
                raise ValueError(f"第{idx + 1}集取流失败")
            # a 恒为空（merged=True 合并流）：input-slave 对 DASH/fMP4 不可靠
            # 会黑屏，系列路径直接忽略（已知限制，见设计规格 §7）。
            return v, a, hdrs, ad_block

        def _on_play(idx: int, v: str, a: str) -> None:
            """代理线程执行：只 emit 信号，槽在主线程跑。"""
            self._external_now_playing.emit(idx, v, a)

        try:
            key = proxy.register_series(_resolve, _on_play,
                                        count=len(self._episodes),
                                        force_proxy=force_proxy)
            urls = [proxy.series_episode_url(key, i)
                    for i in range(len(self._episodes))]
        except Exception:  # noqa: BLE001 —— 注册/建 URL 失败 → 退回单集
            return None
        self._series_key = key
        self._series_urls = urls
        # 标题：源章节标题自带集号时**不再**加「第NN集」前缀（否则播放列表里
        # 会出现「第1集 第1集 章节名」）；源无标题才用「第NN集」兜底。
        # 非数字开头由 _sanitize_title 兜底防御。
        titles = [(ep.title or "").strip() or f"第{i + 1}集"
                  for i, ep in enumerate(self._episodes)]
        return [(urls[i], "", titles[i]) for i in range(len(urls))]
```

3e. `_play` 内的播放列表组装段替换（`gui/pages/reader/video_view.py:1440-1449`）：

```python
        # 组装全集惰性播放列表（VLC 内可任意点集、App 内切集不重开）：
        # 每集一个 /e/ 惰性 URL，流地址播到时才取；不足 2 集 → None 退回单集。
        series = self._build_series_playlist(
            video, audio, hdrs, ad_block, self._force_proxy_enabled())
        if series is not None:
            start_url = self._series_urls[self._current_idx]
            real_url = video      # 分类用真实流地址（惰性 URL 判不出 HLS）
        else:
            series, start_url, real_url = None, video, video
        msg = open_with_player(
            start_url, audio=audio,
            referer=hdrs.get("Referer", ""), user_agent=hdrs.get("User-Agent", ""),
            headers=hdrs, ad_block=ad_block, force_proxy=self._force_proxy_enabled(),
            episodes=series,
            classify_url=real_url,
            start_idx=self._current_idx,
            on_playlist_ready=self._on_vlc_playlist_ready,
            caching_ms=self._source_network_caching_ms(),
        )
```

3f. 停播时按需注销（`_stop_player` 末尾追加，`gui/pages/reader/video_view.py:1204` 之后）：

```python
        # 外部播放器还在播就别注销系列（VLC 可能继续请求 /e/，注销即 404）；
        # 播放器已退出才回收（代理侧 _SERIES_MAX=8 另有 FIFO 兜底）。
        if not self._external_vlc_running():
            self._unregister_series()
```

同时在 `_stop_player` 之前新增该判定方法：

```python
    def _external_vlc_running(self) -> bool:
        """最近拉起的外部播放器进程是否仍在运行。"""
        try:
            from framework.external_player import player_running

            return bool(player_running())
        except Exception:  # noqa: BLE001 —— 模块异常：按未运行处理
            return False
```

3g. `shutdown_video` 追加（`gui/pages/reader/video_view.py:1189` 之后）：

```python
        # App 退出：终止外部 VLC（连带释放代理租约）再注销系列
        try:
            from framework.external_player import _terminate_previous

            _terminate_previous()
        except Exception:  # noqa: BLE001
            pass
        self._external_active = False
        self._unregister_series()
```

- [ ] **Step 4: 运行测试确认通过**

```
cd D:\code\claw; python -m pytest tests/test_video_series_playlist.py -q -p no:randomly
```
Expected: 全部 passed

- [ ] **Step 5: 回归视频视图既有测试**

```
cd D:\code\claw; python -m pytest tests/test_video_next_ep.py tests/test_hls_throughput_tuning.py -q -p no:randomly
```
Expected: 全部 passed

- [ ] **Step 6: 提交**

```
git add gui/pages/reader/video_view.py tests/test_video_series_playlist.py
git commit -m "feat(video): 惰性系列播放列表装配 + VLC 正在播状态回填"
```

---

### Task 7: video_view App 内切集不重开 + `pl_previous` 修正 + 外播入口接入

**Files:**
- Modify: `gui/pages/reader/video_view.py:684-695`（`_select_episode`）、`gui/pages/reader/video_view.py:748-785`（`_handle_key`）、`gui/pages/reader/video_view.py:1736-1770`（`_open_external`）
- Test: `tests/test_video_series_playlist.py`（追加）、`tests/test_video_next_ep.py`（不改，仅回归）

**Interfaces:**
- Consumes: Task 5/6 的 `external_player.player_goto`、`VideoView._vlc_item_ids`、`_build_series_playlist`、`_series_urls`
- Produces:
  - `VideoView._try_external_goto(idx: int) -> bool` — True 表示已指挥 VLC 切集，调用方**不得**再走重开路径
  - `VideoView._select_episode` 在外播会话 + 有映射时改为切集而非重开
  - `VideoView._handle_key` 的 `P` 键发 `pl_previous`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_video_series_playlist.py`：

```python
def test_select_episode_uses_vlc_goto_when_mapped(_qapp, monkeypatch):
    """有映射 → 发 pl_play，不重开播放器（进度不丢、窗口不闪）。"""
    view = _view(_video_detail(5), 0)
    view._external_active = True
    view._vlc_item_ids = {0: 10, 1: 11, 2: 12}
    got = []
    monkeypatch.setattr(ep, "player_goto", lambda i: got.append(i) or True)
    opened = []
    monkeypatch.setattr(ep, "open_with_player",
                        lambda *a, **k: opened.append(a) or "已用外部播放器打开")
    view._load_episode = lambda idx: opened.append(("load", idx))
    view._refresh_ep_menu = lambda: None
    view._paint_card_selection = lambda: None
    view._select_episode(2)
    assert got == [12]
    assert opened == []              # 没有重开
    assert view._current_idx == 2    # 乐观更新


def test_select_episode_falls_back_when_goto_fails(_qapp, monkeypatch):
    """pl_play 失败 → 回落现有重开路径。"""
    view = _view(_video_detail(5), 0)
    view._external_active = True
    view._vlc_item_ids = {0: 10, 1: 11}
    monkeypatch.setattr(ep, "player_goto", lambda i: False)
    loaded = []
    view._load_episode = lambda idx: loaded.append(idx)
    view._refresh_ep_menu = lambda: None
    view._select_episode(1)
    assert loaded == [1]


def test_select_episode_falls_back_without_mapping(_qapp, monkeypatch):
    """无映射（握手未完成/失败）→ 回落重开。"""
    view = _view(_video_detail(5), 0)
    view._external_active = True
    view._vlc_item_ids = {}
    monkeypatch.setattr(ep, "player_goto",
                        lambda i: (_ for _ in ()).throw(AssertionError("不该发")))
    loaded = []
    view._load_episode = lambda idx: loaded.append(idx)
    view._refresh_ep_menu = lambda: None
    view._select_episode(3)
    assert loaded == [3]


def test_select_episode_normal_when_not_external(_qapp, monkeypatch):
    """非外播会话 → 行为完全不变（内嵌/单集路径不受影响）。"""
    view = _view(_video_detail(5), 0)
    view._external_active = False
    view._vlc_item_ids = {0: 10, 1: 11}
    monkeypatch.setattr(ep, "player_goto",
                        lambda i: (_ for _ in ()).throw(AssertionError("不该发")))
    loaded = []
    view._load_episode = lambda idx: loaded.append(idx)
    view._refresh_ep_menu = lambda: None
    view._select_episode(1)
    assert loaded == [1]


def test_next_prev_ep_go_through_vlc(_qapp, monkeypatch):
    """上一集/下一集在映射可用时指挥 VLC，不重开。"""
    view = _view(_video_detail(5), 1)
    view._external_active = True
    view._vlc_item_ids = {0: 10, 1: 11, 2: 12}
    got = []
    monkeypatch.setattr(ep, "player_goto", lambda i: got.append(i) or True)
    view._load_episode = lambda idx: got.append(("load", idx))
    view._refresh_ep_menu = lambda: None
    view._paint_card_selection = lambda: None
    view._on_next_ep()
    view._on_prev_ep()
    assert got == [12, 10]


def test_handle_key_prev_uses_pl_previous(_qapp, monkeypatch):
    """回归：P 键必须发 pl_previous（VLC 没有 pl_prev，此前从未生效）。"""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent

    view = _view(_video_detail(3), 0)
    view._external_active = True
    view._player = None
    sent = []
    monkeypatch.setattr(ep, "player_command",
                        lambda c, v="", item_id=None: sent.append(c) or True)
    ev = QKeyEvent(QEvent.KeyPress, Qt.Key_P, Qt.NoModifier)
    view._handle_key(ev)
    assert sent == ["pl_previous"]
    ev2 = QKeyEvent(QEvent.KeyPress, Qt.Key_N, Qt.NoModifier)
    view._handle_key(ev2)
    assert sent == ["pl_previous", "pl_next"]


def test_open_external_uses_series_playlist(_qapp, monkeypatch):
    """「⚙外部播放器」入口也走系列路径（此前完全不传 episodes）。"""
    view = _view(_video_detail(4), 1)
    view._source = "SRC"
    view._content = _FakeContent()
    view._current_play = "https://cdn/cur.m3u8"
    view._current_audio = ""
    view._current_title = "第2集"
    view._force_proxy_enabled = lambda: False
    view._source_network_caching_ms = lambda: 30000
    proxy = _FakeProxy()
    _install_proxy(monkeypatch, proxy)
    view._locate_vlc_stub = True
    import framework.external_player as _ep
    monkeypatch.setattr(_ep, "_locate_vlc", lambda: r"C:\vlc.exe")
    captured = {}

    def _open(url, **kw):
        captured["url"] = url
        captured.update(kw)
        return "已用外部播放器打开"

    monkeypatch.setattr(_ep, "open_with_player", _open)
    view._open_external()
    assert captured["episodes"] is not None
    assert len(captured["episodes"]) == 4
    assert captured["url"] == "http://127.0.0.1:9999/e/k1/1"
    assert captured["classify_url"] == "https://cdn/cur.m3u8"
    assert captured["caching_ms"] == 30000
```

- [ ] **Step 2: 运行测试确认失败**

```
cd D:\code\claw; python -m pytest tests/test_video_series_playlist.py -q -p no:randomly
```
Expected: FAIL —— 切集仍走 `_load_episode`（`opened`/`got` 不含 vlc id）；`P` 键断言拿到 `["pl_prev"]`

- [ ] **Step 3: 最小实现**

3a. `_select_episode` 替换（`gui/pages/reader/video_view.py:684-695`）：

```python
    def _select_episode(self, idx: int) -> None:
        """播放器内选集：外播会话优先指挥 VLC 切集，否则加载该集。

        选集态下点当前高亮集也视为选择 → 立即取流播放；
        播放中点同一集不重载。
        """
        if not (0 <= idx < len(self._episodes)):
            return
        if idx == self._current_idx and not self._selection_mode:
            return
        # 外播且有 vlc_id 映射 → pl_play 指挥 VLC 切集（不重开进程、进度不丢）。
        # 无映射（握手未完成/失败）或 pl_play 失败 → 落回重开路径。
        if self._try_external_goto(idx):
            self._refresh_ep_menu()
            return
        self._load_episode(idx)
        self._refresh_ep_menu()
```

3b. 新增 `_try_external_goto`（紧跟 `_select_episode` 之后）：

```python
    def _try_external_goto(self, idx: int) -> bool:
        """外播会话中指挥 VLC 切到第 idx 集（不重开进程）。

        需要 _external_active 且 idx 在 _vlc_item_ids 里（握手已建映射）。
        成功 → 乐观更新 _current_idx 并返回 True；真实生效由 /e/ 的 on_play
        钩子最终确认（用户在 VLC 内手动跳集也由它纠正）。失败/不可用 → False，
        调用方回落「重开 VLC」路径。
        """
        if not self._external_active:
            return False
        item_id = self._vlc_item_ids.get(idx)
        if item_id is None:
            return False
        try:
            from framework.external_player import player_goto

            if not player_goto(int(item_id)):
                return False
        except Exception:  # noqa: BLE001 —— 无控制会话/网络失败：回落重开
            return False
        self._current_idx = idx
        self._paint_card_selection()
        return True
```

3c. `_handle_key` 的 `P` 键修正（`gui/pages/reader/video_view.py:771-772`）：

```python
            elif key == Qt.Key_P:
                cmd = ("pl_previous", "")   # VLC 没有 pl_prev（P 从未生效过）
```

3d. `_open_external` 替换（`gui/pages/reader/video_view.py:1736-1770`）：

```python
    def _open_external(self) -> None:
        """「设置 → 外部播放器」：把当前集交外部播放器（VLC 桌面版优先）。

        Referer 保护的 CDN 直链（如 B 站 durl）：VLC 带 --http-referrer 播放
        媒体直链；VLC 不可用时回退浏览器打开集页面（页面播放绕开防盗链）。
        多集源走与自动播放相同的**全集惰性播放列表**路径。
        """
        if not self._current_play:
            return
        audio = getattr(self, "_current_audio", "")
        hdrs = {}
        _rh = getattr(self._source, "request_headers", None)
        if callable(_rh):
            hdrs = _rh() or {}
        from framework.external_player import _locate_vlc, open_with_player

        if not _locate_vlc():
            # 无 VLC：Referer 保护的媒体直链改开集页面（浏览器播放绕防盗链）
            page_url = ""
            if 0 <= self._current_idx < len(self._episodes):
                page_url = self._episodes[self._current_idx].url or ""
            elif self._detail_url_for_play:
                page_url = self._detail_url_for_play
            if page_url and self._media_needs_referer():
                from urllib.parse import urljoin

                webbrowser.open(urljoin(self._source.base_url, page_url))
                return
        ad_block = {}
        try:
            ad_block = (self._source.raw or {}).get("ad_block") or {}
        except Exception:  # noqa: BLE001
            pass
        force_proxy = self._force_proxy_enabled()
        series = self._build_series_playlist(
            self._current_play, audio, hdrs, ad_block, force_proxy)
        if series is not None:
            start_url = self._series_urls[self._current_idx]
        else:
            series, start_url = None, self._current_play
        msg = open_with_player(
            start_url, audio=audio,
            referer=hdrs.get("Referer", ""), user_agent=hdrs.get("User-Agent", ""),
            headers=hdrs, ad_block=ad_block, force_proxy=force_proxy,
            episodes=series, classify_url=self._current_play,
            start_idx=self._current_idx,
            on_playlist_ready=self._on_vlc_playlist_ready,
            caching_ms=self._source_network_caching_ms(),
        )
        self._show_status(msg)
        self._external_active = True  # 外播会话在 → App 内快捷键转发 VLC
```

- [ ] **Step 4: 运行测试确认通过**

```
cd D:\code\claw; python -m pytest tests/test_video_series_playlist.py tests/test_video_next_ep.py -q -p no:randomly
```
Expected: 全部 passed

- [ ] **Step 5: 回归全部相关测试**

```
cd D:\code\claw; python -m pytest tests/ -q -p no:randomly -k "external or vlc or proxy or series or video or playlist or hls"
```
Expected: 全部 passed

- [ ] **Step 6: 提交**

```
git add gui/pages/reader/video_view.py tests/test_video_series_playlist.py
git commit -m "feat(video): App 内切集指挥 VLC 不重开 + pl_previous 修正 + 外播入口接系列"
```

---

### Task 8: 全量验证 + 文档

**Files:**
- Modify: `SESSION.md`（追加本次收口记录）
- Test: 全量

**Interfaces:**
- Consumes: Task 1~7 全部产出
- Produces: 无代码产出；交付验收证据

- [ ] **Step 1: 跑全量测试**

```
cd D:\code\claw; python -m pytest --ignore=tests/test_comic_scroll_anchor.py --ignore=tests/test_comic_view_referer.py -q -p no:randomly
```
Expected: 0 failed（沿用既有 2 个 ignore）。基线是 `3dba2dd` 后的 697 passed。

- [ ] **Step 2: 密钥扫描**

```
cd D:\code\claw; git diff 3dba2dd..HEAD -- framework gui tests | Select-String -Pattern "api[_-]?key|token|secret|password" -CaseSensitive:$false
```
Expected: 无新增真实密钥（`--http-password` / `token_hex` 是既有本地随机口令，不是凭据）。

- [ ] **Step 3: 确认无关文件未入库**

```
cd D:\code\claw; git status --short
```
Expected: 仅 `?? sources/fanqie.json.bak-fanqie-categories`（未跟踪、未提交）。

- [ ] **Step 4: 更新 SESSION.md**

追加一节（沿用文件既有小节格式）：

```markdown
## 外部播放器全集播放列表（vlc-series-playlist，2026-09-27）

- 设计：`docs/superpowers/specs/2026-09-27-vlc-series-playlist-design.md`
- 计划：`docs/superpowers/plans/2026-09-27-vlc-series-playlist.md`
- 落地：media_proxy 惰性系列 `/e/<key>/<idx>` + 租约；external_player 全集
  入列 + `#标题` + `--no-random` + 后台握手 pl_play；video_view 切集不重开
- 契约破坏：`open_with_player(episodes=...)` 现为**全集有序列表**，
  `episodes[0]` 不再跳过，`url` 只用于定位起始项
- 已知限制：每集不单独算 network-caching（用当前集分类作全列表基线）；
  系列路径不挂 input-slave（merged 流）；全集超 300 集/30000 字符才按当前集往后截断
- 待人工验收：见规格 §8「手动 GUI 验收」7 条
```

- [ ] **Step 5: 提交**

```
git add SESSION.md
git commit -m "docs(session): 记录外部播放器全集播放列表落地与已知限制"
```

- [ ] **Step 6: 人工 GUI 验收（单元测试无法证明 VLC 行为）**

按顺序逐条实测并记录结果：

1. 打开 ≥10 集剧集 → `Ctrl+L` → 列表显示 `第01集 …` 到 `第NN集 …` 真实标题，顺序正确
2. 从第 5 集开播 → 确认从第 5 集开始播（不是第 1 集）
3. **从第 5 集开播后，打开 VLC 播放列表（`Ctrl+L`）确认第 1..4 集也在列内**——
   全集入列是「P 能往回」的前提；若列表里只剩第 5 集往后，说明 `_fit_series`
   误用了降级窗口
4. 列表里点第 30 集 → 能播；`N` → 第 31 集；`P` → 回到第 30 集
5. ikanpp 源：第 2 集及之后仍走 30s 缓冲（无新卡顿）
6. 暂停 15 分钟后恢复 → 不断流
7. App 内点「下一集」→ VLC 不重启、不闪窗、进度不丢
8. 代理日志确认每集只触发一次上游取流（memo 生效）

任一条不通过 → 回到对应 Task 修，不要在验收阶段打补丁。

