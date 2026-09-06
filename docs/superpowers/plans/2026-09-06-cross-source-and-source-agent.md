# 跨源换源 + LLM Agent 自动制源 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **双轨并行约定**：本计划分 Track A（跨源换源）与 Track B（LLM Agent 制源）两条独立轨道，**由两个独立 agent 分别实现**，互不依赖，最后合流做整体回归。每个 Track 内部按任务顺序推进，任务粒度 = 可独立测试的最小交付单元。

**Goal:** 实现「跨源换源（全类型）」与「LLM Agent 自动制源」两个功能，各自含完整测试，覆盖 106 tests 回归基线。

**Architecture:**
- Track A 新建 `framework/cross_source.py` 纯逻辑模块（标题清洗/相似度/跨源搜索/切换），GUI 层通过各视图新增信号 + ReaderPage 统一调度接入；阅读列表长标题换行修复。
- Track B 新建 `framework/llm.py`（OpenAI 兼容客户端 + Ollama 管理 + 密钥存储）与 `framework/source_agent.py`（探测→生成→验证闭环），设置页与源编辑器接入 LLM 配置与 AI 制源对话框。

**Tech Stack:** Python 3 + PySide6 6.11 + PyInstaller；测试用 pytest（无网络 mock，`SourceConfig.from_dict` + 假 HttpClient 驱动真实框架路径）；LLM 走 OpenAI Chat Completions 协议（`HttpClient` 直连，不引第三方 SDK）。

## Global Constraints

- 语言：代码注释、UI 文案全部简体中文；标识符保留英文。
- 测试基线：`pytest tests`（当前 106 passed）；新增测试沿用 `tests/test_*.py` 模式（`sys.path.insert(0, parents[1])` + `SourceConfig.from_dict` + 假 HttpClient），**不引入新依赖**；已通过代码不重复测，**修改相关代码必须重测**。
- 密钥安全：API Key 只写 `data/llm_keys.json`（`data/` 已在 .gitignore），**禁止**写入 `app_config.json`、源 JSON 或任何 git 跟踪文件；Key 输入用 `EchoMode.Password`。
- 本地模型：地址由用户在设置页填写，默认 `http://127.0.0.1:11434`；base_url 不以 `/v1` 结尾时追加 `/v1`（管理接口 `/api/*` 用原始地址）。
- 相似度：标题**先清洗噪音**再算 `difflib.SequenceMatcher.ratio()`，≥0.90 才入候选；`第X季`/`Season X` 必须保留。
- 集数序号：跨源切换后视频保留当前集序号，**0 基**：`min(max(0, current_ep_no), len(new_chapters)-1)`；漫画/小说从第 0 章加载。
- 前端 list 换行修复不影响现有布局/间距；ep_list 图标从 84×56 改 76×50。
- 每次任务完成需跑对应测试；Track 结束时跑 `pytest tests` 全量回归并提交。
- 范围外（YAGNI）：不自动换源、不跨类型转换、不做 epub 换源、不跨源书签迁移、不部署代理池/验证码破解、不无限重试（Agent N=5 保底）。

---

# Track A：跨源换源（全类型）

**目标**：阅读界面（视频/漫画/小说）源失效时，用当前作品标题去其他同类型源搜索，候选列表（相似度≥90%）供用户选择，切到新源续读。同步修复阅读列表长标题截断。

**依赖顺序**：A1（纯逻辑）→ A2（对话框）→ A3（ReaderPage 调度）→ A4（三视图入口+换行）。

---

### Task A1: cross_source 核心逻辑

**Files:**
- Create: `framework/cross_source.py`
- Test: `tests/test_cross_source.py`

**Interfaces:**
- Produces:
  - `clean_title(raw: str) -> str`
  - `similarity(a: str, b: str) -> float`
  - `<class CrossSourceCandidate>`：dataclass，字段 `source_id`, `source_name`, `title`, `url`, `cover`, `similarity`, `content_type`, `sid`（多线路源用，单线路为空串）
  - `find_cross_source(manager, content, detail, content_type, concurrent: int = 4, top_n: int = 10) -> list[CrossSourceCandidate]`
  - `switch_to_source(content, source, detail, content_type, current_ep_no: int, target: CrossSourceCandidate) -> tuple[Detail, list]`（返回新 Detail + chapters 列表）

**说明（实现者必读）**：`Content` 构造参数见 `framework/content.py:103`（`__init__(self, http, parser, cache=None, checker=None, ...)`）。`switch_to_source` 对目标源：抓详情用 `content.fetch_detail(target_source, url)`；视频取集数用 `min(max(0, int(current_ep_no)), len(chapters)-1)`（0 基）。`manager.get(source_id)` 拿 `SourceConfig`；`source_manager.all()` 返回启用源。SearchResult 的 `source_id` 字段在 `search.py`。`detail.title`/`content_type` 从阅读上下文来。清洗规则：去 `【】`、`（）` 括号内容、`undefined`/`null`/`none`（大小写不敏感）等噪音词；**保留** `第X季`/`Season X`（含数字）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_cross_source.py
# -*- coding: utf-8 -*-
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from framework.cross_source import clean_title, similarity, find_cross_source, switch_to_source, CrossSourceCandidate

def test_clean_title_strips_noise():
    assert "【" not in clean_title("【合集】名称")
    assert "（" not in clean_title("名称（独家）")
    assert clean_title("名称 undefined  ") == "名称"

def test_similarity_same_is_one():
    assert similarity("测试", "测试") == 1.0

def test_similarity_low_when_season_differs():
    assert similarity("测试 第一季", "测试 第三季") < 0.9
    assert similarity("测试 Season 1", "测试 Season 2") < 0.9
```

- [ ] **Step 2: 运行测试确认失败**
  Run: `pytest tests/test_cross_source.py -v`
  Expected: FAIL（`framework/cross_source.py` 不存在）

- [ ] **Step 3: 实现 `framework/cross_source.py`**
  - `clean_title`：用正则去除括号（全角/半角）及括号内容、`【】`、`undefined`/`null`/`none`、多余空白；`第X季`/`Season X` 保留原样。
  - `similarity`：`difflib.SequenceMatcher(None, a, b).ratio()`。
  - `CrossSourceCandidate` dataclass（如上字段）。
  - `find_cross_source`：取 `manager.all()` 中 `$type == content_type` 且 `source_id != detail.source_id`；并发 `ThreadPoolExecutor(max_workers=concurrent)` 调 `search_one_cached`（`search.py:104`）；每个 SearchResult 清洗标题→算相似度→≥0.90 组装候选；按相似度降序取 `top_n`；单源异常隔离（`log.warning` + continue）。
  - `switch_to_source`：对 video 类型，`min(max(0, int(current_ep_no)), len(chapters)-1)`；非 video 返回全部 chapters。

- [ ] **Step 4: 运行测试确认通过**
  Run: `pytest tests/test_cross_source.py -v`
  Expected: PASS

- [ ] **Step 5: 补充真实 fixture 测试（验证并发与异常隔离）**
  写 `_FakeHttp`（仿 `tests/test_search_merge.py`，按 URL 返回预设 HTML）驱动真实 `Search._search_html`；写 `_fake_manager(src_dicts) -> SourceManager`（用 `SourceConfig.from_dict` 注册 src）与 `_fake_content(src, http) -> Content`（注入假 http）。新增断言：
  - `find_cross_source` 并发上限：两源同时搜，结果数量正确
  - 单源异常不影响其他源（一个源抛错，另一个源结果保留）
  - 相似度 <0.9 被过滤（标题不同的源不出现）
  - `第X季` 不同的同源作品相似度 <0.9 被过滤

- [ ] **Step 6: 运行测试确认通过**
  Run: `pytest tests/test_cross_source.py -v`
  Expected: PASS

- [ ] **Step 7: 回归 + 提交**
  Run: `pytest tests`
  Expected: 106 + 新增全绿
  Commit: `git add framework/cross_source.py tests/test_cross_source.py && git commit -m "feat: 跨源换源核心逻辑（标题清洗/相似度/搜索/切换）"`

---

### Task A2: 换源候选对话框

**Files:**
- Create: `gui/components/cross_source_dialog.py`
- Test: `tests/test_cross_source_dialog.py`

**Interfaces:**
- Consumes: 来自 A1：`CrossSourceCandidate`
- Produces: `CrossSourceDialog(QDialog)`，构造 `__init__(self, candidates, parent=None)`；属性 `selected: CrossSourceCandidate | None`（用户选择后）；按钮「换源」启用条件 = 选中候选；含「无结果」分支（调用方传入空列表时显示提示文案）。

**说明**：候选卡显示封面（`QListWidget` + 自定义 item widget 或 `QListWidgetItem` 带 icon）+ `源名 - 标题 - 相似度%`。选满后确定；`selected` 供调用方读取。样式沿用现有 `gui/components/` 对话框惯例。

- [ ] **Step 1: 写失败测试**
  用 `pytest-qt`？**不引入新依赖**。改为**无 GUI 冒烟**：`gui/_smoke_reader.py` 扩展或新建 `gui/_smoke_cross_dialog.py`，直接在 QApplication 下构造对话框、设置候选、触发 select、断言 `selected`。
- [ ] **Step 2: 运行冒烟确认失败**
  Run: `python gui/_smoke_cross_dialog.py`
  Expected: ImportError（对话框不存在）
- [ ] **Step 3: 实现对话框**（如上）
- [ ] **Step 4: 运行冒烟确认通过**
  Run: `python gui/_smoke_cross_dialog.py`
  Expected: ok
- [ ] **Step 5: 提交**
  Commit: `git add gui/components/cross_source_dialog.py gui/_smoke_cross_dialog.py && git commit -m "feat: 跨源换源候选对话框"`

---

### Task A3: ReaderPage 调度中枢

**Files:**
- Modify: `gui/pages/reader_page.py`
- Test: `tests/test_reader_page_cross.py`（无 GUI 冒烟扩展 `gui/_smoke_reader.py`）

**Interfaces:**
- Consumes: A1 `find_cross_source`/`switch_to_source`；A2 `CrossSourceDialog`
- Produces: `ReaderPage._on_cross_source_triggered(candidate)` 内部方法（供三视图信号调用）；`_current_source`/`_current_source_id`/`_current_book_url`/`_current_content_type` 状态已存在（`reader_page.py:320-343`）。

**连接**：`__init__` 中把三视图新增信号 `cross_source_chosen` 连到 `self._on_cross_source_triggered`（信号 payload = 当前 detail，见 A4）。该方法：读 `self._current_*` 状态（`reader_page.py:320-343`）→ **先取当前视图集/章序号**（video 用 `current_episode_no()`，novel/comic 用 `0`）→ 弹 `CrossSourceDialog`（阻塞 `exec()`，UI 线程可）→ 有 `selected` 则起 `QRunnable` 后台跑 `switch_to_source(content, new_source, detail, content_type, current_index, selected)`（仿 `_SwitchSourceTask`，`reader_page.py:51`）→ 成功后按 `selected.content_type` 转调三视图对应 `load`（`source=new_source`, `start_chapter_url=""`，由视图 `load` 内部分集/章节触发进度记忆落盘，见 `reader_page.py:376-395`）。失败 → `error` 信号提示。

- [ ] **Step 1: 背景任务类 `_CrossSourceTask`（仿 `_SwitchSourceTask`）**
  `run()` 调 `cross_source.switch_to_source`，发 `done(new_detail, chapters)` / `error(str)`。
- [ ] **Step 2: 写冒烟（`gui/_smoke_reader.py` 扩展）**
  构造 ReaderPage + fake Content（给 `fetch_detail` 返回 Detail，`switch_to_source` 返回新 Detail），触发 `_on_cross_source_triggered`，断言内部状态更新为候选源。
- [ ] **Step 3: 实现 `_on_cross_source_triggered`**（见上面说明）
- [ ] **Step 4: 运行冒烟确认通过**
  Run: `python gui/_smoke_reader.py`
  Expected: ok
- [ ] **Step 5: 提交**
  Commit: `git add gui/pages/reader_page.py gui/_smoke_reader.py && git commit -m "feat: reader_page 接入跨源换源调度"`

---

### Task A4: 三视图入口 + 列表长标题换行修复

**Files:**
- Modify: `gui/pages/reader/video_view.py`（⇄菜单「跨源换源」项；`current_episode_no()` getter；`cross_source_chosen = Signal(object)`；ep_list 图标 76×50 + setWordWrap）
- Modify: `gui/pages/reader/comic_view.py`（工具条 `⇄ 换源` 按钮 + `cross_source_chosen` 信号 + toc 列表 setWordWrap）
- Modify: `gui/pages/reader/novel_view.py`（工具条 `⇄ 换源` 按钮 + `cross_source_chosen` 信号 + toc 列表 setWordWrap）
- Modify: `gui/pages/reader/epub_view.py`（toc 列表 setWordWrap，仅换行修复，不做换源入口）
- Test: `gui/_smoke_reader.py` 扩展

**Interfaces:**
- Produces: 三视图 `cross_source_chosen = Signal(object)`（payload = 当前 detail）；`VideoView.current_episode_no() -> int`（返回当前集索引，0 基；添加后供 ReaderPage 在后台任务前捕获）

**说明**：`⇄` 菜单位于 `video_view.py:384` 换源按钮；菜单项「跨源换源」触发时 `cross_source_chosen.emit(self._detail)`。ep_list 图标 84×56→76×50 见 `_populate_ep_list`（每集 item.setIcon）。comic/novel 工具条按钮放 `font_up`/`mode_btn` 后（参照 `novel_view.py:79`、`comic_view.py:82`）。toc 列表在各自 `_build_*` 中，确保 `setWordWrap(True)` 且若 `setUniformItemSizes(True)` 需关掉。冒烟验证入口按钮存在 + 点击发信号。

- [ ] **Step 1: video_view 加信号 + getter + 菜单项 + 图标尺寸 + 换行**
- [ ] **Step 2: comic_view 加按钮 + 信号 + 换行**
- [ ] **Step 3: novel_view 加按钮 + 信号 + 换行**
- [ ] **Step 4: epub_view 仅换行（不强加换源入口）**
- [ ] **Step 5: 冒烟确认三视图入口 + 换行**
  Run: `python gui/_smoke_reader.py`
  Expected: ok
- [ ] **Step 6: 全量回归 + 提交**
  Run: `pytest tests`
  Expected: 全绿
  Commit: `git add gui/pages/reader/ && git commit -m "feat: 三视图跨源换源入口 + 阅读列表长标题换行修复"`

---

### Task A5: Track A 收尾自检

- [ ] **Step 1: 对照 spec §10 验收，逐条核对**
  - 视频 `⇄` 菜单有「跨源换源」；漫画/小说工具条有 `⇄ 换源`。
  - 候选仅 ≥90%（清洗后）；含封面+源名+相似度。
  - 全源无结果弹明细提示。
  - 换源后播放器/分集/封面切到新源；视频集数保留 0 基。
- [ ] **Step 2: Track A 全量回归 + 提交**
  Run: `pytest tests`
  Expected: 全绿
  Commit: `git commit -m "chore: track A 跨源换源完成"`（若有残留改动）

---

# Track B：LLM Agent 自动制源

**目标**：设置页配置云端（OpenAI 兼容）/本地（Ollama，用户填地址）模型；源编辑器「🤖 AI 制源」输入 URL+类型，Agent 自动 抓取→生成→验证（N=5 轮）→ 成功加源 / 失败存草稿+建议。API Key 存 `data/llm_keys.json`（不入 git）。

**依赖顺序**：B1（LLM 客户端+密钥库）→ B2（Ollama 管理）→ B3（提示词模板）→ B4（SourceAgent 闭环）→ B5（设置页 LLM 区）→ B6（源编辑器 AI 入口）→ B7（Track B 收尾）。

---

### Task B1: LLM 客户端 + 密钥存储

**Files:**
- Create: `framework/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Produces:
  - `class LlmError(Exception)`
  - `class LlmKeyStore`: `__init__(self, path: str|Path = "data/llm_keys.json")`；`load() -> dict`（结构见下，缺失→空 dict）；`save(data: dict)`（mkdir parent）；`cloud() -> dict` / `local() -> dict` 便捷 getter
  - `class LlmClient`: `__init__(self, base_url: str, api_key: str = "", model: str = "")`；`chat(self, system: str, user: str, json_mode: bool = False, timeout: float = 60.0) -> str`

**说明**：`LlmClient.chat` 用 `HttpClient()`（读全局默认，不传 headers 让其注 UA）POST `{base_url}/chat/completions`；body `{"model": model, "messages": [{"role":"system","content":system},{"role":"user","content":user}]}`；`json_mode=True` 时加 `"response_format": {"type": "json_object"}`；解析 `choices[0].message.content` 返回 str；非 200/缺 `choices` 抛 `LlmError`。`base_url` 归一：`/v1` 结尾才直接用，否则追 `/v1`（存原始未归一版供 OllamaManager）。密钥结构：
```json
{"cloud": {"api_key": "", "base_url": "", "model": ""}, "local": {"base_url": "http://127.0.0.1:11434", "model": ""}}
```

- [ ] **Step 1: 写失败测试**
  `tests/test_llm.py`：用假 HttpClient 断言 `chat` 发出正确 JSON body、解析 content、抛错分支；`LlmKeyStore` 写读 `/` 缺失 / **确认写的是 data/ 下文件且不碰 app_config.json**。
- [ ] **Step 2: 运行测试确认失败**
  Run: `pytest tests/test_llm.py -v`
  Expected: FAIL（模块不存在）
- [ ] **Step 3: 实现 `framework/llm.py`**（如上）
- [ ] **Step 4: 运行测试确认通过**
  Run: `pytest tests/test_llm.py -v`
  Expected: PASS
- [ ] **Step 5: 回归 + 提交**
  Run: `pytest tests`
  Expected: 106 + 新增全绿
  Commit: `git add framework/llm.py tests/test_llm.py && git commit -m "feat: LLM 客户端与密钥存储（data/ 隔离不入 git）"`

---

### Task B2: Ollama 管理

**Files:**
- Modify: `framework/llm.py`（追加 `OllamaManager`）
- Test: `tests/test_ollama.py`

**Interfaces:**
- Consumes: B1 `LlmKeyStore`（`local().get("base_url")`）
- Produces: `class OllamaManager`: `__init__(self, base_url: str)`；`running() -> bool`；`models() -> list[str]`；`start() -> bool`

**说明**：`running()` GET `{base}/api/tags`（base=原始无 `/v1`），连通且 `{}` 响应或含 `models` 键 → True；`models()` 解析 `models[].name`；`start()` 探测 11434 → 未通则尝试 `subprocess.Popen(["ollama", "serve"])`（`shutil.which("ollama")` 找可执行，找不到返回 False）。全部用 `HttpClient`，异常捕获返回默认值（不阻塞 UI）。

- [ ] **Step 1: 写失败测试**（mock 端口/which）
- [ ] **Step 2: 运行确认失败**
  Run: `pytest tests/test_ollama.py -v`
- [ ] **Step 3: 实现 `OllamaManager`**
- [ ] **Step 4: 运行确认通过**
  Run: `pytest tests/test_ollama.py -v`
- [ ] **Step 5: 回归 + 提交**
  Commit: `git add framework/llm.py tests/test_ollama.py && git commit -m "feat: Ollama 管理（一键启动/健康探测/模型列表）"`

---

### Task B3: 提示词模板

**Files:**
- Create: `prompts/source_builder.txt`
- Test: `tests/test_prompt_template.py`

**说明**：模板内容按 spec §18——角色设定 + 类型字段矩阵（novel→content.chapter、comic→content.page、video→content.episode+media）+ 选择器写作规范 + URL 占位符用法 + 反爬注意事项 + 输出格式强约束（只输出单个 json 代码块、`$id` 唯一、必填字段齐备）。同时支持 `{site_url}`/`{content_type}`/`{html_sample}`/`{last_error}` 四个变量占位，供 SourceAgent 填充。作者按 `docs/source-schema-v2.md` 提炼字段矩阵。

- [ ] **Step 1: 写模板 + 失败测试（模板存在、含必需小节）**
- [ ] **Step 2: 运行确认失败**
  Run: `pytest tests/test_prompt_template.py -v`
- [ ] **Step 3: 完善模板内容**（对照 source-schema-v2.md）
- [ ] **Step 4: 运行确认通过**
- [ ] **Step 5: 提交**
  Commit: `git add prompts/source_builder.txt tests/test_prompt_template.py && git commit -m "feat: 内置 Agent 制源提示词模板"`

---

### Task B4: SourceAgent 闭环

**Files:**
- Create: `framework/source_agent.py`
- Test: `tests/test_source_agent.py`

**Interfaces:**
- Consumes: B1 `LlmClient`；B3 模板路径；`framework/preview.py Preview`；`framework/source_manager.py SourceManager`
- Produces: `class AgentResult`：字段 `ok: bool`、`source_id: str`、`draft: dict | None`、`attempts: int`、`logs: list[str]`、`suggestions: list[str]`
  `class SourceAgent`: `__init__(self, llm, preview: Preview, manager: SourceManager, http, template_path="prompts/source_builder.txt", max_attempts: int = 5)`；`make_source(self, site_url: str, content_type: str, category: str = "", on_log=None) -> AgentResult`

**说明**：
- **Phase 1 探测**：`http.get_text(site_url)` → 长度/有无 `</html>` 判断 SSR/SPA；空壳 → `suggestions` 加「需 render（JS）」，后续可复用 `playwright_helper`。
- **Phase 2 生成**：模板填充变量 → `llm.chat(system=模板, user=url+type+html_sample, json_mode=True)` → 剥 ```json 代码块 → `json.loads` → `SourceConfig.from_dict` 校验。
- **Phase 3 验证闭环**：`data/sources` 写入临时 → `Preview.preview_search`（keyword 用「小说/漫画/视频通用词」）+ `preview_detail`（用搜索结果第一个 url）→ 全类型都要过；失败 → 把错误+新 html 回喂 `llm.chat` 修正，最多 `max_attempts-1` 轮重试。
- **Phase 4 结果**：成功 → `manager.add(source_config)` → 写 `sources/{id}.json` → `AgentResult(ok=True, ...)`；失败 → 草稿存 `data/agent_drafts/{id}.json` + 建议（可疑反爬/需 render/需参考同类站）。
- 每阶段 `on_log(msg)` 回调通知 UI。

- [ ] **Step 1: 写失败测试**（fake LlmClient + 本地 fixture 源网站点 HTML，断言四个 Phase 行为、N 轮重试、失败草稿）
- [ ] **Step 2: 运行确认失败**
  Run: `pytest tests/test_source_agent.py -v`
- [ ] **Step 3: 实现 `framework/source_agent.py`**
- [ ] **Step 4: 运行确认通过**
  Run: `pytest tests/test_source_agent.py -v`
- [ ] **Step 5: 回归 + 提交**
  Run: `pytest tests`
  Commit: `git add framework/source_agent.py tests/test_source_agent.py && git commit -m "feat: SourceAgent 制源闭环（探测/生成/验证/重试）"`

---

### Task B5: 设置页 LLM 区块

**Files:**
- Modify: `gui/pages/settings_page.py`
- Test: `gui/_smoke_settings.py`（或在现有冒烟中扩展）

**Interfaces:**
- Consumes: B1 `LlmKeyStore`；B2 `OllamaManager`
- Produces: 设置页新增「LLM」tab；保存 → `LlmKeyStore.save()`（云端 Key 用 `EchoMode.Password`，不落 app_config.json）；「🤖 启动 Ollama」按钮 + 状态灯；「获取模型列表」下拉。

**说明**：仿 `_build_network`/`_build_ui`（`settings_page.py:174/185`）结构。Key 读取时 `load()` 不回显明文（空输入=原值）。

- [ ] **Step 1: 写冒烟（存在 LLM tab、Key 输入不回显）**
- [ ] **Step 2: 运行确认失败**
  Run: `python gui/_smoke_settings.py`
- [ ] **Step 3: 实现 LLM tab**
- [ ] **Step 4: 运行确认通过**
- [ ] **Step 5: 提交**
  Commit: `git add gui/pages/settings_page.py gui/_smoke_settings.py && git commit -m "feat: 设置页 LLM 配置区（密钥隔离存储）"`

---

### Task B6: 源编辑器 AI 制源入口

**Files:**
- Modify: `gui/components/source_editor.py`
- Create: `gui/components/agent_dialog.py`
- Test: `gui/_smoke_agent.py`

**Interfaces:**
- Consumes: B4 `SourceAgent`；B1 `LlmKeyStore`
- Produces: `AgentDialog(QDialog)`: 输入 site_url/content_type/category/模型选择；实时日志 QPlainTextEdit；开始/停止按钮；成功→信号 `source_added = Signal(object)`（含 SourceConfig 或 source_id），失败→显示建议+「保存草稿」

**说明**：顶部按钮区加「🤖 AI 制源」→ `exec()` `AgentDialog`；对话框内跑 `SourceAgent.make_source`（后台线程，`on_log` 追加日志）；成功自动 `_load_config`（`source_editor.py:1695`）进表单。

- [ ] **Step 1: 写冒烟（对话框打开/日志追加/成功信号）**
- [ ] **Step 2: 运行确认失败**
  Run: `python gui/_smoke_agent.py`
- [ ] **Step 3: 实现 `AgentDialog` + 源编辑器按钮**
- [ ] **Step 4: 运行确认通过**
- [ ] **Step 5: 提交**
  Commit: `git add gui/components/agent_dialog.py gui/components/source_editor.py gui/_smoke_agent.py && git commit -m "feat: 源编辑器 AI 制源入口 + AgentDialog"`

---

### Task B7: Track B 收尾自检

- [ ] **Step 1: 对照 spec §22 验收，逐条核对**
  - 设置页云端（Key/base_url/model）+ 本地（地址/模型）可配，Key 存 `data/llm_keys.json` 不回显明文且不入 git。
  - 「启动 Ollama」可拉起 + 可读模型。
  - 源编辑器「🤖 AI 制源」成功后源出现在源列表、可在预览面板搜索/详情验证。
  - Agent 失败自动重试 ≤5 轮，仍失败存草稿+日志+建议。
- [ ] **Step 2: Track B 全量回归 + 提交**
  Run: `pytest tests`
  Expected: 全绿
  Commit: `git commit -m "chore: track B LLM Agent 制源完成"`

---

# 合流收尾（双轨完成后）

### Task C1: 全量回归确认

- [ ] **Step 1: 全量回归**
  Run: `pytest tests`
  Expected: 106 + Track A + Track B 新增全绿
- [ ] **Step 2: 双击运行 `python gui/app.py` 冒烟主流程**（若环境可跑）
- [ ] **Step 3: 更新文档 + 提交**
  - 更新 `docs/superpowers/specs/2026-09-06-cross-source-switch-design.md` 状态为「已实施」。
  - `git add -A && git commit -m "feat: 跨源换源 + LLM Agent 制源完成"`（若有残留）