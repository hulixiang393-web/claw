# 虚拟 DLNA 电视与多格式投屏验证实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务执行。每个步骤使用 checkbox 跟踪。

**Goal:** 构建不依赖真实电视的虚拟 DLNA MediaRenderer，实际拉取 MediaProxy URL 并用 FFmpeg 解码，验证多种视频格式和真实源投屏链路。

**Architecture:** 测试工具层实现 `VirtualRenderer`、SOAP 控制服务、真实 HTTP 媒体抓取和 FFmpeg 解码。离线样本使用 FFmpeg 生成并由本地 HTTP server 提供；真实源测试显式启用，通过现有 MediaProxy 和 CastSession 接入虚拟电视。虚拟电视只有在成功下载并解码后才返回 `PLAYING`。

**Tech Stack:** Python 标准库、现有 `DlnaClient`/`MediaProxy`/`CastSession`、本机 FFmpeg/ffprobe、pytest、`http.server`、`threading`。

## Global Constraints

- 不引入 Python 第三方媒体库。
- FFmpeg 路径支持 `--ffmpeg` 和 PATH 查找；缺失时测试明确报告环境缺失，不伪造通过。
- 离线格式矩阵必须默认可运行，不依赖公网或真实电视。
- 真实源测试只能通过 `CAST_REAL_SOURCES=1` 显式启用，不进入默认全量测试。
- 不记录完整 token URL、请求头或敏感源信息。
- 只有 FFmpeg 实际解码成功后，虚拟设备才返回 `PLAYING`。
- 不修改 `app_config.json`、`启动.bat`、`build.bat`。
- 不将二进制媒体样本提交到仓库。

---

### Task 1: FFmpeg 工具与虚拟媒体 HTTP 服务

**Files:**
- Create: `tools/virtual_dlna_renderer.py`
- Create: `tools/generate_cast_samples.py`
- Test: `tests/test_virtual_dlna_renderer.py`

**Interfaces:**
- `find_ffmpeg(explicit: str | None = None) -> str`
- `run_ffprobe(ffmpeg_path: str, media_url: str, timeout: float = 10.0) -> dict`
- `decode_probe(ffmpeg_path: str, media_url: str, seconds: float = 3.0, timeout: float = 20.0) -> dict`
- `VirtualMediaFetcher.fetch(url: str) -> dict`
- `VirtualMediaFetcher.decode(url: str) -> dict`
- `start_media_server(root: Path) -> tuple[ThreadingHTTPServer, str]`
- `generate_sample_set(output: Path, ffmpeg_path: str) -> dict[str, Path]`

- [ ] **Step 1: 写失败测试**

覆盖：FFmpeg 路径缺失报 `FileNotFoundError`；本地 HTTP URL 返回状态码、Content-Type、首包时间和字节数；FFmpeg 成功 probe/decode 返回容器、视频编码、音频编码、时长；错误媒体返回非零结果并包含 stderr 摘要。

```python
def test_fetcher_records_http_and_decode_metadata(tmp_path):
    server, base = start_media_server(tmp_path)
    try:
        result = VirtualMediaFetcher(ffmpeg_path).decode(f"{base}/sample.mp4")
        assert result["http_status"] == 200
        assert result["decoded"] is True
        assert result["video_codec"] == "h264"
    finally:
        server.shutdown()
```

- [ ] **Step 2: 运行 RED**

Run: `C:\Users\alonely\AppData\Local\Programs\Python\Python310\python.exe -m pytest tests/test_virtual_dlna_renderer.py -q`

Expected: FAIL because the tool classes/functions do not exist.

- [ ] **Step 3: 实现最小工具**

使用 `shutil.which` 查找 FFmpeg；使用 `subprocess.run`，设置超时、捕获 stdout/stderr；使用 `ThreadingHTTPServer` 提供样本目录；禁止把完整 URL 或请求头写入日志。`generate_sample_set` 用固定色块/正弦音生成 3 秒 720p H.264/AAC MP4，随后生成 HLS TS、HLS fMP4、DASH 和 WebM；单个编码器不可用时返回 capability 记录而不是伪造文件。

- [ ] **Step 4: 运行 GREEN**

Run: `C:\Users\alonely\AppData\Local\Programs\Python\Python310\python.exe -m pytest tests/test_virtual_dlna_renderer.py -q`

Expected: 所有工具单测通过；缺失 codec 的测试使用 capability skip，并在结果中说明原因。

- [ ] **Step 5: 验证编译并提交**

Run: `C:\Users\alonely\AppData\Local\Programs\Python\Python310\python.exe -m compileall -q tools/virtual_dlna_renderer.py tools/generate_cast_samples.py tests/test_virtual_dlna_renderer.py`

Commit: `git add tools/virtual_dlna_renderer.py tools/generate_cast_samples.py tests/test_virtual_dlna_renderer.py && git commit -m "test(cast): add ffmpeg media simulation tools"`

---

### Task 2: VirtualRenderer SOAP/状态机

**Files:**
- Modify: `tools/virtual_dlna_renderer.py`
- Test: `tests/test_virtual_dlna_renderer.py`

**Interfaces:**
- `VirtualRenderer(host: str, port: int, ffmpeg_path: str, fetch_timeout: float = 10.0)`
- `VirtualRenderer.start() -> DlnaRenderer`
- `VirtualRenderer.stop() -> None`
- `VirtualRenderer.state() -> dict`
- `VirtualRenderer.set_uri(uri: str, title: str, mime_type: str) -> None`
- `VirtualRenderer.play() -> None`
- `VirtualRenderer.pause() -> None`
- `VirtualRenderer.stop_playback() -> None`
- `VirtualRenderer.seek(seconds: int) -> None`
- `VirtualRenderer.renderer -> DlnaRenderer`

- [ ] **Step 1: 写失败测试**

覆盖：SetURI 进入 `TRANSITIONING`；Play 实际抓取并 FFmpeg 解码成功才进入 `PLAYING`；解码失败进入 `ERROR`；Pause/Play 改变状态；Seek 更新位置；GetPositionInfo 提供 duration/position/URI。

```python
def test_play_reports_playing_only_after_decode(virtual_renderer, sample_url):
    virtual_renderer.set_uri(sample_url, "sample", "video/mp4")
    virtual_renderer.play()
    state = virtual_renderer.state()
    assert state["state"] == "PLAYING"
    assert state["duration"] > 0
```

- [ ] **Step 2: 运行 RED**

Run: `... -m pytest tests/test_virtual_dlna_renderer.py -k "playing or pause or seek" -q`

Expected: FAIL because the state machine/SOAP control path is not implemented.

- [ ] **Step 3: 实现状态机与控制服务**

用 `ThreadingHTTPServer` 暴露 AVTransport control URL，解析 SOAP action，调用 `VirtualRenderer` 方法并返回标准 SOAP XML。`play()` 同步执行 fetch/probe/decode；网络或解码失败保存安全错误摘要、状态为 `ERROR`，不得伪造 `PLAYING`。`seek()` 只允许已知 duration，位置限制在 `[0, duration]`。设备描述 XML 提供 `AVTransport:1` 和可选 `RenderingControl:1`。

- [ ] **Step 4: 运行 GREEN 与协议回归**

Run: `... -m pytest tests/test_virtual_dlna_renderer.py tests/test_dlna.py -q`

Expected: PASS；现有 DLNA 协议测试不回归。

- [ ] **Step 5: 提交**

Commit: `git add tools/virtual_dlna_renderer.py tests/test_virtual_dlna_renderer.py && git commit -m "test(cast): simulate DLNA renderer state and SOAP"`

---

### Task 3: 离线多格式矩阵与完整 CastSession 集成

**Files:**
- Create: `tests/test_cast_format_matrix.py`
- Create: `tests/conftest_cast_simulation.py`（仅在现有 conftest 结构允许时；否则 fixture 放测试文件）
- Modify: `tools/virtual_dlna_renderer.py`

**Interfaces:**
- `run_format_case(sample: Path, renderer: VirtualRenderer) -> dict`
- `format_matrix() -> list[dict]`
- `VirtualRenderer.play()` 通过现有 `DlnaClient` SOAP 服务可被 `CastSession` 使用。

- [ ] **Step 1: 写失败测试**

矩阵逐项记录 `format/container/video_codec/audio_codec/decoded/elapsed_ms/reason`。H.264/AAC MP4、HLS TS、HLS fMP4 为必须通过；HEVC、DASH、WebM 按 FFmpeg capability 记录通过或明确 unsupported，不得无结果跳过。

- [ ] **Step 2: 运行 RED**

Run: `tools/generate_cast_samples.py --output data/cast_samples` 后运行 `... -m pytest tests/test_cast_format_matrix.py -q`

Expected: FAIL until sample manifest、VirtualRenderer 和 CastSession integration fixture 完成。

- [ ] **Step 3: 实现矩阵 fixture**

临时样本放 `data/cast_samples`，加入 `.gitignore` 或测试结束清理。使用现有 `MediaProxy` LAN endpoint 和 `CastSession.start()`，将 VirtualRenderer 的 `DlnaRenderer` 注入 `DlnaClient` factory；断言 LAN URL 可达、虚拟电视实际返回 PLAYING、CastSession.poll_state 返回 position/duration。

- [ ] **Step 4: 验证控制与隔离**

Run: `... -m pytest tests/test_cast_format_matrix.py tests/test_cast_session.py tests/test_media_proxy_lan.py -q`

断言 Pause/Play/Seek、停止清理、失败时本地播放器回调未触发、本地 token 不被 LAN stop 删除。

- [ ] **Step 5: 提交**

Commit: `git add tests/test_cast_format_matrix.py tools/virtual_dlna_renderer.py .gitignore && git commit -m "test(cast): cover offline video format matrix"`

---

### Task 4: 真实源联网测试与诊断报告

**Files:**
- Create: `tests/test_cast_real_sources.py`
- Create: `tools/run_cast_real_sources.py`
- Create: `docs/superpowers/reports/2026-10-04-cast-simulation-report.md`

**Interfaces:**
- `discover_project_real_sources() -> list[dict]`
- `run_real_source_case(source: dict, ffmpeg_path: str) -> dict`
- `classify_failure(result: dict) -> str`

- [ ] **Step 1: 先写失败/环境测试**

默认未设置 `CAST_REAL_SOURCES=1` 时测试明确 skip；启用时对项目当前真实源执行，源解析失败记录 `source_unavailable`，不能将环境失败当代码通过。

- [ ] **Step 2: 接入项目真实取流**

复用现有 dm84/omofuna resolver/取流路径，不硬编码完整 token URL；只输出 source id、媒体类型、HTTP 状态、首包耗时、FFmpeg probe/decode 结果和分类。

- [ ] **Step 3: 实际运行联网测试**

Run: `$env:CAST_REAL_SOURCES='1'; & 'C:\Users\alonely\AppData\Local\Programs\Python\Python310\python.exe' -m pytest tests/test_cast_real_sources.py -q -s`

同时运行 `tools/run_cast_real_sources.py` 生成诊断报告，保留错误分类和耗时；不记录请求头、完整 token 或凭证。

- [ ] **Step 4: 运行受影响回归**

Run: `... -m pytest tests/test_virtual_dlna_renderer.py tests/test_cast_format_matrix.py tests/test_cast_session.py tests/test_dlna.py tests/test_media_proxy_lan.py tests/test_video_cast.py -q`

- [ ] **Step 5: 提交报告与代码**

Commit: `git add tests/test_cast_real_sources.py tools/run_cast_real_sources.py docs/superpowers/reports/2026-10-04-cast-simulation-report.md && git commit -m "test(cast): validate real sources through virtual renderer"`

---

### Task 5: 最终验证与兼容性结论

**Files:**
- Modify: `docs/superpowers/reports/2026-10-04-cast-simulation-report.md`
- Test: existing test suite

- [ ] **Step 1: 运行离线矩阵两次**

Run: `... -m pytest tests/test_virtual_dlna_renderer.py tests/test_cast_format_matrix.py -q` twice;结果必须一致。

- [ ] **Step 2: 运行真实源测试**

按 Task 4 命令运行；记录每个源是 `decoded`、`source_unavailable`、`proxy_unreachable`、`unsupported_format`、`ffmpeg_decode_error` 或 `protocol_error`。

- [ ] **Step 3: 运行完整受影响回归**

Run: `... -m pytest tests/test_dlna.py tests/test_media_proxy_lan.py tests/test_cast_session.py tests/test_cast_dialog.py tests/test_video_cast.py tests/test_cast_history.py tests/test_video_series_playlist.py -q`

- [ ] **Step 4: 检查工作区与敏感数据**

确认 `data/cast_samples` 未加入 Git，`app_config.json`、`启动.bat`、`build.bat` 保留用户现有改动，不被暂存；报告不包含完整媒体 URL、请求头或 token。

- [ ] **Step 5: 更新验收结论**

报告必须明确：

- 哪些格式实际解码成功。
- 哪些格式因 FFmpeg capability 或源限制失败。
- 虚拟电视是否只在实际解码后报告 PLAYING。
- 是否发现原始流需要转码；若需要，列为下一独立任务，不宣称当前产品已通用。
