"""Playwright 渲染辅助（playwright_helper.py）。

用于：
- comicbox 等图片分片重组站：通过浏览器执行 JS，从 canvas 提取图片
- B站等需要 JS 渲染的站：获取渲染后内容

headless 模式，无弹窗。
"""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)


async def fetch_rendered_images(
    url: str,
    wait_for: str = "canvas",
    timeout_ms: int = 30000,
    extra_delay_ms: int = 2000,
    output_dir: Optional[str] = None,
) -> List[str]:
    """用 Playwright 渲染页面，从 canvas 提取图片，保存到本地。

    返回本地图片路径列表（或 base64 data URI 列表）。
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("需要安装 playwright: pip install playwright && python -m playwright install chromium")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0",
                viewport={"width": 900, "height": 2000},
            )
            page = await context.new_page()
            try:
                await page.goto(url, timeout=timeout_ms, wait_until="networkidle")
                # 等 JS 渲染 canvas
                try:
                    await page.wait_for_selector(wait_for, timeout=10000)
                except Exception:
                    pass
                await page.wait_for_timeout(extra_delay_ms)

                # 提取所有 canvas 内容为 JPEG data URI
                images = await page.evaluate("""() => {
                    const canvases = document.querySelectorAll('canvas');
                    return Array.from(canvases).map(c => {
                        try {
                            return c.toDataURL('image/jpeg', 0.85);
                        } catch(e) {
                            return null;
                        }
                    }).filter(Boolean);
                }""")

                # 如果没有 canvas，尝试 img（非加密源）
                if not images:
                    imgs = await page.query_selector_all("img")
                    for img in imgs[:200]:
                        try:
                            src = await img.get_attribute("data-src") or await img.get_attribute("src")
                            if src:
                                images.append(src)
                        except Exception:
                            pass

                # 保存到本地（可选）
                if output_dir:
                    out = Path(output_dir)
                    out.mkdir(parents=True, exist_ok=True)
                    saved = []
                    for i, data_uri in enumerate(images):
                        if data_uri.startswith("data:"):
                            # data URI → 保存为文件
                            header, b64 = data_uri.split(",", 1)
                            img_bytes = base64.b64decode(b64)
                            path = out / f"page_{i+1:03d}.jpg"
                            path.write_bytes(img_bytes)
                            saved.append(str(path))
                        else:
                            # 外部 URL → 直接引用
                            saved.append(data_uri)
                    return saved
                return images
            finally:
                await page.close()
        finally:
            await browser.close()


def fetch_rendered_images_sync(
    url: str,
    wait_for: str = "canvas",
    timeout_ms: int = 30000,
    extra_delay_ms: int = 2000,
    output_dir: Optional[str] = None,
) -> List[str]:
    """同步版本的 fetch_rendered_images。"""
    return asyncio.run(
        fetch_rendered_images(url, wait_for, timeout_ms, extra_delay_ms, output_dir)
    )
