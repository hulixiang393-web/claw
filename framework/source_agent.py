"""LLM Agent 自动制源（source_agent.py）。

Agent 制源闭环：抓取页面 → 分析结构 → LLM 生成源配置 JSON → 复用验证引擎自动验证 →
失败自动优化重试 → 成功添加为可用源。

对应 design.md §15-§22（部分 II：LLM Agent 自动制源）。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .config import SourceConfig, CONTENT_TYPES, is_valid_source_id
from .http import HttpClient
from .parser import Parser
from .llm import LlmClient, LlmError

log = logging.getLogger(__name__)

# 默认提示词模板路径
_DEFAULT_TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "prompts",
    "source_builder.txt",
)

# HTML 采样长度上限（字符）
_HTML_SAMPLE_MAX = 8000


# ====================================================================== #
# AgentResult
# ====================================================================== #

@dataclass
class AgentResult:
    """Agent 制源结果。"""

    ok: bool
    source_id: str = ""
    draft: dict | None = None
    attempts: int = 0
    logs: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


# ====================================================================== #
# 辅助
# ====================================================================== #

def _extract_json_from_llm(text: str) -> dict | None:
    """从 LLM 回复中提取 JSON 代码块或裸 JSON 对象。"""
    # 尝试从 ```json ... ``` 代码块提取
    m = re.search(r"```json\s*\n(.*?)\n\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 尝试从 ``` ... ``` 代码块提取（无语言标记）
    m = re.search(r"```\s*\n(.*?)\n\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 尝试直接解析（裸 JSON）
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return None


def _detect_page_tech(html: str) -> str:
    """判定页面技术：ssr / spa / api。

    - SSR：HTML 正文有实质内容（标题/链接/文本 > 200 字符）
    - SPA：body 空壳或仅含 <div id="app"> 等 JS 挂载点
    - API：响应是 JSON
    """
    # 简单 JSON 检测
    stripped = html.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        return "api"
    # 去标签后看实质文本长度
    text_only = re.sub(r"<[^>]+>", "", stripped)
    text_only = re.sub(r"\s+", " ", text_only).strip()
    if len(text_only) > 200:
        return "ssr"
    # body 空壳检测
    if re.search(r'<div\s+id=["\']app["\']', stripped, re.I):
        return "spa"
    if re.search(r'<div\s+id=["\']root["\']', stripped, re.I):
        return "spa"
    return "spa"  # 默认视为 SPA


def _sample_html(html: str, max_len: int = _HTML_SAMPLE_MAX) -> str:
    """截取 HTML 片段供 LLM 分析。"""
    if len(html) <= max_len:
        return html
    # 优先保留 <head>（meta/charset）+ <body> 前半
    head_match = re.search(r"<head[^>]*>.*?</head>", html, re.S | re.I)
    head = head_match.group(0) if head_match else ""
    body_start = html.find("<body")
    if body_start < 0:
        body_start = 0
    remaining = max_len - len(head)
    if remaining > 0:
        body_sample = html[body_start : body_start + remaining]
    else:
        body_sample = ""
    return head + body_sample


# ====================================================================== #
# SourceAgent
# ====================================================================== #

class SourceAgent:
    """LLM Agent 自动制源。

    参数：
        llm: LlmClient 实例（OpenAI 兼容）
        http: HttpClient 实例（复用现有请求基础设施）
        manager: SourceManager 实例（成功后添加源）
        preview: Preview 实例（验证搜索/详情）
        template_path: 提示词模板文件路径
        max_attempts: 最大验证重试轮数
    """

    def __init__(
        self,
        llm: LlmClient,
        http: HttpClient,
        manager: Any = None,
        preview: Any = None,
        template_path: str = _DEFAULT_TEMPLATE,
        max_attempts: int = 5,
    ):
        self._llm = llm
        self._http = http
        self._manager = manager
        self._preview = preview
        self._template_path = template_path
        self._max_attempts = max_attempts
        self._parser = Parser()
        self._cancelled = False

    def cancel(self) -> None:
        """取消正在进行的制源流程。"""
        self._cancelled = True

    # ------------------------------------------------------------------ #
    def make_source(
        self,
        site_url: str,
        content_type: str,
        category: str = "",
        on_log: Callable[[str], None] | None = None,
    ) -> AgentResult:
        """制源主流程：探测 → 生成 → 验证闭环 → 保存/草稿。

        on_log: 可选日志回调，每个阶段输出日志到 UI。
        """
        self._cancelled = False
        logs: list[str] = []

        def _log(msg: str) -> None:
            logs.append(msg)
            if on_log:
                on_log(msg)

        _log(f"[Phase 1] 开始探测站点：{site_url}")

        # ========== Phase 1: 探测 ==========
        try:
            html = self._http.get_text(
                site_url,
                timeout=15.0,
                retries=1,
            )
        except Exception as exc:
            _log(f"[Phase 1] 站点抓取失败：{exc}")
            return AgentResult(
                ok=False,
                logs=logs,
                suggestions=[
                    "站点可能需要 JS 渲染（SPA），建议启用 playwright 渲染选项",
                    f"原始错误：{exc}",
                ],
            )

        page_tech = _detect_page_tech(html)
        _log(f"[Phase 1] 页面技术判定：{page_tech}")

        hints = []
        if page_tech == "spa":
            hints.append("该站点可能是 SPA（单页应用），需要 JS 渲染才能获取内容")
        if page_tech == "api":
            hints.append("该站点使用 API 接口返回数据")

        html_sample = _sample_html(html)
        _log(f"[Phase 1] HTML 采样 {len(html_sample)} 字符")

        # ========== Phase 2: LLM 生成 ==========
        _log("[Phase 2] 调用 LLM 生成源配置...")

        draft = self._generate_config(
            site_url, content_type, html_sample, hints, category, _log
        )
        if draft is None:
            return AgentResult(
                ok=False,
                logs=logs,
                suggestions=["LLM 生成的配置无法解析为有效 JSON，请检查站点 URL 和 LLM 配置"],
            )

        # 结构校验
        source_id = draft.get("$id", "")
        try:
            SourceConfig.from_dict(draft, path="<agent>")
            _log(f"[Phase 2] 结构校验通过，source_id={source_id}")
        except Exception as exc:
            _log(f"[Phase 2] 结构校验失败：{exc}")
            return AgentResult(
                ok=False,
                draft=draft,
                logs=logs,
                suggestions=[f"LLM 生成的配置结构不合法：{exc}"],
            )

        # ========== Phase 3: 验证闭环 ==========
        _log(f"[Phase 3] 开始验证（最多 {self._max_attempts} 轮）...")

        last_error = ""
        for attempt in range(1, self._max_attempts + 1):
            if self._cancelled:
                _log("[Phase 3] 已取消")
                return AgentResult(ok=False, draft=draft, attempts=attempt, logs=logs)

            _log(f"[Phase 3] 第 {attempt}/{self._max_attempts} 轮验证...")
            ok, error_msg = self._validate(draft, content_type, _log)

            if ok:
                _log(f"[Phase 3] 验证通过（第 {attempt} 轮）")
                break

            last_error = error_msg
            _log(f"[Phase 3] 验证失败：{error_msg}")

            # 让 LLM 修正
            if attempt < self._max_attempts:
                _log("[Phase 3] 回喂 LLM 修正...")
                corrected = self._correct_config(
                    draft, error_msg, html_sample, content_type, _log
                )
                if corrected is not None:
                    draft = corrected
                    # 重新结构校验
                    try:
                        SourceConfig.from_dict(draft, path="<agent>")
                    except Exception as exc:
                        _log(f"[Phase 3] 修正后结构校验失败：{exc}，继续下一轮")
        else:
            # 全部轮次用完仍然失败
            _log(f"[Phase 3] {self._max_attempts} 轮验证均失败")
            suggestions = self._generate_suggestions(last_error, page_tech)
            # 保存草稿
            try:
                draft_path = self._save_draft(draft)
                _log(f"[Phase 3] 草稿已保存：{draft_path}")
            except Exception as exc:  # noqa: BLE001
                # 草稿保存失败（磁盘/权限）→ 结果仍返回内存草稿，不中断
                _log(f"[Phase 3] 草稿保存失败（草稿保留在内存中）：{exc}")
            return AgentResult(
                ok=False,
                draft=draft,
                attempts=self._max_attempts,
                logs=logs,
                suggestions=suggestions,
            )

        # ========== Phase 4: 保存 ==========
        source_id = draft.get("$id", "unknown")
        try:
            source_path = self._save_source(draft)
            _log(f"[Phase 4] 源已保存：{source_path}")
        except Exception as exc:  # noqa: BLE001
            # 源保存失败（$id 非法被拒 / 磁盘错误）→ 降级为草稿，仍返回内存中
            # 的草稿供编辑器加载，不让异常逃逸出去卡死 AgentDialog worker。
            _log(f"[Phase 4] 源保存失败：{exc}")
            suggestions = self._generate_suggestions(f"源保存失败：{exc}", page_tech)
            try:
                self._save_draft(draft)
                suggestions.append("草稿已保存，可在源编辑器中加载并手动调整")
            except Exception as exc2:  # noqa: BLE001 —— 降级草稿也失败：草稿保留内存
                _log(f"[Phase 4] 降级草稿保存也失败（草稿保留在内存中）：{exc2}")
            return AgentResult(
                ok=False,
                draft=draft,
                attempts=self._max_attempts if last_error else 1,
                logs=logs,
                suggestions=suggestions,
            )

        # 添加到 SourceManager
        if self._manager is not None:
            try:
                config = SourceConfig.from_dict(draft, path=source_path)
                self._manager.add(config)
                _log(f"[Phase 4] 已添加到源列表：{source_id}")
            except Exception as exc:
                _log(f"[Phase 4] 添加到源列表失败：{exc}")

        return AgentResult(
            ok=True,
            source_id=source_id,
            attempts=self._max_attempts if last_error else 1,
            logs=logs,
        )

    # ------------------------------------------------------------------ #
    # Phase 2: LLM 生成
    # ------------------------------------------------------------------ #
    def _generate_config(
        self,
        site_url: str,
        content_type: str,
        html_sample: str,
        hints: list[str],
        category: str,
        log_fn: Callable,
    ) -> dict | None:
        """读取提示词模板，调用 LLM 生成源配置 JSON。"""
        try:
            # 模板优先用户定制（data/llm/source_builder.txt），否则内置；
            # 兼容测试传入的临时 template_path（存在则优先于内置读取）。
            from .llm import load_effective_template, effective_template_path

            if self._template_path and self._template_path != _DEFAULT_TEMPLATE \
                    and os.path.exists(self._template_path):
                template = Path(self._template_path).read_text(encoding="utf-8")
            else:
                _ = effective_template_path()
                template = load_effective_template()
        except OSError as exc:
            log_fn(f"[Phase 2] 提示词模板读取失败：{exc}")
            return None

        # 组装 last_error 区段
        hint_text = ""
        if hints:
            hint_text = "## 页面特征提示\n" + "\n".join(f"- {h}" for h in hints)

        user_msg = template.replace("{site_url}", site_url)
        user_msg = user_msg.replace("{content_type}", content_type)
        user_msg = user_msg.replace("{html_sample}", html_sample)
        user_msg = user_msg.replace("{last_error}", hint_text)
        if category:
            user_msg += f"\n\n## 额外信息\n- 可选类别：{category}"

        try:
            response = self._llm.chat(
                system="你是专业的爬虫配置工程师。根据提供的网站 HTML 片段，生成符合项目 Schema 的源配置 JSON。",
                user=user_msg,
                json_mode=True,
                timeout=120.0,
                # 上下文预算对齐 llama-server 16384 ctx：输入≤12000 token +
                # 输出 2048 → 总 14048 < 16384，制源不会因超 ctx 被 400 拒绝。
                # 旧进程（手动启动、默认 4096 ctx）会 400，此时设置页重启
                # llama-server 即可（新启动命令带 --ctx-size 16384）。
                ctx_limit=12000,
                max_tokens=2048,
            )
        except LlmError as exc:
            log_fn(f"[Phase 2] LLM 调用失败：{exc}")
            if "context" in str(exc).lower() or "exceeds" in str(exc).lower() \
                    or "400" in str(exc):
                log_fn("[Phase 2] 提示：本地 llama-server 上下文不足，请在设置页"
                       "「LLM」中点击「停止」后再点「启动」重启（新版本自动启用 "
                       "--ctx-size 16384）；或换更快/更大的模型")
            return None

        draft = _extract_json_from_llm(response)
        if draft is None:
            log_fn("[Phase 2] LLM 返回内容无法解析为 JSON")
            log_fn(f"[Phase 2] LLM 原始回复（前 500 字符）：{response[:500]}")
        return draft

    # ------------------------------------------------------------------ #
    # Phase 3: 验证
    # ------------------------------------------------------------------ #
    def _validate(
        self, draft: dict, content_type: str, log_fn: Callable
    ) -> tuple[bool, str]:
        """运行验证链：结构 → 搜索 → 详情 → 类型特定。"""
        # 3.1 结构校验（已在外部做，这里冗余检查）
        try:
            config = SourceConfig.from_dict(draft, path="<agent>")
        except Exception as exc:
            return False, f"结构校验失败：{exc}"

        # 3.2 搜索验证 + 3.3 详情验证（每轮只搜一次：搜索结果既用于数量校验，
        # 也用于提取详情 URL——原先同轮重复搜索会翻倍网络请求）
        if self._preview is not None:
            try:
                results = self._preview.preview_search(config, "test")
            except Exception as exc:  # noqa: BLE001 —— 预览实现通常吞异常返回 []，
                # 这里作为兜底：真抛异常同样按验证失败处理
                log_fn(f"[验证] 搜索测试异常：{exc}")
                return False, f"搜索验证失败：{exc}"

            if not results:
                # 空结果视为验证失败（选择器失效/需 JS 渲染），回喂 LLM 修正
                log_fn("[验证] 搜索无结果（可能需要 JS 渲染或配置有误）")
                return False, "搜索验证失败：搜索无结果（选择器可能失效或需要 JS 渲染）"
            log_fn(f"[验证] 搜索返回 {len(results)} 条结果")

            # 3.3 详情验证（用本次搜索结果的第一条 URL）
            try:
                first_result = results[0]
                first_url = getattr(first_result, "url", None) or ""
                if not first_url:
                    return False, "详情验证失败：搜索结果缺少 URL"
                detail = self._preview.preview_detail(config, first_url)
                if not detail or detail.get("error"):
                    err = detail.get("error", "详情为空") if detail else "详情为空"
                    log_fn(f"[验证] 详情获取异常：{err}")
                    return False, f"详情验证失败：{err}"
                log_fn(f"[验证] 详情获取成功，标题：{detail.get('title', '?')}")
            except Exception as exc:  # noqa: BLE001
                log_fn(f"[验证] 详情测试异常：{exc}")
                return False, f"详情验证失败：{exc}"

        # 3.4 类型特定验证（简化：确认 content 块存在且非空）
        content_block = draft.get("content") or {}
        if content_type == "novel":
            chapter_cfg = content_block.get("chapter")
            if not chapter_cfg:
                return False, "novel 类型缺少 content.chapter 配置"
        elif content_type == "comic":
            page_cfg = content_block.get("page")
            if not page_cfg:
                return False, "comic 类型缺少 content.page 配置"
        elif content_type == "video":
            ep_cfg = content_block.get("episode")
            if not ep_cfg:
                return False, "video 类型缺少 content.episode 配置"

        return True, ""

    # ------------------------------------------------------------------ #
    # Phase 3: LLM 修正
    # ------------------------------------------------------------------ #
    def _correct_config(
        self,
        draft: dict,
        error_msg: str,
        html_sample: str,
        content_type: str,
        log_fn: Callable,
    ) -> dict | None:
        """将验证错误回喂 LLM，请求修正。"""
        correction_prompt = (
            f"你之前生成的源配置存在以下问题：\n\n"
            f"**错误信息**：{error_msg}\n\n"
            f"**当前配置**：\n```json\n{json.dumps(draft, ensure_ascii=False, indent=2)}\n```\n\n"
            f"**原始 HTML 片段**：\n```html\n{html_sample[:4000]}\n```\n\n"
            f"请分析错误原因，修正配置，输出修正后的完整 JSON。"
        )

        try:
            response = self._llm.chat(
                system="你是专业的爬虫配置工程师。根据验证错误修正源配置 JSON。",
                user=correction_prompt,
                json_mode=False,
                timeout=120.0,
            )
        except LlmError as exc:
            log_fn(f"[Phase 3] LLM 修正调用失败：{exc}")
            return None

        corrected = _extract_json_from_llm(response)
        if corrected is None:
            log_fn("[Phase 3] LLM 修正回复无法解析为 JSON")
        return corrected

    # ------------------------------------------------------------------ #
    # Phase 4: 保存
    # ------------------------------------------------------------------ #
    def _sources_dir(self) -> Path:
        """源配置文件目录（项目根 sources/）。独立成方法便于测试重定向。"""
        return Path(__file__).resolve().parent.parent / "sources"

    def _drafts_dir(self) -> Path:
        """失败草稿目录（项目根 data/agent_drafts/）。独立成方法便于测试重定向。"""
        return Path(__file__).resolve().parent.parent / "data" / "agent_drafts"

    def _save_source(self, draft: dict) -> str:
        """保存源配置到 sources/{id}.json。

        非法 $id（可能为提示注入的路径穿越，如 "../data/llm_keys"）直接拒绝
        抛 ValueError——调用方据此降级为草稿保存，绝不把不可控文件名落盘。
        """
        source_id = draft.get("$id", "")
        if not is_valid_source_id(source_id):
            raise ValueError(
                f"非法 $id：{source_id!r}（仅允许小写字母/数字/下划线/连字符）"
            )
        sources_dir = self._sources_dir()
        sources_dir.mkdir(exist_ok=True)
        path = sources_dir / f"{source_id}.json"
        path.write_text(
            json.dumps(draft, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return str(path)

    def _save_draft(self, draft: dict) -> str:
        """保存失败草稿到 data/agent_drafts/{id}.json。

        非法 $id（可能为提示注入的路径穿越）：不写原文件名，改用内容哈希兜底
        文件名，保证文件绝不逃逸 data/agent_drafts/ 目录。
        """
        source_id = draft.get("$id", "")
        if not is_valid_source_id(source_id):
            log.warning("[agent] 草稿 $id 非法，改用哈希兜底文件名：%r", source_id)
            import hashlib

            payload = json.dumps(draft, ensure_ascii=False, sort_keys=True).encode("utf-8")
            source_id = "draft_" + hashlib.sha1(payload).hexdigest()[:12]
        drafts_dir = self._drafts_dir()
        drafts_dir.mkdir(parents=True, exist_ok=True)
        path = drafts_dir / f"{source_id}.json"
        path.write_text(
            json.dumps(draft, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return str(path)

    # ------------------------------------------------------------------ #
    def _generate_suggestions(self, last_error: str, page_tech: str) -> list[str]:
        """根据验证失败原因生成人工介入建议。"""
        suggestions = []
        if page_tech == "spa":
            suggestions.append("站点需要 JS 渲染，建议启用 playwright 渲染选项后再试")
        if "搜索" in last_error:
            suggestions.append("搜索验证失败，可能需要调整搜索选择器或搜索参数名")
        if "详情" in last_error:
            suggestions.append("详情验证失败，可能需要调整详情页选择器")
        if "chapter" in last_error or "episode" in last_error or "page" in last_error:
            suggestions.append("内容块配置缺失或不完整，请检查 content 配置")
        if not suggestions:
            suggestions.append("建议手动在源编辑器中调整配置后重新验证")
        suggestions.append("草稿已保存，可在源编辑器中加载并手动调整")
        return suggestions
