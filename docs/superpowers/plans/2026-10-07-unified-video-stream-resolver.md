# 统一视频取流与播放器兜底实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 统一视频流候选、超时、缓存和线路兜底，修复动漫巴士、Omofun、提线、饭搭子、XDM 取流慢/失败及 YouTube VLC 本地代理 403。

**Architecture:** 在 `Content.fetch_video_streams` 与播放入口之间增加轻量 resolver，复用现有 HTML、source_switch、Playwright、yt-dlp 逻辑；流候选按预算快速尝试并缓存。播放器代理判定仅对 Referer/Cookie/强制代理生效，YouTube 签名直链直连。

**Tech Stack:** Python 3.10、PySide6、Playwright、curl_cffi、yt-dlp、VLC、pytest。

## Global Constraints

- 不绕过验证码、WAF、登录或防盗链机制。
- 不记录完整签名 URL、Cookie 或敏感参数。
- 普通视频源行为保持兼容，源级配置优先于全局默认。
- 失败结果不缓存，成功签名 URL 只短期缓存并按源/集/画质隔离。
- 不修改用户配置文件，不提交 commit。

---

### Task 1: 统一流候选与预算

**Files:**
- Create: `framework/stream_resolver.py`
- Modify: `framework/content.py:2734-2778, 3068-3135`
- Test: `tests/test_stream_resolver.py`

**Interfaces:**
- `StreamResolver.resolve(source, episode_url, quality="", merged=True) -> tuple[str, str]`
- `StreamResolver` 接受现有 `Content` 实例，通过回调调用既有解析方法。
- `Content.fetch_video_streams` 保持现有签名，内部委托 resolver。

- [ ] 写失败测试：直链候选优先、候选去重、成功缓存、失败不缓存、总预算到期停止后续候选、同一 key 并发请求合并。
- [ ] 运行 `python -m pytest tests/test_stream_resolver.py -q` 确认 RED。
- [ ] 实现 resolver：使用 `time.monotonic()` 截止时间；默认总预算 30 秒、单候选 10 秒、最大候选 6 个；任何阶段成功立即返回；异常只保留脱敏阶段信息。
- [ ] 在 Content 中接入 resolver，保留 `fetch_video_episode` 兼容入口，避免下载器/投屏调用断裂。
- [ ] 运行定向测试和现有 `tests/test_video_single_chapter.py`、`tests/test_video_next_ep.py`。

---

### Task 2: source_switch 和多解析候选兜底

**Files:**
- Modify: `framework/content.py:3068-3235`
- Modify: `sources/omofuna.json`, `sources/trtag.json`, `sources/fdzys.json`, `sources/xuandm.json`
- Test: `tests/test_omofuna_source.py`, `tests/test_kanav.py`, 新增 `tests/test_video_stream_fallback.py`

**Interfaces:**
- `_fetch_play_url_auto_switch` 返回首个可用播放 URL，线路重复 URL 去重。
- 新增候选提取 helper，支持 `player_aaaa`、`var now`、`video/source`、转义/未转义 JSON m3u8。

- [ ] 写失败测试：第一条线路无地址时尝试第二条；Omofun/提线/F饭搭子/XDM fixture 中一种规则失败时命中另一候选；相对 URL 正确绝对化。
- [ ] 运行目标测试确认 RED。
- [ ] 最小实现候选收集与去重；每条线路使用 resolver 剩余预算，不继续使用默认重试叠加；不删除现有 source_switch 配置。
- [ ] 为目标源补精确 fallback selector/regex 和 `stream_budget`，只加入真实 fixture 已证明的规则。
- [ ] 运行目标测试确认 GREEN。

---

### Task 3: Playwright 快速路径和动漫巴士策略

**Files:**
- Modify: `framework/playwright_helper.py:1703-1792`
- Modify: `framework/content.py:2839-2855`
- Modify: `sources/dm84.json`
- Test: `tests/test_rendered_video_fetch.py`, `tests/test_dm84_source.py`

**Interfaces:**
- `fetch_rendered_video_sync(..., slow_fallback_rounds=3) -> str` 保持默认兼容。

- [ ] 增加失败测试：源传 `slow_fallback_rounds=0` 时不执行 3×4 秒慢站等待；媒体请求或 video/source 出现立即返回。
- [ ] 运行测试确认 RED。
- [ ] 实现源级预算传递；动漫巴士保持 500ms 首轮、0 次慢回退，其他源保持默认慢播放器兼容。
- [ ] 运行 dm84 与 Playwright 定向测试。

---

### Task 4: YouTube 直链与 MediaProxy 判定

**Files:**
- Modify: `framework/external_player.py:312-331`
- Modify: `framework/media_proxy.py`（必要时仅补 URL 判定 helper）
- Modify: `sources/youtube.json`（仅必要的播放策略字段）
- Test: `tests/test_external_player_playlist.py`, 新增 `tests/test_youtube_direct_stream.py`

**Interfaces:**
- `_resolve(target, src_audio)` 对只有通用 User-Agent 的直链返回原 URL。
- 需要 Referer/Cookie 或 `force_proxy=True` 的源仍返回 `/s/<token>`。

- [ ] 写失败测试：googlevideo 签名 URL + User-Agent 不进入本地代理；Referer/Cookie 仍进入代理；HLS 广告过滤源不改变。
- [ ] 运行测试确认 RED。
- [ ] 修改代理判定：通用 UA 不作为强制代理条件；保留防盗链头和强制代理逻辑。
- [ ] 运行 YouTube/VLC/MediaProxy 相关测试，确认 VLC MRL 不再默认是失败的本地 `/s/` 地址。

---

### Task 5: 缓存、错误诊断与播放器集成回归

**Files:**
- Modify: `gui/pages/reader/video_view.py:117-145, 580-604`
- Modify: `framework/content.py`
- Test: `tests/test_video_stream_fallback.py`, `tests/test_video_series_playlist.py`, `tests/test_video_cast.py`

- [ ] 写失败测试：取流任务复用 resolver 缓存；旧播放代次不会消费新流；总预算错误显示简短可读信息。
- [ ] 实现成功流短 TTL 缓存、换源/换作品/画质变化失效，同集并发合并，错误日志脱敏。
- [ ] 确认 VLC 单集、系列播放、投屏、下载仍使用兼容的 `(video, audio)` 返回结构。
- [ ] 运行视频集成回归。

---

### Task 6: 全量验证

**Files:**
- No source changes expected.

- [ ] 运行目标源测试、resolver、Playwright、MediaProxy、VLC、视频 smoke。
- [ ] 运行 `python -m compileall -q framework gui tests`。
- [ ] 运行 `git diff --check`。
- [ ] 检查 `git status --short`，确认不覆盖用户文件。
