"""页面解析器（parser.py）。

消费 selector 配置（css/xpath + attr + fallback 链），对 HTML 求值提取。
对应 source-schema-v2 §9.1 selector（含 fallback 链）与 design.md「执行引擎」层。
优先 lxml，未装则基础正则降级。

用法：
    p = Parser()
    doc = p.parse(html)
    items = p.parse_items(doc, root_selector, fields, base_url)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

try:
    import lxml.html

    _LXML = True
except ImportError:
    _LXML = False

log = logging.getLogger(__name__)

# 未装 lxml 的回退模式只告警一次（进程内），避免每次解析都刷日志
_WARNED_FALLBACK = False


def _normalize_selector(sel: dict) -> Optional[dict]:
    """归一化 selector：支持 {css} / {xpath} / {fallback} / {attr}。"""
    if sel is None:
        return None
    if isinstance(sel, list):
        return {"fallback": sel}
    if not isinstance(sel, dict):
        return None
    return sel


def _selector_attr(sel: dict | list | None) -> str:
    """Find the effective attribute when a selector uses a fallback chain."""
    sel = _normalize_selector(sel)
    if not sel:
        return ""
    attr = sel.get("attr")
    if attr:
        return str(attr)
    for nested in sel.get("fallback") or []:
        attr = _selector_attr(nested)
        if attr:
            return attr
    return ""


class Parser:
    def __init__(self) -> None:
        self._lxml = _LXML

    def engine(self) -> str:
        return "lxml" if self._lxml else "fallback"

    # ------------------------------------------------------------------ #
    def parse(self, html: str) -> Any:
        """把 HTML 解析为可查询的文档（lxml 树或字符串）。"""
        if self._lxml:
            try:
                return lxml.html.fromstring(html)
            except Exception:
                pass
            try:
                return lxml.html.document_fromstring(html)
            except Exception:
                # D7：解析失败不再静默；记录可观察错误后向上抛出，交由调用方
                # 处理（fail visible），避免把不可解析的 HTML 当作有效文档。
                log.error("HTML 解析失败（lxml 无法构造文档），已按失败处理")
                raise
        return html

    # ------------------------------------------------------------------ #
    def _query(self, doc: Any, css: Optional[str], xpath: Optional[str]) -> List[Any]:
        """在 doc 上执行 css 或 xpath 查询。"""
        if not self._lxml:
            return []
        try:
            if xpath:
                nodes = doc.xpath(xpath)
            elif css:
                nodes = doc.cssselect(css)
            else:
                nodes = []
        except Exception:
            return []
        return nodes

    # 文本提取需要补换行的块级标签（keep_breaks）
    _BLOCK_TAGS = frozenset({
        "p", "div", "br", "li", "ul", "ol", "dl", "dt", "dd", "tr", "td",
        "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "blockquote",
        "pre", "table", "hr",
    })

    @classmethod
    def _text_with_breaks(cls, node) -> str:
        """取节点文本，但把 <br>/块级边界还原为换行，并压缩多余空行。

        `text_content()` 会丢掉 `<br>` 与块边界：正文选择器指向容器时（站点
        用 `<br>` 分段），整章会被粘成一行——前端表现为"整章只有一段"。
        """
        if not _LXML or not hasattr(node, "iter"):
            return node.text_content() if hasattr(node, "text_content") else str(node)
        parts: List[str] = []

        def walk(el) -> None:
            if el.text:
                parts.append(el.text)
            for child in el:
                tag = child.tag.lower() if isinstance(child.tag, str) else ""
                if tag == "br":
                    parts.append("\n")
                elif tag in cls._BLOCK_TAGS:
                    parts.append("\n")
                    walk(child)
                    parts.append("\n")
                else:
                    walk(child)
                if child.tail:
                    parts.append(child.tail)

        walk(node)
        lines = [line.strip(" \t\u00a0\u3000") for line in "".join(parts).split("\n")]
        out: List[str] = []
        for line in lines:
            if not line and out and not out[-1]:
                continue  # 连续空行压成一行
            out.append(line)
        return "\n".join(out).strip("\n")

    # ------------------------------------------------------------------ #
    def extract(
        self, doc: Any, sel: dict | None, base_url: str = ""
    ) -> List[str]:
        """按 selector 提取全部值（文本或属性）。支持 fallback 链。"""
        sel = _normalize_selector(sel)
        if sel is None:
            return []
        if "fallback" in sel:
            # fallback 链：第一个匹配且非空返回
            for s in sel["fallback"]:
                vals = self.extract(doc, s, base_url)
                if vals:
                    return vals
            return []

        css = sel.get("css")
        xpath = sel.get("xpath")
        attr = sel.get("attr")
        regex = sel.get("regex")
        b64 = sel.get("b64decode")
        # keep_breaks：文本提取时把 <br>/块级元素边界还原成换行。
        # 正文选择器常直接指向容器（如 #content），容器里的段落全靠 <br> 分隔；
        # 用 text_content() 会把整章粘成一行（读起来"整章只有一段"）。
        keep_breaks = bool(sel.get("keep_breaks"))
        nodes = self._query(doc, css, xpath)
        results: List[str] = []
        for node in nodes:
            if isinstance(node, str):
                results.append(node.strip())
                continue
            if attr == "html":
                inner = "".join(lxml.html.tostring(c, encoding="unicode") for c in node.iterchildren())
                results.append(inner)
            elif attr:
                v = node.get(attr)
                # 懒加载兜底：URL 类属性（src/href）为空**或是 data: 占位**
                # （如 eporner 懒加载卡片 src="data:image/gif;base64,..."、
                # 真实图在 data-src）时回退 data-src/data-original。
                # data: URI 非空但不是可加载的网络图，必须让位给懒加载属性，
                # 否则一半卡片封面拿到透明 GIF（表现为"封面时有时无"）。
                if attr in ("src", "href") and (not v or v.startswith("data:")):
                    for lazy_attr in ("data-src", "data-original", "data-lazy-src"):
                        v = node.get(lazy_attr)
                        if v and not v.startswith("data:"):
                            break
                if v:
                    results.append(v)
            else:
                txt = (
                    self._text_with_breaks(node)
                    if keep_breaks
                    else node.text_content()
                ).strip()
                if txt:
                    results.append(txt)
        if regex or b64:
            import re as _re
            from urllib.parse import unquote
            import base64 as _b64

            def _transform(r: str) -> str:
                if regex:
                    m = _re.search(str(regex), r)
                    r = m.group(1) if m else ""
                if b64 and r:
                    try:
                        r = _b64.b64decode(unquote(r)).decode("utf-8", "replace")
                    except Exception:
                        r = ""
                return r

            results = [_transform(r) for r in results]
        if base_url:
            results = [self._abs(base_url, r) for r in results]
        # 字段 URL 重写（refresh/防盗链换域）：url_replace = [regex, repl]。
        # repl 支持 {n} 引用捕获组（对齐 content._add_imgs）；无匹配保留原值。
        ur = sel.get("url_replace")
        if ur and results and isinstance(ur, (list, tuple)) and len(ur) == 2 and ur[0]:
            import re as _re

            ur_re = str(ur[0])
            ur_tpl = str(ur[1])

            def _rewrite(r: str) -> str:
                m = _re.search(ur_re, r)
                if not m:
                    return r
                repl = ur_tpl
                for gi in range(1, len(m.groups()) + 1):
                    repl = repl.replace("{%d}" % gi, m.group(gi) or "")
                return repl

            results = [_rewrite(r) for r in results]
        return results

    def extract_first(
        self, doc: Any, sel: dict | None, base_url: str = ""
    ) -> str:
        vals = self.extract(doc, sel, base_url)
        return vals[0] if vals else ""

    # ------------------------------------------------------------------ #
    def parse_items(
        self,
        doc: Any,
        root_selector: dict | None,
        fields: Dict[str, dict | None],
        base_url: str,
        *,
        source_id: str = "",
        page_url: str = "",
    ) -> List[Dict[str, str]]:
        """按字段表解析条目列表。

        root_selector 定位每个条目根；fields 是 {字段名: selector}。
        返回 [{字段: 值}]，相对 URL 已拼接。
        source_id / page_url 只用于诊断日志（可选），不影响解析结果。
        """
        root_selector = _normalize_selector(root_selector)
        if not self._lxml:
            # D7：缺少 lxml 是「解析器不可用」状态，不再静默降级。旧行为把整页
            # 当单条目返回，结果错误且掩盖了配置/环境问题。这里明确记录错误并
            # 返回明确失败（空列表），让上层可观察、可定位（fail visible）。
            global _WARNED_FALLBACK
            if not _WARNED_FALLBACK:
                _WARNED_FALLBACK = True
                log.error(
                    "解析器不可用：未安装 lxml，无法按 selector 解析条目，已返回空结果"
                    "（source=%s url=%s）。请安装：pip install lxml。",
                    source_id or "-",
                    page_url or base_url or "-",
                )
            return []
        if root_selector is None:
            roots = [doc]
        else:
            css = root_selector.get("css")
            xpath = root_selector.get("xpath")
            roots = self._query(doc, css, xpath)
            # exclude: source-schema §3.4 list.root_selector.exclude
            # 从 roots 中移除匹配 exclude 选择器的节点
            exclude_sels = root_selector.get("exclude")
            if exclude_sels and roots and self._lxml:
                exclude_nodes: set = set()
                for es in exclude_sels:
                    es = _normalize_selector(es)
                    if es is None:
                        continue
                    ecss = es.get("css")
                    exml = es.get("xpath")
                    if ecss or exml:
                        for n in self._query(doc, ecss, exml):
                            exclude_nodes.add(n)
                if exclude_nodes:
                    roots = [r for r in roots if r not in exclude_nodes]
        items = []
        for root in roots:
            item = self._parse_fields_on_doc(root, fields, base_url)
            items.append(item)
        return items

    def _parse_fields_on_doc(
        self, doc: Any, fields: Dict[str, dict | None], base_url: str
    ) -> Dict[str, str]:
        item: Dict[str, str] = {}
        for key, sel in fields.items():
            sel = _normalize_selector(sel)
            if sel is None:
                item[key] = ""
                continue
            # 仅 URL 类 attr 才做绝对化拼接（含懒加载属性 data-*，
            # 否则封面等返回相对路径 GUI 加载不出）
            # A top-level fallback has no own ``attr`` key, but its selected
            # branch may be a URL attribute that still needs base resolution.
            attr = _selector_attr(sel)
            use_base = base_url if attr in (
                "href", "src", "srcset", "data-src", "data-original", "data-lazy-src"
            ) else ""
            item[key] = self.extract_first(doc, sel, use_base)
        return item

    # ------------------------------------------------------------------ #
    @staticmethod
    def _abs(base_url: str, url: str) -> str:
        from urllib.parse import urlsplit

        # 协议相对 URL（//host/path）补 https:，否则 GUI 加载不了
        if url.startswith("//"):
            return "https:" + url
        if url.startswith(("http://", "https://", "data:", "javascript:")):
            return url
        # 大写协议也识别（HTTP:// 等）
        if (urlsplit(url).scheme or "").lower() in ("http", "https"):
            return url
        return urljoin(base_url, url)
