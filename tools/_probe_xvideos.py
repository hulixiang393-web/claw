"""xvideos 源真实链路验收（一次性脚本，非产品代码）。

跑发现 → 搜索 → 详情 → 取流四段，打印可复现的统计，不改任何数据文件。

    python tools/_probe_xvideos.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from framework.config import load_source  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.decrypter import Decrypter  # noqa: E402
from framework.discovery import Discovery  # noqa: E402
from framework.http import HttpClient  # noqa: E402
from framework.parser import Parser  # noqa: E402
from framework.search import Search  # noqa: E402
from framework.selfcheck import StructureChecker  # noqa: E402

SRC = ROOT / "sources" / "xvideos.json"


def main() -> int:
    source = load_source(SRC)
    http = HttpClient()
    parser = Parser()
    checker = StructureChecker(http, parser)
    content = Content(http, parser, checker, Decrypter(http))
    discovery = Discovery(http, parser, checker)
    search = Search(http, parser, discovery, concurrent=1)
    headers = source.request_headers()

    print("== discovery /tags/xvedio (page 1)")
    works = discovery.list_works(source, "/tags/xvedio", 1)
    print(f"   works={len(works)}")
    for w in works[:3]:
        print(f"   - {w.title[:60]} | {w.url}")

    print("\n== discovery /tags/xvedio?page=2")
    works2 = discovery.list_works(source, "/tags/xvedio", 2)
    overlap = {w.url for w in works} & {w.url for w in works2}
    print(f"   works={len(works2)} overlap_with_page1={len(overlap)}")
    for w in works2[:2]:
        print(f"   - {w.title[:60]} | {w.url}")

    print("\n== search 'brazzers'（免费 tab）")
    results = search.search_one(source, "brazzers")
    print(f"   results={len(results)}")
    for r in results[:3]:
        print(f"   - {r.title[:60]} | {r.url}")

    print("\n== detail + stream")
    target = results[0].url if results else (works[0].url if works else "")
    detail = content.fetch_detail(source, target)
    print(f"   url={detail.url}")
    print(f"   title={detail.title[:70]}")
    print(f"   author={detail.author!r} status={detail.status!r}")
    print(f"   tags={detail.tags[:6]}")
    print(f"   chapters={len(detail.chapters)} -> {[c.url for c in detail.chapters][:1]}")
    ep = detail.chapters[0].url if detail.chapters else target
    play = content._fetch_play_url_once(source, ep)
    print(f"   play_url={play}")

    head = http.get_text(play, headers=headers, timeout=30, retries=1)
    variants = [ln.strip() for ln in head.splitlines()
                if ln.strip() and not ln.startswith("#")]
    print(f"   master bytes={len(head)} variants={variants}")
    if variants:
        variant_url = urljoin(play, variants[0])
        sub = http.get_text(variant_url, headers=headers, timeout=30, retries=1)
        segs = [ln.strip() for ln in sub.splitlines()
                if ln.strip() and not ln.startswith("#")]
        print(f"   variant={variant_url.rsplit('/', 1)[-1]} segments={len(segs)}")
        if segs:
            seg_url = urljoin(variant_url, segs[0])
            print(f"   first_segment HTTP {http.get_status(seg_url, headers=headers, timeout=30)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
