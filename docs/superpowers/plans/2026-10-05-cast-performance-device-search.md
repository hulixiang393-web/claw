# 投屏流畅性诊断与手动设备搜索实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development 逐任务执行。

**Goal:** 建立投屏链路性能诊断与按源调优，并将 DLNA 搜索改为用户点击后执行。

**Architecture:** `CastDeviceDialog` 默认只展示历史设备，新增显式搜索按钮触发现有 SSDP worker。MediaProxy 在不改变媒体数据的前提下记录脱敏 HLS 传输指标；`external_player`/源配置只对指定源传递投屏缓冲和可选预取参数。

**Tech Stack:** Python 标准库、PySide6、requests、现有 MediaProxy、VLC CLI、pytest。

## Global Constraints

- 打开设备窗口不自动发起 SSDP 搜索。
- 历史设备立即显示；用户点击「搜索设备」或「刷新」才扫描。
- 不修改应用端 VLC 默认播放行为。
- 不记录完整媒体 URL、token、Cookie、请求头。
- 不对所有源默认开启并发预取。
- 不修改 `app_config.json`、`build.bat`、`启动.bat`。

---

### Task 1: 手动设备搜索

**Files:**
- Modify: `gui/components/cast_dialog.py`
- Test: `tests/test_cast_dialog.py`

**Interfaces:**
```python
self.search_btn: QPushButton
```

- [ ] **Step 1：写 RED 测试**：`show()` 不调用 `discover_renderers`；历史设备可见；无历史提示点击搜索；点击 `search_btn` 启动 worker；刷新继续启动 worker。
- [ ] **Step 2：运行 RED**：`Python310 -m pytest tests/test_cast_dialog.py -q`，确认自动扫描断言失败。
- [ ] **Step 3：最小实现**：新增「搜索设备」按钮；`showEvent` 仅 `super().showEvent`；`_on_search_clicked` 调 `_start_scan`；无历史提示改为「点击搜索设备」。
- [ ] **Step 4：运行 GREEN**：`Python310 -m pytest tests/test_cast_dialog.py -q`。
- [ ] **Step 5：提交**：`git add gui/components/cast_dialog.py tests/test_cast_dialog.py && git commit -m "fix(cast): make device discovery user initiated"`。

---

### Task 2: 投屏链路性能诊断

**Files:**
- Modify: `framework/media_proxy.py`
- Test: `tests/test_media_proxy_metrics.py`
- Modify: `tools/run_cast_real_sources.py`

**Interfaces:**
```python
def transport_metrics(self) -> list[dict]: ...
def clear_transport_metrics(self) -> None: ...
```

- [ ] **Step 1：写 RED 测试**：本地 HLS 清单和分片请求产生媒体类型、状态、首字节耗时、字节数；结果不包含完整 URL/token/headers。
- [ ] **Step 2：运行 RED**：`Python310 -m pytest tests/test_media_proxy_metrics.py -q`。
- [ ] **Step 3：实现**：在 `_forward_m3u8`/`_forward_media` 的响应边界记录单请求指标；使用 `urlsplit` 只保留 path 类型或扩展名摘要；异常也记录分类；限制内存队列长度。
- [ ] **Step 4：真实源诊断**：扩展显式 `CAST_REAL_SOURCES=1` 工具输出每个源的汇总指标，仍脱敏。
- [ ] **Step 5：回归并提交**：运行媒体代理和真实源默认 skip 回归；commit `feat(cast): add redacted proxy transport metrics`。

---

### Task 3: B 站及按源投屏调优

**Files:**
- Modify: `sources/bilibili.json`
- Modify: `gui/pages/reader/video_view.py`
- Modify: `framework/external_player.py`
- Test: `tests/test_cast_source_tuning.py`

**Interfaces:**
```python
def _source_network_caching(self) -> int: ...
def _source_cast_prefetch(self) -> dict: ...
```

- [ ] **Step 1：写 RED 测试**：B 站投屏传递源级缓冲配置；普通源不被强制提高；应用端 `open_with_player` 参数保持既有值。
- [ ] **Step 2：运行 RED**。
- [ ] **Step 3：实现**：在 `sources/bilibili.json` 增加媒体投屏配置；VideoView 读取并传递 `caching_ms`/prefetch 配置；MediaProxy 只在该源显式启用时配置有界预取，默认不启用。
- [ ] **Step 4：离线验证**：使用本地 HLS 源模拟分片延迟，比较无预取和有界预取指标，不连接真实 B 站也不改应用端。
- [ ] **Step 5：提交**：`git add sources/bilibili.json gui/pages/reader/video_view.py framework/external_player.py tests/test_cast_source_tuning.py && git commit -m "perf(cast): tune selected source buffering"`。

---

### Task 4: 最终验证与报告

**Files:**
- Modify: `docs/superpowers/reports/2026-10-04-cast-simulation-report.md`
- Modify: `SESSION.md`

- [ ] **Step 1：运行**：设备对话框、代理指标、投屏源调优、CastSession、DLNA、VirtualRenderer 和转码回归。
- [ ] **Step 2：显式联网调研**：运行真实源测试，记录 B 站/其他源的分类和指标，不宣称无法复现的问题已解决。
- [ ] **Step 3：更新报告**：区分电视端代理吞吐问题、源 CDN 限速、电视解码和应用状态问题。
- [ ] **Step 4：执行 `compileall` 和 `git diff --check`，确认不包含用户配置改动。
- [ ] **Step 5：提交**：`git commit -m "docs(cast): report source streaming investigation"`。
