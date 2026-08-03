"""Playwright 渲染辅助（playwright_helper.py）。

用于：
- comicbox 等图片分片重组站：通过浏览器执行 JS，从 canvas 提取图片
- B站等需要 JS 渲染的站：获取渲染后内容

headless 模式，无弹窗。

关键修复：
1. **自动使用系统代理**：Python requests 走 Clash 代理能连上 comicbox，
   而 Playwright 默认不走代理 → ERR_CONNECTION_TIMED_OUT。
   这里读取 HTTP_PROXY / HTTPS_PROXY 环境变量（Clash 常设 127.0.0.1:7890），
   并优先使用源配置 transports.proxy。
2. **wait_until="domcontentloaded" 代替 networkidle**：
   漫画页有持续懒加载，networkidle 永远等不到 → 超时。
3. **逐页等待 canvas 绘制**：懒加载页面初始只渲染前几页 canvas，
   滚动/等待后更多 canvas 出现，逐个提取。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)


def _system_proxy() -> Optional[str]:
    """从环境变量读取系统代理（Clash 等）。优先 HTTPS，其次 HTTP。"""
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = os.environ.get(var)
        if val:
            return val
    # 兜底：常见 Clash 默认端口（很多用户只配了浏览器，没配环境变量）
    for port in ("7890", "7897", "10809"):
        candidate = f"http://127.0.0.1:{port}"
        if _port_open(int(port)):
            return candidate
    return None


def _port_open(port: int, timeout: float = 0.15) -> bool:
    """快速探测本地端口是否有服务在监听。"""
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _pick_proxy(source_proxy: Optional[str] = None) -> Optional[str]:
    """选取代理：源配置指定 > 系统环境代理 > Clash 默认端口探测 > None。"""
    if source_proxy:
        return source_proxy
    return _system_proxy()


def _default_user_agent() -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )


def _page_sort_key(cid: str):
    """cropped id 排序键：尽量按末尾数字序（如 '897144'），否则按字符串。"""
    m = re.search(r"\d+$", cid or "")
    if m:
        return (0, int(m.group(0)))
    return (1, cid or "")


async def _wait_canvas_ready(page, selector: str, count: int, timeout_ms: int):
    """等待指定数量的 canvas 出现并绘制完成。

    页面懒加载：初始只渲染部分 canvas。轮询直到足够数量且尺寸非 0。
    """
    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000.0
    while asyncio.get_event_loop().time() < deadline:
        n = await page.evaluate(
            """() => {
                const cs = Array.from(document.querySelectorAll('canvas'));
                return cs.filter(c => c.width > 0 && c.height > 0).length;
            }"""
        )
        if n >= count:
            return n
        await page.wait_for_timeout(300)
    return n


async def _dom_canvas_total(page) -> int:
    """DOM 中 canvas 总数（与是否绘制无关）。表示页面共部署了多少张图的分片。"""
    return await page.evaluate("() => document.querySelectorAll('canvas').length")


async def _rendered_canvas_count(page) -> int:
    """已绘制（宽高非 0）的 canvas 数。"""
    return await page.evaluate(
        """() => {
            return Array.from(document.querySelectorAll('canvas'))
                .filter(c => c.width > 0 && c.height > 0).length;
        }"""
    )


async def _wait_all_canvases_ready(
    page, expected: int, timeout_ms: int, min_data_len: int = 2000
) -> int:
    """等待全部 canvas 就绪：总数≥expected、每个宽高非0、且 toDataURL 非空白。

    merge_monga 的绘制是异步 img.onload→decode→drawImage 链，canvas 被创建后
    像素可能尚未提交（宽高已设但内容空白）。本函数轮询到每个 canvas 的
    toDataURL 长度超过阈值（非空白）或超时。
    """
    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000.0
    while asyncio.get_event_loop().time() < deadline:
        state = await page.evaluate(
            """(threshold) => {
                const cs = Array.from(document.querySelectorAll('canvas'))
                    .filter(c => c.width > 0 && c.height > 0);
                let painted = 0;
                for (const c of cs) {
                    try {
                        const uri = c.toDataURL('image/jpeg', 0.85);
                        if (uri.length > threshold) painted++;
                    } catch(e) {}
                }
                return {total: cs.length, painted};
            }""",
            min_data_len,
        )
        if state["total"] >= expected and state["painted"] >= expected:
            return True
        if state["total"] >= expected and state["total"] == state["painted"] > 0:
            return True
        await page.wait_for_timeout(300)
    return False


async def fetch_rendered_images(
    url: str,
    wait_for: str = "canvas",
    wait_until: str = "domcontentloaded",
    timeout_ms: int = 30000,
    extra_delay_ms: int = 2500,
    click_selector: Optional[str] = None,
    scroll_to_bottom: bool = False,
    extract_mode: str = "canvas",
    output_dir: Optional[str] = None,
    proxy: Optional[str] = None,
    page_container_selector: Optional[str] = None,
    scroll_step_px: int = 600,
    scroll_stale_rounds: int = 6,
) -> List[str]:
    """用 Playwright 渲染页面，提取内容。

    返回内容列表：base64 data URI / 图片 URL / 文本块，取决于 extract_mode。

    参数（源配置 render_config 可覆盖全部）：
        wait_for:          渲染目标选择器（如 "canvas" / ".img"）
        wait_until:        页面就绪策略（"domcontentloaded" / "networkidle" / "load"）
        extra_delay_ms:    渲染后额外等待（ms）
        click_selector:    可选：渲染后点击该元素（展开弹层/触发懒加载）
        scroll_to_bottom:  可选：滚动到页底，触发滚动懒加载
        extract_mode:      提取方式
            "canvas"  → 收集所有 canvas 的 toDataURL（加密分片图合并结果）
            "img"     → 收集所有 img 的 data-src/src（普通图片 URL）
            "text"    → 收集页面 body 文本（JS 渲染出的正文）
        proxy:             显式代理（如 http://127.0.0.1:7890）；None 时自动探测系统代理。
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError(
            "需要安装 playwright: pip install playwright && python -m playwright install chromium"
        )

    used_proxy = _pick_proxy(proxy)
    if used_proxy:
        log.info("[playwright] 使用代理 %s", used_proxy)
    else:
        log.info("[playwright] 未发现代理，直连（若连不上请配置 Clash 代理）")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context_opts = {
                "user_agent": _default_user_agent(),
                "viewport": {"width": 900, "height": 2000},
            }
            if used_proxy:
                context_opts["proxy"] = {"server": used_proxy}
            context = await browser.new_context(**context_opts)
            page = await context.new_page()
            try:
                # 页面就绪策略由源配置 render_config.wait_until 控制
                await page.goto(url, timeout=timeout_ms, wait_until=wait_until)
                # 等渲染目标出现
                try:
                    await page.wait_for_selector(wait_for, timeout=12000)
                except Exception:
                    pass

                # 可选动作：点击元素（展开弹层/触发更多内容）
                if click_selector:
                    try:
                        await page.click(click_selector, timeout=5000)
                    except Exception:
                        log.warning("[playwright] 点击 %s 失败（忽略）", click_selector)
                # 可选动作：滚动到页底，触发滚动懒加载。
                # 注意：此类漫画站（comicbox）的 canvas 是"视口内复用"的——
                # 滚出视口即被 JS 移除回收。因此必须**边滚边收集**：
                # 每一步滚动后立即提取当前已绘制 canvas，按 cropped id 去重累积；
                # 滚到底后在底部深等（尾部 canvas 集中绘制），再补一轮收集。
                drawn_pages: dict = {}  # {cropped_id: data_uri}
                if scroll_to_bottom:
                    async def _collect_drawn() -> None:
                        items = await page.evaluate(
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
                                    try {
                                        const uri = c.toDataURL('image/jpeg', 0.85);
                                        if (uri.length > 2000) out.push([node.id, uri]);
                                    } catch(e) {}
                                }
                                return out;
                            }"""
                        )
                        for cid, uri in items:
                            drawn_pages[cid] = uri

                    # 大步快滚 + 边滚边收集：按已收集的页 id 数判断是否到底，
                    # 不再机械滚满 150 步。新的页 id 不再增长即视为结束（更快）。
                    step = max(scroll_step_px, 1000)
                    no_new_pages = 0
                    for _scroll_step in range(120):
                        await page.evaluate(f"window.scrollBy(0, {step})")
                        await page.wait_for_timeout(80)  # 80ms/步，更快
                        prev_count = len(drawn_pages)
                        await _collect_drawn()
                        if len(drawn_pages) > prev_count:
                            no_new_pages = 0
                        else:
                            no_new_pages += 1
                        # 连续 10 步无新页 → 已近末尾，提前结束滚动
                        if no_new_pages >= 10:
                            break
                    # 到底/结束：短等尾部 canvas 集中绘制，再补一轮收集
                    await page.wait_for_timeout(2500)
                    await _collect_drawn()

                # 给 JS 绘制留时间
                await page.wait_for_timeout(extra_delay_ms)


                # 按 extract_mode 分派提取
                if extract_mode == "text":
                    # JS 渲染出的正文/文本
                    text = await page.evaluate("() => document.body.innerText")
                    images = [t.strip() for t in (text or "").splitlines() if t.strip()] or []
                elif extract_mode == "img":
                    # 普通图片 URL（懒加载 data-src / src）
                    imgs = await page.query_selector_all("img")
                    images = []
                    for img in imgs[:500]:
                        try:
                            src = (
                                await img.get_attribute("data-src")
                                or await img.get_attribute("src")
                            )
                            if src:
                                images.append(src)
                        except Exception:
                            pass
                else:
                    # 默认 canvas：加密分片图合并后的完整页。
                    # 若已边滚边收集（scroll_to_bottom），直接用累积结果；
                    # 否则滚动回顶后统一等待并提取。
                    async def _collect_canvases_with_ids():
                        return await page.evaluate(
                            """() => {
                                const canvases = document.querySelectorAll('canvas');
                                const out = [];
                                for (const c of canvases) {
                                    if (c.width === 0 || c.height === 0) continue;
                                    let node = c;
                                    while (node && !(node.classList && node.classList.contains('cropped'))) {
                                        node = node.parentElement;
                                    }
                                    try {
                                        const uri = c.toDataURL('image/jpeg', 0.85);
                                        if (uri.length < 2000) continue;
                                        out.push([node ? node.id : '', uri]);
                                    } catch(e) {}
                                }
                                return out;
                            }"""
                        )

                    if drawn_pages:
                        images = [uri for _, uri in
                                  sorted(drawn_pages.items(),
                                         key=lambda kv: _page_sort_key(kv[0]))]
                    else:
                        # 未滚动：等待并提取当前视口全部 canvas
                        await _wait_canvas_ready(page, wait_for, count=1, timeout_ms=8000)
                        collected = await _collect_canvases_with_ids()
                        for cid, uri in collected:
                            drawn_pages[cid] = uri
                        images = [uri for _, uri in drawn_pages.items()]
                    # 无 canvas 时回退 img（非加密源）
                    if not images:
                        imgs = await page.query_selector_all("img")
                        for img in imgs[:200]:
                            try:
                                src = (
                                    await img.get_attribute("data-src")
                                    or await img.get_attribute("src")
                                )
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
                            header, b64 = data_uri.split(",", 1)
                            img_bytes = base64.b64decode(b64)
                            path = out / f"page_{i+1:03d}.jpg"
                            path.write_bytes(img_bytes)
                            saved.append(str(path))
                        else:
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
    wait_until: str = "domcontentloaded",
    timeout_ms: int = 30000,
    extra_delay_ms: int = 2500,
    click_selector: Optional[str] = None,
    scroll_to_bottom: bool = False,
    extract_mode: str = "canvas",
    output_dir: Optional[str] = None,
    proxy: Optional[str] = None,
    page_container_selector: Optional[str] = None,
    scroll_step_px: int = 600,
    scroll_stale_rounds: int = 6,
) -> List[str]:
    """同步版本的 fetch_rendered_images。"""
    return asyncio.run(
        fetch_rendered_images(
            url, wait_for, wait_until, timeout_ms, extra_delay_ms,
            click_selector, scroll_to_bottom, extract_mode, output_dir, proxy,
            page_container_selector, scroll_step_px, scroll_stale_rounds,
        )
    )
