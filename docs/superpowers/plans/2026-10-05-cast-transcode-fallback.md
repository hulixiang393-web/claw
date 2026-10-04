# 投屏自动转码回退实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development 逐任务执行。

**Goal:** 原始流投屏失败时自动启动 FFmpeg H.264/AAC HLS 转码，并通过现有 LAN proxy 回退到电视。

**Architecture:** 新增 `framework/transcode_session.py` 管理 FFmpeg 进程和临时 HLS 输出；`CastSession` 暴露一次性 transcode fallback hook；`VideoView` 在原始 start 异常或 8 秒超时后启动转码，成功 PLAYING 前不停止本地 VLC。VirtualRenderer 使用相同 LAN URL 实际拉取并解码。

**Tech Stack:** Python 标准库、FFmpeg CLI、现有 MediaProxy/CastSession/VirtualRenderer、pytest、临时目录、线程。

## Global Constraints

- 原始流优先，自动回退最多一次。
- 不修改本地 VLC 默认链路。
- 不新增第三方 Python 依赖。
- 不记录完整 URL、token、请求头。
- 转码失败不得报告 PLAYING，必须保留本地播放器。
- stop/关闭/切换时必须终止 FFmpeg、清理 token 和临时目录。
- 不修改 `app_config.json`、`启动.bat`、`build.bat`。

---

### Task 1: TranscodeSession 核心

**Files:**
- Create: `framework/transcode_session.py`
- Test: `tests/test_transcode_session.py`

**Interfaces:**
```python
class TranscodeSession:
    def __init__(self, ffmpeg_path=None, temp_root=None, startup_timeout=10.0): ...
    def start(self, source_url: str, headers: dict, public_base: str) -> str: ...
    def stop(self) -> None: ...
    @property
    def playlist_path(self) -> Path | None: ...
    @property
    def diagnostics(self) -> dict: ...
```

- [ ] **Step 1：写 RED 测试**：命令包含 H.264/AAC、HLS 2 秒分片、请求头；playlist 未出现时超时；出现 playlist 后返回 URL；stop 终止进程清理目录；stderr 不含完整 URL/headers。
- [ ] **Step 2：运行 RED**：`Python310 -m pytest tests/test_transcode_session.py -q`，预期模块缺失。
- [ ] **Step 3：实现**：`tempfile.mkdtemp`；`subprocess.Popen` 独立线程；输入 headers 转成 FFmpeg `-headers`；输出 `index.m3u8` 到临时目录；轮询 playlist 和首 segment；超时终止并抛 `TranscodeError`。
- [ ] **Step 4：运行 GREEN**：目标测试通过，确认 fake process 被终止。
- [ ] **Step 5：提交**：`git add framework/transcode_session.py tests/test_transcode_session.py && git commit -m "feat(cast): add ffmpeg transcode session"`

---

### Task 2: LAN 转码输出与 VirtualRenderer 集成

**Files:**
- Modify: `framework/media_proxy.py`
- Modify: `framework/transcode_session.py`
- Test: `tests/test_transcode_integration.py`

**Interfaces:**
```python
def register_transcode_directory(self, directory: Path, public_base: str) -> str: ...
def unregister_transcode_directory(self, token: str) -> None: ...
```

- [ ] **Step 1：写 RED 测试**：LAN endpoint 可访问转码 playlist 和 TS segment；停止时 URL 失效且本地 token 不受影响。
- [ ] **Step 2：运行 RED**：`Python310 -m pytest tests/test_transcode_integration.py -q`。
- [ ] **Step 3：实现**：只允许当前 LAN endpoint 注册目录；通过专用 token/路径映射提供 playlist/segments；请求边界不允许目录穿越；复用 LAN 生命周期锁。
- [ ] **Step 4：集成 VirtualRenderer**：本地 FFmpeg 生成原始不可用输入，转码输出用 VirtualRenderer 实际 fetch/decode；只有 decode 成功才 PLAYING。
- [ ] **Step 5：回归并提交**：`tests/test_transcode_integration.py tests/test_media_proxy_lan.py tests/test_virtual_dlna_renderer.py`；commit `feat(cast): serve transcoded hls over lan proxy`。

---

### Task 3: CastSession 一次性回退 API

**Files:**
- Modify: `framework/cast_session.py`
- Test: `tests/test_cast_session.py`

**Interfaces:**
```python
def start_transcoded(self, renderer, episode_index: int, playlist_url: str, title: str) -> None: ...
def has_transcode_fallback(self) -> bool: ...
```

- [ ] **Step 1：写 RED 测试**：原始 start 失败后可启动转码 URL；PLAYING 前不触发本地 stop；同一 session 第二次 fallback 被拒绝；stop 清理 transcode session。
- [ ] **Step 2：运行 RED**。
- [ ] **Step 3：最小实现**：共享 renderer/client；转码 playlist 按 HLS MIME 设置；`_transcode_attempted` 一次性守卫；失败清理所有资源。
- [ ] **Step 4：运行 Task 1/2 + CastSession 回归**。
- [ ] **Step 5：提交**：`feat(cast): add one-shot transcode fallback to session`。

---

### Task 4: VideoView 自动回退与 UI 状态

**Files:**
- Modify: `gui/pages/reader/video_view.py`
- Test: `tests/test_video_cast.py`

**Interfaces:**
```python
def _start_cast_transcode_fallback(self, idx: int, video: str, renderer, error: BaseException) -> None: ...
def _on_transcode_started(self, playlist_url: str) -> None: ...
```

- [ ] **Step 1：写 RED 测试**：原始 start 异常/8 秒 timeout 后调用一次 fallback；转码 PLAYING 后才停止本地；原始和转码都失败时保留本地；迟到原始信号不能杀新转码会话。
- [ ] **Step 2：运行 RED**。
- [ ] **Step 3：实现**：复用后台 start worker；原始失败调用 fallback，清理原始 session 后建立转码 session；状态显示「正在转换为电视兼容格式」；禁用重复 fallback；失败提示明确。
- [ ] **Step 4：运行 GUI/Session/Proxy 回归**。
- [ ] **Step 5：提交**：`fix(gui): fallback to h264 transcoding for cast playback`。

---

### Task 5: VirtualRenderer、真实源和性能验收

**Files:**
- Modify: `tools/virtual_dlna_renderer.py`
- Create: `tests/test_transcode_format_matrix.py`
- Modify: `tests/test_cast_real_sources.py`
- Modify: `docs/superpowers/reports/2026-10-04-cast-simulation-report.md`

- [ ] **Step 1：写 RED 测试**：原始不可解码样本经转码后 VirtualRenderer 必须 PLAYING；记录首 playlist/首片段/PLAYING 延迟。
- [ ] **Step 2：离线实际测试**：生成 HEVC/WebM/DASH 或伪不兼容输入，运行原始失败→转码 HLS→实际 FFmpeg 解码；验证 MP4/HLS 原始流不触发转码。
- [ ] **Step 3：真实源测试**：显式 `CAST_REAL_SOURCES=1`，每源记录原始分类、转码分类、首片段耗时、失败原因，不记录敏感 URL。
- [ ] **Step 4：性能测试**：记录 FFmpeg 启动、首片段、PLAYING 时间，确认 stop 无孤儿进程/临时目录。
- [ ] **Step 5：最终回归**：运行 DLNA/LAN/CastSession/VideoView/VirtualRenderer/transcode 测试两次；更新报告并提交 `test(cast): validate transcode fallback end to end`。
