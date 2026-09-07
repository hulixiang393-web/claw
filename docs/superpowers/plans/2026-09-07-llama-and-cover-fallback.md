# LLAMA 本地模型改造 + 封面兜底 + 云端测试 + 提示词编辑

项目：claw（D:\code\claw） master
规划日期：2026-09-07

## 背景与目标

上一轮已实现「跨源换源」与「LLM Agent 自动制源」。本轮用户反馈：

1. **本地模型不是 Ollama 而是 LLAMA**（llama.cpp 的 `llama-server` + 本地 `.gguf` 文件）。现有 `OllamaManager`（探测 /api/tags + `ollama serve` 一键启动）形态不符，需替换为 LLAMA：
   - `llama-server.exe` 可执行文件：浏览选择 + PATH 探测，记住路径
   - 模型：浏览选择 `.gguf` 文件；选择过的模型记录到 recent 列表，后期下拉直接选
   - 项目内托管启动：后台 Popen 启动 `llama-server -m model.gguf`，不阻塞 UI；启动失败捕获 stderr 展示失败原因；端口可配（默认 11434 兼容既有约定）
2. **封面全局兜底**（视频源）：有的源 `detail.cover` 为空 → 阅读界面顶部封面不显示。结论：与集数无关，是源配置缺 cover。兜底方案：detail.cover 为空时 → 尝试第一集 `ep.cover` → 再不行后台 `fetch_cover` 重新提取详情页封面 → 成功则更新 UI。
3. **云端模型「测试链接」**：GET 根地址探活 + 真实 chat 打招呼验证，展示两端结果，失败区分原因（网络不通 / API Key 错误 / 模型不存在）。
4. **制源提示词可编辑**：设置页直接编辑模板文本框，保存到 data/ 下用户模板文件；SourceAgent 加载时优先用户模板，缺省回退内置 `prompts/source_builder.txt`；提供「恢复默认」。

## 关键接口变更

### framework/llm.py
- `OllamaManager` → 删除，新增 **`LlamaManager`**：
  - `__init__(base_url="http://127.0.0.1:11434", server_path="", model_path="", port=0)`
  - `running() -> bool`：GET `/health`（llama-server 支持；兼容 GET `/` 探活）
  - `start() -> tuple[bool, str]`：后台 `Popen([server_path, "-m", model_path, "--host", ..., "--port", ...])`
    失败原因从 stderr 采集（等待期间读 stderr，超时无行则探测端口）；找不到 exe / 模型不存在 / 已运行 / 启动成功分别返回 (ok, reason)。**不阻塞 UI**：启动耗时超过阈值立即返回「后台启动中」状态。
  - `stop()`：结束托管进程（记录 pid）
  - `models() -> list[str]`：改为读 `local.recent`（不再拉远程列表）
- `LlmKeyStore`：`local()` 扩展段：`{"base_url", "port", "model_path", "server_path", "model", "recent": [{model_path, server_path, name}]}`
- `LlmClient.test_connection(base_url, api_key, model) -> dict`：`{"reachable": bool, "chat_ok": bool, "steps": [...]}`，失败 step 带原因。

### framework/source_agent.py
- 模板加载改：`_template_path` 优先用户模板（`data/prompts/source_builder.txt` 存在则用它），否则内置 `prompts/source_builder.txt`。提供 `load_template()` 单点解析路径+内容。

### 新增 `framework/llm_config.py`（或并入 llm.py）
- 模板读写辅助：`user_template_path()` / `save_user_template(text)` / `reset_user_template()` / `effective_template()`——统一「用户模板优先」逻辑，供 settings_page 与 source_agent 共用。

### settings_page.py（_build_llm 重构）
- 云端组：新增「🔍 测试连接」按钮 → 后台 QThread 跑 `test_connection` → 状态行展示逐步结果（探活✓/chat✓/APIKey 错误…）。
- 本地组：标题改「本地模型（LLAMA）」：
  - llama-server 路径行：QLineEdit + 「📁 浏览…」+（PATH 自动探测提示）
  - 模型文件行：QLineEdit + 「📁 浏览 GGUF」+ 「最近使用」下拉（recent 列表，选中即填入）
  - 端口行：QSpinBox（默认 11434）
  - 「🚀 后台启动」按钮（异步/后台，不卡 UI）+ 状态行（运行中 / 失败原因）+ 「⏹ 停止」
- 提示词编辑区：「制源提示词模板」QPlainTextEdit + 「💾 保存」+「↩️ 恢复默认」（是否覆盖/重置用户模板）
- Load/Save 同步扩展字段到 `LlmKeyStore`。

### agent_dialog.py
- 本地模型判定改：读 `LlmKeyStore.local()` 的 base_url/port/model_path，模型下拉并入 recent 列表项（格式 `名称 (路径)`）；创建 LlmClient 时按当前下拉选中项匹配云端或本地。

### video_view.py + reader_page.py（封面兜底）
- `_load_ep_cover`：cover 为空 → 遍历 `detail.chapters` 取第一个非空 `ep.cover` 显示；全空 → 后台 `fetch_cover(source, detail.url)`（Content 已有，异步）→ 成功后 `setPixmap`。
- 需给 video_view 注入 content/source 引用（load() 已有 source + detail，Content 可从 reader_page 传入或复用 `_content`）。

### 测试
- 删除 `test_ollama.py`，新增 `test_llama.py`：LlamaManager running/start 失败原因（mock Popen）/停止/PATH 探测/模型不存在/port 默认。
- `test_llm.py`：补 `test_connection`（探活、chat、各失败分支）与 LlmKeyStore local 扩展段。
- `test_source_agent.py`：用户模板优先（临时 data/ 文件覆盖内置模板生效）。
- GUI 冒烟 `_smoke_settings.py` / `_smoke_agent.py` 适配（本地从 Ollama 布局改为 LLAMA + 测试按钮不点击）。

## 风险与注意
- `test_connection` chat 打招呼有真实网络请求，测试必须全 mock/离线（沿用 _FakeHttp 模式）。
- `LlamaManager.start` 不真正 spawn 进程进测试；用 mock Patch 验证命令行与 stderr 采集逻辑。
- 封面兜底 `fetch_cover` 是轻量单次请求，data 缓存复用，不引入额外反爬压力。
- 用户项目根已有 `Qwen3-VL-8B-Instruct-abliterated-v2.Q4_K_M.gguf`（未跟踪）。确认 .gitignore 不含 .gguf 时提示用户（模型不必入 git）。

## 验收
1. 设置页 LLM tab：本地为 LLAMA 布局，能浏览选 exe/.gguf，recent 可回选，端口可改，后台启动不卡界面，失败有原因。
2. 云端「测试连接」点击后展示探活/chat 结果并可区分失败原因（离线 mock 单测覆盖）。
3. 提示词可在设置页编辑并保存为 data/ 用户模板，制源实际使用用户模板；恢复默认后回退内置（单测覆盖）。
4. 视频源 detail.cover 为空时阅读界面顶部封面仍显示（第一集 cover 兜底 → fetch_cover 二段兜底）。
5. 全量测试通过（原 187 + 新增/改造），GUI 冒烟通过。