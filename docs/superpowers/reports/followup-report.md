# Followup Review 修复报告

## 范围

- 修复 HLS 广告段误删、外部播放器代理、VideoView 锁源和检测状态清理。
- 修复搜索标题规范化、ikanpp provider fallback 判定与详情 URL 编码。
- 保留工作区现有改动；未修改用户配置、SESSION、记忆文件；未提交。

## 修复

- `framework/search.py`：标题规范化改用 NFKC、大小写折叠和真正的 `\W` 标点/空白清理，覆盖中文标题、英文副标题和全角标点；命中规范化标题时不触发 provider fallback，未命中时才回退。
- `framework/search.py`、`framework/discovery.py`：模板生成的详情 URL 对路径占位符和 query 参数进行 URL 编码，标题含 `&`、`#`、`?` 时仍可由详情请求正确还原。
- `framework/adblock.py`：恢复 URL + `#EXT-X-BYTERANGE` 重复段键、协议/CUE 标记、带可靠广告信号的独立短块、明确广告 URL 过滤；普通重复 URL、不同 BYTERANGE 和无可靠信号的短块保留；不从 TS URL/二进制关键词猜测广告；移除 `False and` 禁用规则。
- `framework/external_player.py`、`framework/media_proxy.py`：URL 任意位置包含 `m3u8` 均视为 HLS；无 VLC 回退仍使用代理 URL，不直接打开原始 HLS。
- `framework/content.py`、`framework/stream_resolver.py`、`gui/pages/reader/video_view.py`：当前作品已选源取流失败时保持源锁，不调用 `_next_available_sid`，也不进入 content 自动换线路；换作品仍清除锁。
- `gui/pages/reader/video_view.py`：检测取消或作品/源切换时清除进度文案，恢复检测按钮和线路原始名称。

## 回归验证

- 定向搜索、ikanpp、广告过滤及相关媒体测试：`60 passed`。
- Python 3.10 `-k "search or adblock or ikanpp or media_proxy_ad or downloader_ad_default"`：`156 passed`，4 个失败为工作区既有缺失源文件/配置断言。
- Python 3.10 全量：复现既有 `tests/test_acgxmh_perf.py::test_future_prefetch_jumps_ahead_of_backward_cache` 失败，并在 GUI 测试阶段超时，未发现本次相关测试失败。
- Python 3.10：`compileall` 通过。
- `git diff --check` 通过；仅报告既有 CRLF 转换提示。
