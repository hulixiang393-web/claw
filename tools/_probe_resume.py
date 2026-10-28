"""续播定位自检（临时探针，非产品代码）。

问题：视频记住的播放位置在重开后丢失，从头播放。
本脚本只做一件事：把 sources/<id>.json 的详情页按**不带 sid 的书本 URL**
重新抓一遍分集，和 data/reading_progress.json 里记下的 chapter_url 比对，
看「同一集」的 URL 是否一致——不一致就是续播丢失的直接原因。

    python tools/_probe_resume.py omofuna https://www.omofuna.com/anime/xxx.html
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from framework.content import Content  # noqa: E402
from framework.decrypter import Decrypter  # noqa: E402
from framework.http import HttpClient  # noqa: E402
from framework.parser import Parser  # noqa: E402
from framework.selfcheck import StructureChecker  # noqa: E402
from framework.source_manager import SourceManager  # noqa: E402


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    source_id, book_url = argv[1], argv[2]
    manager = SourceManager(ROOT / "sources")
    try:
        manager.load_all()
    except Exception:
        pass
    source = manager.get(source_id)
    if source is None:
        print(f"源不存在：{source_id}")
        return 2
    http = HttpClient()
    parser = Parser()
    checker = StructureChecker(http, parser)
    content = Content(http, parser, checker, Decrypter(http))
    detail = content.fetch_detail(source, book_url)
    print(f"详情：{detail.title}  url={detail.url}")
    print(f"分集 {len(detail.chapters)} 条：")
    for i, ch in enumerate(detail.chapters):
        print(f"  [{i}] {ch.title}  {ch.url}")

    rec_path = ROOT / "data" / "reading_progress.json"
    raw = json.loads(rec_path.read_text(encoding="utf-8"))
    rec = raw.get(book_url)
    if not rec:
        rec = next((v for k, v in raw.items()
                    if v.get("book_url", "").rstrip("/") == book_url.rstrip("/")), None)
    if not rec:
        print("\n没有该书进度记录")
        return 0
    saved = rec.get("chapter_url") or ""
    print(f"\n记录章节：{saved}")
    print(f"记录位置：{rec.get('position')} / location={rec.get('location')}")
    hit = [i for i, ch in enumerate(detail.chapters) if ch.url == saved]
    print(f"精确命中下标：{hit or '无'}")
    if not hit:
        # 同一集在别条线路上的 URL：去掉 sid 参数后再比对
        def strip_sid(u: str) -> str:
            base, _, q = u.partition("?")
            keep = [p for p in q.split("&") if p and not p.startswith("sid=")]
            return base + ("?" + "&".join(keep) if keep else "")

        loose = [i for i, ch in enumerate(detail.chapters) if strip_sid(ch.url) == strip_sid(saved)]
        print(f"忽略 sid 后命中下标：{loose or '无'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
