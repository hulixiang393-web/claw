# dm84 与 omofuna 视频源实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 为 dm84.tv 与 omofuna.com 增加符合项目 video Schema 的可播放视频源，并验证分类、详情、分集和 HLS 播放链路。

**Architecture:** 复用现有 SourceConfig、Parser、Content、HLS media proxy 与外部播放器。dm84 使用详情页分集链接和播放页 iframe 参数，再由 hhjx 播放接口解析动态 HLS；omofuna 使用 MacCMS 播放页 `player_aaaa`，对 URL 编码值解码后直接交给 HLS 播放链路。源级配置只保存稳定模板、选择器、请求头和线路规则，不缓存时效签名地址。

**Tech Stack:** Python、JSON source schema v2、pytest、requests/现有 HttpClient、Playwright 真实验证。

## Global Constraints

- 不硬编码真实 HLS 签名 URL、token 或当前日期路径。
- 保留项目现有 `$type: "video"`、metadata tags、media hls、ad_block 与 constraints 约定。
- 每个站点至少覆盖分类、搜索、详情、分集和播放地址解析测试。
- 真实验证只验证公开页面可访问、媒体清单可请求，不绕过登录、验证码或访问控制。

---

### Task 1: 添加离线解析回归测试

**Files:**
- Create: `tests/test_dm84_source.py`
- Create: `tests/test_omofuna_source.py`
- Create: `tests/fixtures/dm84/detail.html`
- Create: `tests/fixtures/dm84/play.html`
- Create: `tests/fixtures/omofuna/detail.html`
- Create: `tests/fixtures/omofuna/play.html`

**Interfaces:**
- Tests consume existing `SourceManager`/`Content` parsing entry points and fixture HTML.
- Tests produce stable expected fields for later source configuration.

- [ ] Capture representative public HTML from one detail page and one play page per site.
- [ ] Assert dm84 categories include `/list-1.html` through `/list-4.html` and type filters.
- [ ] Assert dm84 detail extracts title, metadata, episode URL `/p/{id}-{sid}-{ep}.html`, and iframe `url` token.
- [ ] Assert omofuna detail extracts title, tags, category, four play channels, and episode URL `/anime/{hash}/play/{sid}/{ep}.html`.
- [ ] Assert omofuna play fixture extracts `player_aaaa.url`, URL-decodes it, and returns an `.m3u8` URL.
- [ ] Run `pytest tests/test_dm84_source.py tests/test_omofuna_source.py -q`; tests must fail before source/config implementation.

### Task 2: Implement dm84 source configuration and parser support

**Files:**
- Create: `sources/dm84.json`
- Modify: `framework/content.py` only if the existing episode parser cannot consume iframe parameter resolution.
- Test: `tests/test_dm84_source.py`

**Interfaces:**
- Source id: `dm84`.
- Stable episode input: `/p/{video_id}-{sid}-{episode}.html`.
- Output: one episode title/url plus resolved playable HLS URL.

- [ ] Configure list categories, type/tag URLs, list selectors, detail selectors, and episode selectors.
- [ ] Configure dm84 iframe extraction without storing current signed CDN URLs.
- [ ] Add the smallest reusable resolution branch for `hhjx.hhplayer.com/api/parse` only if schema configuration cannot express it.
- [ ] Preserve `Referer`, User-Agent, timeout, retry, and interval rules.
- [ ] Add HLS media settings and ad-block configuration consistent with existing video sources.
- [ ] Run the focused dm84 tests until green.

### Task 3: Implement omofuna source configuration and URL decoding

**Files:**
- Create: `sources/omofuna.json`
- Modify: `framework/content.py` or `framework/decrypter.py` only if existing `maccms_url` handling cannot decode `player_aaaa.url`.
- Test: `tests/test_omofuna_source.py`

**Interfaces:**
- Source id: `omofuna`.
- Stable detail input: `/anime/{hash}.html`.
- Stable episode input: `/anime/{hash}/play/{sid}/{ep}.html`.
- Output: decoded HLS URL, with source/channel metadata preserved.

- [ ] Configure six site categories and complete detail metadata selectors.
- [ ] Configure play list extraction, four channel IDs, episode numbering, and title fields.
- [ ] Configure `player_aaaa` JSON extraction and URL percent-decoding.
- [ ] Preserve channel-specific Referer and avoid caching time-sensitive media URLs.
- [ ] Run focused omofuna tests until green.

### Task 4: Real-site discovery and playback verification

**Files:**
- Modify only source JSON or narrowly scoped parser code when real evidence requires it.
- Test: both focused test files plus temporary probe outside repository.

- [ ] Probe dm84 home, all four categories, one detail page, three episodes, iframe parse endpoint, and final HLS response.
- [ ] Probe omofuna home, all six categories, one detail page, three episodes across at least two channels, and final HLS response.
- [ ] Verify HLS playlist status, content type, segment URL resolution, and required Referer/Origin behavior.
- [ ] Record any unavailable categories or unstable channels instead of claiming universal playback.
- [ ] Run focused tests after every evidence-based change.

### Task 5: Quality gates and final review

**Files:**
- Review all changed source, parser, and test files.

- [ ] Run `python -m compileall framework tests`.
- [ ] Run `pytest tests/test_dm84_source.py tests/test_omofuna_source.py -q`.
- [ ] Run related video regression tests: `pytest tests/test_video_cards.py tests/test_video_single_chapter.py tests/test_video_series_playlist.py -q`.
- [ ] Run repository lint/typecheck commands if project configuration provides them.
- [ ] Inspect `git diff --stat`, `git diff`, and `git status --short`; ensure only intended files changed.
