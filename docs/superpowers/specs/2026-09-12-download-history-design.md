# 下载记录持久化与清除设计（download-history）

日期：2026-09-12
状态：用户已批准方案 A（框架层历史模块）

## 1. 背景与目标

当前 `DownloadQueue` 的下载任务**纯内存**，退出程序后全部丢失。用户需求：

1. 退出程序再打开后，**保存下载记录**——包括「下载完成」和「未下载完成」两类任务。
2. 「未下载完成」的任务恢复后要走**已写好的下载错误逻辑**（`retry_task` / `restart_task`，直接调用，不新写）。
3. 下载界面要有**清除下载记录**的入口。

已确认决策：
- 恢复规则：`DONE` 原样保留；其余状态（含用户主动取消的 `CANCELED`）**统一置 `FAILED`**，错误提示「程序退出时任务未完成」。
- 清除入口：提供**「清除已完成」**与**「清除记录」（全清）**两个按钮；全清弹确认框防误删，清除已完成直接执行。
- 清除记录**不影响**本地文件（删除本地文件走现有「删除本地文件」按钮）。

## 2. 架构

新增独立模块 `framework/download_history.py`（框架层，不依赖 GUI），负责：

- `DownloadTask` ↔ dict 的序列化/反序列化
- `data/downloads.json` 的文件读写
- 恢复时的状态归一化

`DownloadQueue` 只做接线：

- 构造时接收 `history_path`（App 传 `data/downloads.json`），启动时调用 `history.load()` 恢复任务
- 关键变更点调用 `history.save(self._tasks)`

数据文件结构：

```json
{
  "version": 1,
  "tasks": [ { "...": "序列化后的任务" } ]
}
```

## 3. DownloadTask 序列化白名单

只序列化「展示 + 恢复」所需字段，**排除运行时/大型字段**：

保存：
- `task_id` `source_id` `content_type` `url` `title`
- `chapters` → `[{title, url, source_id, cover}]`（`Chapter` 四字段，JSON 安全）
- `selected` `quality` `total` `status` `error`
- `out_dir` `epub_ok` `has_ads` `start_time` `end_time` `parallel`

**不保存**（恢复时重新初始化）：
- `worker`（线程/QRunnable 引用）
- `cancel_evt` / `pause_evt`（threading.Event 不可 JSON）
- `active_titles` `dispatched` `merge_progress`
- `epub_chapters`（novel 全文 / comic 图片字节，体积过大，绝不入 JSON）
- `done` `bytes_written` `done_chapters` `failed` `failed_idx`（下载进度记忆——恢复后不保留，见 §4）

反序列化时补齐运行时缺省：`cancel_evt`/`pause_evt` 新建 `threading.Event`，`worker=None`，`active_titles=[]`，`epub_chapters=[]`，`done=0`，`bytes_written=0`，`done_chapters=[]`，`failed=[]`，`failed_idx=[]`。

## 4. 恢复规则

`DownloadHistory.load()` 返回任务列表，逐条：

- `status == DONE`：**原样保留**（进度信息完整；`out_dir`/`epub_ok` 用于「打开文件夹 / 打开阅读」）。
- 其他状态（`WAITING`/`DOWNLOADING`/`PAUSED`/`CANCELED`/`FAILED`）→ 统一置 **`FAILED`**，`error = "程序退出时任务未完成"`，不保留任何下载进度记忆。

恢复的任务**不自动派发**（保持界面静止，待用户操作）。

**未完成任务重试语义**：恢复后 `failed_idx`/`done_chapters` 均为空 → 用户点「重试」走 `retry_task` 的「整体失败」分支（failed_idx 为空 → 保持原勾选、`_download_all` 从头续下全部选中章节，`MAX_ATTEMPTS` 重试机制不变）——即恢复任务 = 安全整本重下，复用现有逻辑，零新代码。

## 5. 保存时机

`DownloadQueue` 在以下时机调用 `history.save(self._tasks)`：

1. `add_task` 成功后
2. `_run_task` 的 `finally`（终态落定后）
3. `cancel_task` 之后
4. `remove_done` 之后
5. 新增两个清除方法之后（见 §6）
6. `retry_task` / `restart_task` 重置状态后（同步一致性，任务从 FAILED 回到 WAITING）

保存为同步 JSON 写（任务量少）。文件损坏/缺失 → 静默返回空，不抛异常（与 `cf_cookies.json`、`library.json` 行为一致）。

## 6. 清除按钮

`DownloadQueue` 新增两个方法：

- `clear_done()`：移除所有 `status == DONE` 任务 + save
- `clear_all()`：移除全部任务 + save

`DownloadPage._build_ui` 工具栏（在「全取消」按钮与 `addStretch` 之间）新增两个 `QPushButton`：

- 「清除已完成」→ `self._queue.clear_done()` → `self._full_rebuild()`
- 「清除记录」→ `QMessageBox.question` 确认「确定清空所有下载记录吗？(不影响已下载文件)」→ Yes 才 `self._queue.clear_all()` → `self._full_rebuild()`

清空后下载页回到空态（现有 `stack` 空态逻辑自然生效，因为 `tasks()` 为空）。

## 7. App 接线

`gui/app.py` 创建 `DownloadQueue` 处（`download_queue.py` 构造点）传入：

```python
history_path=base_dir / "data" / "downloads.json"
```

`DownloadQueue.__init__` 增加 `history_path: Optional[Path] = None`；非 None 时加载并恢复任务到 `self._tasks`。

## 8. 测试

新增 `tests/test_download_history.py`：

1. **round-trip**：构建含 DONE / WAITING / PAUSED / CANCELED / FAILED 的任务集 → save → 新 queue load → DONE 原样、其余全 FAILED 且 `error` 为「程序退出时任务未完成」。
2. **运行时字段排除**：save 落盘 JSON 不含 `worker`/`cancel_evt`/`pause_evt`/`epub_chapters`/`done_chapters`。
3. **恢复后不自动派发**：load 出的任务 status 均 FAILED/DONE，无 DOWNLOADING/WAITING。
4. **恢复任务重试安全**：对恢复出的 FAILED 任务调 `retry_task` → status 变 WAITING、`done`/`done_chapters` 从头开始（等价整本重下）。
5. **clear_done**：只移除 DONE，保留 FAILED。
6. **clear_all**：全部移除。
7. **损坏/缺失文件**：load 返回空、不抛异常。
8. **GUI（offscreen）**：工具栏存在「清除已完成」「清除记录」按钮；点击触发 queue 方法；全清弹确认框。

## 9. 边界与特殊情况

- `downloads.json` 与 shelf_meta/library 等 JSON 存储并存的独立性：互不影响。
- 恢复任务 `task_id`：沿用保存的原 id（`_seq` 持续推进，`_seq` 初始化从 max(现有任务 id 编号)+1 起，避免冲突）。
- comic/novel 半途任务恢复后重试 = 整本重下（§4），用户视角是「重试重新下载」，不静默丢已下内容之外的东西（文件系统无中间产物，只有最终 epub，安全）。
- 退出时正在下载的任务：退出瞬间未做主动保存（进程终止），其上一次保存状态（WAITING/DOWNLOADING…）恢复为 FAILED——符合「未完成 = 失败」语义，无需退出钩子。