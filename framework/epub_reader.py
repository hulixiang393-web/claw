"""轻量 epub 读取器（epub_reader.py）。

用 zipfile 单遍索引 + 按需读取，**不构建 ebooklib 的 item 对象树**——
ebooklib read_epub 对 8000+ item 的大书（如 1.2GB 漫画）要 20 秒+，本模块毫秒级。

能力：
- 解析 container.xml / OPF（manifest + spine）、NCX / nav 目录标题
- 章节按 zip 名单条读取（zipfile.read 只解压目标条目）
- 合并单文档（整本书拼一个超大 xhtml）按 `<h1>` 拆分章节
  （修罗武神 1980 万字、漫画 8267 图这类），拆出的章带 text 或 img_srcs

线程安全：zipfile 文件句柄跨线程读需外部加锁（调用方负责）。
"""

from __future__ import annotations

import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET

OPF_NS = "http://www.idpf.org/2007/opf"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
XHTML_NS = "http://www.w3.org/1999/xhtml"

# 章节正文文档扩展名
DOC_EXTS = (".xhtml", ".html", ".htm", ".xml")
# 判定"合并单文档"的最小解压尺寸（spine 仅一个 xhtml 且大于它 → 按章节拆分）
MERGE_DOC_THRESHOLD = 200 * 1024


def _html_to_text(html: str) -> str:
    """epub 章节 XHTML → 纯文本（段落换行，去标签）。"""
    text = html or ""
    text = re.sub(r"<p[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<br[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def split_single_document(html: str) -> list[tuple[str, str, list]]:
    """把合并单文档按 `<h1>标题</h1>` 拆成 [(title, text, img_srcs), ...]。

    每段：漫画（含 <img>）→ img_srcs 记录图片 zip 名（字节渲染时懒读）；
    否则 text = 该段纯文本。首个 h1 之前的内容（前言/简介）并入首段。
    """
    # 找所有 <h1 ...>标题</h1>，记录标题与内容起点
    marks: list[tuple[int, int, str]] = []  # (content_start, next_start, title)
    for m in re.finditer(r"<h1[^>]*>(.*?)</h1>", html or "", flags=re.IGNORECASE | re.DOTALL):
        title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        marks.append((m.end(), -1, title))
    for i in range(len(marks)):
        nxt = marks[i + 1][0] if i + 1 < len(marks) else len(html)
        marks[i] = (marks[i][0], nxt, marks[i][2])
    if not marks:
        return []  # 无 h1 → 非分章文档，由调用方按整章处理
    # 首个 h1 前的内容（前言）并入第一段
    first = marks[0]
    prefix = html[: first[0]]
    marks[0] = (0, first[1], first[2]) if prefix.strip() else first

    out: list[tuple[str, str, list]] = []
    for start, end, title in marks:
        frag = html[start:end]
        if re.search(r"<img\b", frag, flags=re.IGNORECASE):
            srcs = [s for s in re.findall(r'src="([^"]+)"', frag, flags=re.IGNORECASE) if s]
            out.append((title or f"第{len(out)+1}话", "", srcs))
        else:
            out.append((title or f"第{len(out)+1}章", _html_to_text(frag), []))
    return out


class FastEpub:
    """轻量 epub：zip 单遍索引 + 按需读取（替代 ebooklib 全量解析）。"""

    def __init__(self, path: str):
        self.path = str(path)
        self._zf = zipfile.ZipFile(self.path)
        self._names: set[str] = set(self._zf.namelist())
        self._closed = False
        self.manifest: dict[str, str] = {}     # item id → href
        self.spine_hrefs: list[str] = []       # 正文文档路径（spine 顺序，含 opf 目录前缀）
        self.toc_titles: dict[str, str] = {}   # 文档路径/basename → 标题
        self.basename_index: dict[str, str] = {}  # basename → zip 内路径（图片懒读用）
        for _n in self._names:
            self.basename_index.setdefault(posixpath.basename(_n), _n)
        self._load_metadata()

    # ------------------------------------------------------------------ #
    def _load_metadata(self) -> None:
        opf = self._find_opf()
        if not opf:
            return
        root = ET.fromstring(self.read(opf))
        ns = {"opf": OPF_NS}
        self.manifest = {}
        for item in root.findall(".//opf:manifest/opf:item", ns):
            self.manifest[item.get("id")] = item.get("href") or ""
        spine_el = root.find(".//opf:spine", ns)
        toc_id = spine_el.get("toc") if spine_el is not None else None
        hrefs = []
        if spine_el is not None:
            for itemref in spine_el.findall("opf:itemref", ns):
                idref = itemref.get("idref")
                href = self.manifest.get(idref, "")
                if href:
                    hrefs.append(href)
        opf_dir = posixpath.dirname(opf)
        self.spine_hrefs = [
            self._join(opf_dir, h) for h in hrefs
            if h.lower().endswith(DOC_EXTS) and not self._is_nav(h)
        ]
        self.toc_titles = self._parse_toc(toc_id, opf_dir)

    def _find_opf(self) -> str:
        try:
            container = self.read("META-INF/container.xml")
        except KeyError:
            return ""
        ns = {"c": CONTAINER_NS}
        root = ET.fromstring(container)
        for rf in root.findall(".//c:rootfiles/c:rootfile", ns):
            return rf.get("full-path") or ""
        return ""

    @staticmethod
    def _is_nav(href: str) -> bool:
        low = href.lower()
        return "nav" in low or "toc" in low

    @staticmethod
    def _join(opf_dir: str, href: str) -> str:
        if href.startswith("/"):
            return href.lstrip("/")
        return posixpath.normpath(posixpath.join(opf_dir, href))

    def _parse_toc(self, toc_id: str, opf_dir: str) -> dict:
        titles: dict[str, str] = {}
        href = self.manifest.get(toc_id, "") if toc_id else ""
        if not href:
            # 找不到 ncx/nav → 尝试常见文件名
            for cand in ("nav.xhtml", "toc.ncx", "ncx"):
                for n in self._names:
                    if n.lower().endswith("/" + cand) or n.lower() == cand:
                        href = n
                        break
                if href:
                    break
        if not href:
            return titles
        full = self._join(opf_dir, href)
        if not self.has(full):
            return titles
        try:
            raw = self.read(full)
        except Exception:
            return titles
        text = raw.decode("utf-8", errors="replace")
        # NCX
        ncx_hrefs = re.findall(
            r'<navPoint\b[^>]*>.*?<navLabel>.*?<text>([^<]*)</text>.*?</navLabel>'
            r'.*?<content\s+src="([^"]+)"',
            text, flags=re.IGNORECASE | re.DOTALL,
        )
        if ncx_hrefs:
            for title, src in ncx_hrefs:
                key = self._join(opf_dir, src)
                titles[key] = title.strip()
                titles[posixpath.basename(key)] = title.strip()
            return titles
        # EPUB3 nav.xhtml
        nav_hrefs = re.findall(
            r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', text, flags=re.IGNORECASE | re.DOTALL
        )
        for src, label in nav_hrefs:
            label = re.sub(r"<[^>]+>", "", label).strip()
            key = self._join(opf_dir, src)
            titles[key] = label
            titles[posixpath.basename(key)] = label
        return titles

    # ------------------------------------------------------------------ #
    def has(self, name: str) -> bool:
        return name in self._names

    def read(self, name: str) -> bytes:
        """按 zip 内路径读单条资源（只解压目标条目）。"""
        return self._zf.read(name)

    def file_size(self, name: str) -> int:
        try:
            return self._zf.getinfo(name).file_size
        except KeyError:
            return 0

    def close(self) -> None:
        if not self._closed:
            try:
                self._zf.close()
            except Exception:  # noqa: BLE001
                pass
            self._closed = True
