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

from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

try:
    import lxml.html

    _LXML = True
except ImportError:
    _LXML = False


def _normalize_selector(sel: dict) -> Optional[dict]:
    """归一化 selector：支持 {css} / {xpath} / {fallback} / {attr}。"""
    if sel is None:
        return None
    if isinstance(sel, list):
        return {"fallback": sel}
    if not isinstance(sel, dict):
        return None
    return sel


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
                return lxml.html.document_fromstring(html)
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
                # 懒加载兜底：URL 类属性（src/href）未取到时回退 data-src/data-original
                if not v and attr in ("src", "href"):
                    for lazy_attr in ("data-src", "data-original", "data-lazy-src"):
                        v = node.get(lazy_attr)
                        if v:
                            break
                if v:
                    results.append(v)
            else:
                txt = node.text_content().strip()
                if txt:
                    results.append(txt)
        if base_url:
            results = [self._abs(base_url, r) for r in results]
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
    ) -> List[Dict[str, str]]:
        """按字段表解析条目列表。

        root_selector 定位每个条目根；fields 是 {字段名: selector}。
        返回 [{字段: 值}]，相对 URL 已拼接。
        """
        root_selector = _normalize_selector(root_selector)
        if not self._lxml:
            # 降级：整页当单条目
            return [self._parse_fields_on_doc(doc, fields, base_url)]
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
            # 仅 URL 类 attr 才做绝对化拼接
            attr = sel.get("attr")
            use_base = base_url if attr in ("href", "src", "srcset") else ""
            item[key] = self.extract_first(doc, sel, use_base)
        return item

    # ------------------------------------------------------------------ #
    @staticmethod
    def _abs(base_url: str, url: str) -> str:
        from urllib.parse import urlsplit

        if url.startswith(("http://", "https://", "data:", "javascript:", "//")):
            return url
        # 大写协议也识别（HTTP:// 等）
        if (urlsplit(url).scheme or "").lower() in ("http", "https"):
            return url
        return urljoin(base_url, url)
