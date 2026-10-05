# 启动性能优化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development 逐任务执行。

**Goal:** 将大规模缓存索引维护和高成本主题 QSS 应用移出启动关键路径，缩短首次可交互时间。

**Architecture:** `ShelfCacheRepository` 增加可选的延迟索引修复入口，`MainWindow` 在窗口创建后通过后台任务执行；QSS 在事件循环空闲时应用，避免阻塞构造流程。缓存维护和主题切换语义保持不变。

**Tech Stack:** Python 标准库、PySide6、SQLite、pytest、cProfile。

## Global Constraints

- 不改变缓存数据语义和迁移结果。
- 不改变主题内容和用户切换主题行为。
- 不把网络自检、VLC warmup 改回同步启动。
- 不修改 `app_config.json`、`build.bat`、`启动.bat`。
- 后台任务失败记录日志，不阻塞主窗口。

---

### Task 1: 缓存索引延迟修复

**Files:**
- Modify: `framework/shelf_cache_repository.py`
- Modify: `gui/app.py`
- Test: `tests/test_shelf_cache_repository.py`

**Interfaces:**
```python
ShelfCacheRepository(..., repair_index: bool = True)
def repair_index_background(self, callback=None) -> None
```

- [ ] **Step 1：写 RED 测试**：构造仓库时 `repair_index=False` 不调用全量 repair；显式调用后台 repair 最终完成；失败不抛到 GUI 线程。
- [ ] **Step 2：运行 RED**：`Python310 -m pytest tests/test_shelf_cache_repository.py -q`。
- [ ] **Step 3：实现**：把 `_repair_index` 调用从构造器移到可选路径；`MainWindow` 构造使用 `repair_index=False`，窗口显示后用 `QThreadPool`/后台线程调用修复；保持 SQLite 初始化和快速状态可用。
- [ ] **Step 4：运行 GREEN**：缓存仓库与迁移回归通过。
- [ ] **Step 5：提交**：`git add framework/shelf_cache_repository.py gui/app.py tests/test_shelf_cache_repository.py && git commit -m "perf(app): defer shelf cache index repair"`。

---

### Task 2: 延迟主题应用

**Files:**
- Modify: `gui/app.py`
- Test: `tests/test_app_startup.py`

**Interfaces:**
```python
def _apply_theme_when_idle(self) -> None
```

- [ ] **Step 1：写 RED 测试**：构造窗口不立即调用高成本 `QApplication.setStyleSheet`；事件循环空闲后调用一次；显式主题切换仍立即应用。
- [ ] **Step 2：运行 RED**：`Python310 -m pytest tests/test_app_startup.py -q`。
- [ ] **Step 3：实现**：构造阶段设置轻量初始样式或不设置完整 QSS；使用 `QTimer.singleShot(0, ...)` 在显示/事件循环后应用完整 QSS；增加一次性守卫，避免重复调度。
- [ ] **Step 4：运行 GREEN**：主题相关测试和 GUI smoke 通过。
- [ ] **Step 5：提交**：`git add gui/app.py tests/test_app_startup.py && git commit -m "perf(app): defer initial theme stylesheet"`。

---

### Task 3: 启动基准与最终回归

**Files:**
- Create: `tests/test_startup_performance.py`
- Modify: `SESSION.md`
- Modify: `docs/superpowers/reports/2026-10-04-cast-simulation-report.md`

- [ ] **Step 1：写测试**：验证启动构造路径不直接执行全量缓存 repair，并输出可重复的阶段耗时。
- [ ] **Step 2：运行完整相关回归**：缓存迁移、GUI、source page、投屏和媒体回归。
- [ ] **Step 3：运行无界面 profile**：记录优化前后 `MainWindow.__init__` 与主窗口显示前耗时。
- [ ] **Step 4：运行 `compileall`、`git diff --check`，确认用户配置文件不在 stage。
- [ ] **Step 5：提交**：`git commit -m "test(app): verify startup performance regression"`。
