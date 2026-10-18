"""漫画封面恢复模块（comic_cover_recovery.py）。

部分漫画站（如 comicbox）封面与正文分片图一样走加密：
- HTML 里 `div.cropped[data-src]` 的 URL 是混淆地址，直接下载 404
- 页面 JS（recover_img_url_with_multiple_hosts + merge_monga）恢复并画到 canvas

方案：用单次 Playwright 渲染书单页，滚动触发全部封面懒加载，
从 canvas 提取 base64 data URI，按作品 URL 建立映射，供发现界面展示封面。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional

log = logging.getLogger(__name__)


def recover_booklist_covers_sync(
    booklist_url: str,
    book_urls: list[str],
    output_dir: Optional[str] = None,
    proxy: Optional[str] = None,
) -> Dict[str, str]:
    """同步恢复一个书单页的封面。

    book_urls: 该页所有作品的完整 URL（如 https://www.comicbox.xyz/book/604）
    返回: {book_url: data_uri_jpeg}，仅包含成功恢复且命中 book_urls 的项。
    """
    import os

    from .playwright_helper import _default_user_agent, _pick_proxy

    # book_url 尾部数字 → 作品 id（如 /book/604 → 604）
    book_ids_to_url = {}
    import re as _re

    for u in book_urls:
        m = _re.search(r"/(\d+)/?(?:\?[^/]*)?$", u.rstrip("/"))
        if m:
            book_ids_to_url[m.group(1)] = u
    if not book_ids_to_url:
        return {}

    used_proxy = _pick_proxy(proxy)

    async def _run() -> Dict[str, str]:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                ctx_opts = {
                    "user_agent": _default_user_agent(),
                    "viewport": {"width": 1200, "height": 2000},
                }
                if used_proxy:
                    ctx_opts["proxy"] = {"server": used_proxy}
                ctx = await browser.new_context(**ctx_opts)
                page = await ctx.new_page()
                try:
                    await page.goto(
                        booklist_url, wait_until="domcontentloaded", timeout=45000
                    )
                    await page.wait_for_timeout(3500)
                    # 边滚边收集：封面 canvas 视口内复用、滚出即回收，
                    # 必须在滚动过程中每步提取；滚到底后深等再补一轮。
                    items: list = []
                    seen: set = set()

                    async def _collect_covers() -> None:
                        nonlocal items
                        cur = await page.evaluate(
                            """() => {
                                const cs = Array.from(
                                    document.querySelectorAll('canvas')
                                ).filter(c => c.width > 20 && c.height > 20);
                                const out = [];
                                for (const c of cs) {
                                    let node = c;
                                    while (node && !(node.classList && node.classList.contains('cropped'))) {
                                        node = node.parentElement;
                                    }
                                    if (!node || !node.id) continue;
                                    const m = node.id.match(/^(?:comic_)?(\\d+)_.*$/);
                                    if (!m) continue;
                                    try {
                                        const uri = c.toDataURL('image/jpeg', 0.8);
                                        if (uri.length > 2000) out.push([m[1], uri]);
                                    } catch(e) {}
                                }
                                return out;
                            }"""
                        )
                        for bid, uri in cur:
                            if bid not in seen:
                                seen.add(bid)
                                items.append({"book_id": bid, "uri": uri})

                    for _ in range(150):
                        await page.evaluate("window.scrollBy(0, 900)")
                        await page.wait_for_timeout(250)
                        await _collect_covers()
                        at_bottom = await page.evaluate(
                            "() => (window.innerHeight + window.scrollY) >= document.body.scrollHeight - 50"
                        )
                        if at_bottom:
                            break
                    # 到底深等：尾部封面集中绘制
                    await page.wait_for_timeout(8000)
                    await _collect_covers()
                finally:
                    await page.close()
            finally:
                await browser.close()
            result: Dict[str, str] = {}
            for it in items:
                u = book_ids_to_url.get(str(it["book_id"]))
                if u:
                    result[u] = it["uri"]
            return result

    try:
        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        log.warning("漫画封面恢复失败：%s", exc)
        return {}