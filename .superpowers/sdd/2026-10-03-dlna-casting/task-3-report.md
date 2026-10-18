# Task 3 Report

status: PASS
commit: 1e0ebff（fix(cast): use public proxy API and session-owned title in rollback）

## 测试结果

- `python -m pytest tests/test_cast_session.py -q`：16 passed（复审轮 15 + N-2 复现测试 1）
- `python -m pytest tests/test_media_proxy_lan.py -q`：24 passed（复审轮 22 + 真实 release_token 单测 + N-1 集成测试）
- `python -m pytest tests/test_cast_session.py tests/test_media_proxy_lan.py tests/test_media_proxy_ad.py tests/test_media_proxy_force.py tests/test_media_proxy_series.py tests/test_media_proxy_stream_tuning.py tests/test_dlna.py -q`：120 passed
- `python -m compileall -q framework/cast_session.py framework/media_proxy.py tests/test_cast_session.py tests/test_media_proxy_lan.py`：通过
- `git diff --check`：通过

## 复审（task-3-re-review.md）关闭情况

- **N-1（Important，已关闭）**：`MediaProxy` 新增公开只读接口 `lan_endpoint_error()`（持 `_lan_lock` 读取、不清除）；`CastSession.stop()` 改经该 API 感知 `stop_lan_endpoint()` 的静默失败，不再读写 `proxy._lan_error`，Task 2 的 failed-lifecycle 守卫保持完整。新增测试：
  - 单测 `test_stop_propagates_lan_endpoint_error` 断言 `stop()` 抛错后 `proxy._lan_error` 未被清空（修复前失败复现）；
  - 集成测试 `test_cast_session_stop_failure_keeps_lan_guard`（真实 MediaProxy）：LAN 停止失败 → `stop()` 抛错 → 该状态下 `build_url(public_base=...)` 与 `start_lan_endpoint()` 仍被拒绝（修复前失败复现）。
- **N-2（Important，已关闭）**：会话自持 `_current_title`（`start()`/`set_episode()` 成功时写入、`_clear_state()` 清空），恢复旧 URI 使用该状态；删除 `_episode_title_from_calls`。新增测试 `test_set_episode_recovery_uses_session_owned_title` 用无 `calls` 属性的类真实 client 锁住恢复路径（修复前标题退化 `Unknown` 失败复现）。
- **N-3（已关闭）**：`test_set_episode_uri_success_play_failure_full_recovery` 更名为 `test_set_episode_recovery_failure_wraps_error`，删除死代码，`match` 精确匹配包装消息，补充 `_active`/旧 token 保留/旧标题断言。
- **N-4（已关闭）**：统一顺序测试补 `len(events)` 断言。
- **N-5（已关闭）**：删除死字段 `_lan_endpoint_error`。
- **N-6（部分关闭）**：`_lock` 升级为 `RLock` 并覆盖 `start`/`stop`/`set_episode` 全程、`play`/`pause`/`seek` 与 `poll_state` 的状态检查；`poll_state` 改为「锁内快照 client → 锁外网络调用 → 锁内检查并标记 local_player_stopped」；类 docstring 固化并发契约（回调在持锁状态于轮询线程执行）。
- **N-7（已关闭）**：本报告更新为最新测试与关闭状态。
- **N-8（不处理）**：review 包 diff 中文乱码为审查工件的落盘编码问题，仓库内文件编码正常。
- 另补复审 I-6 遗留断言：旧 token 负向断言、`FakeProxy.lan_tokens` 集合断言、真实 `MediaProxy.release_token` 单测（`test_release_token_removes_target_token_only`）。

## concerns

- `CastSession` 通过 `client_factory(renderer)` 注入 DLNA 客户端，未引入 Qt 或第三方依赖。
- `set_episode()` 失败时保留当前会话字段，token 清理全部经 `MediaProxy.release_token()` 公开接口；LAN 生命周期错误经 `lan_endpoint_error()` 公开只读接口传播，无任何私有状态访问。
- 锁契约（N-6）已以 docstring 固化，但无并发测试覆盖；GUI 接入侧的串行化保证留给接入任务验证。
- `app_config.json` 存在提交前已有改动，未修改、未暂存、未提交；`gui/pages/reader/video_view.py`、`tests/test_video_cast.py`、`gui/components/cast_dialog.py` 属并行任务，未触碰。
