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
import atexit
import base64
import contextlib
import functools
import logging
import os
import queue
import re
import threading
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 浏览器实例复用（阅读/播放提速核心）
# --------------------------------------------------------------------------- #
# Playwright 每次 launch 一个 headless Chromium 约 1-2s，是"每次打开/播放慢"
# 的主要耗时之一（SPA 小说每章、视频播放页每集、漫画每话都在现场启动）。
# 常驻 Chromium 跨调用复用：首次后免启动，秒开。
#
# 注意：Playwright sync API 把事件循环 + greenlet fiber **绑定在首次 start() 的线程**上，
# 跨线程复用或线程销毁后 TID 被复用，会踩已绑定旧线程的 greenlet 循环（曾导致
# hciyuan 播放 0xc0000005 UAF）。因此所有 sync 调用统一收敛到**单条专用渲染线程**
# （见下方专用渲染线程段），浏览器/事件循环生命周期与该线程绑定，彻底消除跨线程 UAF；
# 常驻复用提速收益保留。
_SYNC_REGS: dict = {}
_SYNC_LOCK = threading.Lock()

# --------------------------------------------------------------------------- #
# 专用渲染线程（2026-09 崩溃修复）
# hciyuan 播放 0xc0000005：TID 复用后新线程踩 Playwright sync 绑定的旧线程
# greenlet 事件循环，对已释放对象做槽位替换 → python310.dll+0xc0eeb 读 0x20。
# 根治法：所有 sync Playwright 调用收敛到**单条专用渲染线程**（队列 + Event
# 同步取回结果）。浏览器/事件循环/greenlet 生命周期从此与该线程绑定，
# 进程退出随 daemon 自清，彻底消除跨线程 UAF；常驻复用提速收益保留。
# 代价：sync 类调用全局串行（排队执行），播放取流/正文渲染/搜索等业务并发
# 时按序处理；图片批量渲染走 async API（每次独立浏览器），不经过本线程。
_RENDER_QUEUE: "queue.Queue" = queue.Queue()
_RENDER_THREAD: Optional[threading.Thread] = None
_RENDER_THREAD_LOCK = threading.Lock()
_SHUTDOWN_SENTINEL = object()


def _render_thread_main() -> None:
    """专用渲染线程主循环：串行执行 sync Playwright 任务，退出前关闭自己的浏览器。"""
    tid = threading.get_ident()
    while True:
        item = _RENDER_QUEUE.get()
        if item is _SHUTDOWN_SENTINEL:
            break
        fn, holder = item
        try:
            holder["result"] = fn()
        except BaseException as exc:  # noqa: BLE001
            holder["error"] = exc
        finally:
            holder["done"].set()
    with _SYNC_LOCK:
        entry = _SYNC_REGS.pop(tid, None)
    if entry is not None:
        try:
            entry[1].close()
        except Exception:  # noqa: BLE001
            pass
        try:
            entry[0].stop()
        except Exception:  # noqa: BLE001
            pass


def _ensure_render_thread() -> None:
    """惰性启动专用渲染线程（进程内唯一，不复用销毁，与进程同生命周期）。"""
    global _RENDER_THREAD
    with _RENDER_THREAD_LOCK:
        if _RENDER_THREAD is None or not _RENDER_THREAD.is_alive():
            _RENDER_THREAD = threading.Thread(
                target=_render_thread_main,
                name="claw-render",
                daemon=True,
            )
            _RENDER_THREAD.start()


def run_on_render_thread(fn: Callable[[], object]) -> object:
    """把 fn 投递到专用渲染线程执行并阻塞等待结果（异常跨线程复抛）。

    若渲染线程异常退出，等待方最多 1s 内抛出 RuntimeError，不会永久挂死。
    """
    _ensure_render_thread()
    holder = {"done": threading.Event(), "result": None, "error": None}
    _RENDER_QUEUE.put((fn, holder))
    while True:
        holder["done"].wait(timeout=1.0)
        if holder["done"].is_set():
            break
        thread = _RENDER_THREAD
        if thread is None or not thread.is_alive():
            raise RuntimeError("Playwright 渲染线程异常退出，任务未完成")
    if holder["error"] is not None:
        raise holder["error"]  # 异常对象跨线程复抛，保留原 traceback
    return holder["result"]


def _on_render_thread(fn: Callable) -> Callable:
    """装饰器：把 `*_sync` 函数整体放到专用渲染线程中执行。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return run_on_render_thread(lambda: fn(*args, **kwargs))
    return wrapper


@atexit.register
def _close_sync_browser() -> None:
    """进程退出时通知渲染线程关闭常驻浏览器，join 等待自清。

    关闭动作须与 start() 同线程，故只在渲染线程内执行（收到 sentinel 后
    break 循环，退出前 close+stop）。daemon 线程 + join 超时兜底，最坏
    随进程退出管道断开由 Chromium 自清。
    """
    global _RENDER_THREAD
    thread = _RENDER_THREAD
    if thread is None or not thread.is_alive():
        return
    try:
        _RENDER_QUEUE.put(_SHUTDOWN_SENTINEL)
        thread.join(timeout=5)
    except Exception:  # noqa: BLE001
        pass
    _RENDER_THREAD = None


@contextlib.contextmanager
def _sync_browser(proxy: Optional[str] = None):
    """获取专用渲染线程上 sync Playwright 的连接与浏览器。

    所有 sync 调用经 run_on_render_thread 收敛到**专用渲染线程**执行，故此处
    只由该线程进入：TID 命中的常驻 (pw, browser) 恒为本线程实例，Playwright
    sync 的事件循环/greenlet fiber 自始绑定渲染线程，生命周期与线程一致，
    进程退出时随线程清理（根除原按线程隔离 + 跨线程 atexit close 的 UAF）。

    proxy 指定时在同一条连接上另启带代理的独立浏览器（代理不能复用常驻
    实例，避免污染复用池）；用完仅 close 该浏览器、不 stop 连接（连接仍被
    常驻浏览器使用）。无代理时复用常驻 headless Chromium（跨调用免启动提速）。
    """
    from playwright.sync_api import sync_playwright

    tid = threading.get_ident()
    with _SYNC_LOCK:  # 注册表读写串行；拿到本线程实例后锁即释放
        entry = _SYNC_REGS.get(tid)
        alive = False
        if entry is not None:
            try:
                alive = entry[1].is_connected()
            except Exception:  # 浏览器进程已崩溃/连接异常 → 视为不可用
                alive = False
        if entry is None or not alive:
            if entry is not None:  # Chromium 崩溃自愈：关掉残留重新启动
                _SYNC_REGS.pop(tid, None)
                try:
                    entry[1].close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    entry[0].stop()
                except Exception:  # noqa: BLE001
                    pass
            pw = None
            try:
                pw = sync_playwright().start()
                browser = pw.chromium.launch(
                    headless=True, args=["--no-sandbox"]
                )
            except Exception:
                if pw is not None:
                    try:
                        pw.stop()
                    except Exception:  # noqa: BLE001
                        pass
                raise
            entry = (pw, browser)
            _SYNC_REGS[tid] = entry
    if proxy:
        proxy_browser = entry[0].chromium.launch(
            headless=True,
            args=["--no-sandbox", f"--proxy-server={proxy}"],
        )
        try:
            yield entry[0], proxy_browser
        finally:
            try:
                proxy_browser.close()
            except Exception:  # noqa: BLE001
                pass
        return
    yield entry[0], entry[1]


@contextlib.contextmanager
def _sync_page(browser, viewport: Optional[dict] = None):
    """在复用浏览器上开新 page，退出时自动关闭 page + context。

    复用常驻浏览器时若只关 page 不关 context，上下文会累积泄漏内存；
    统一在此收尾（单独启动的一次性浏览器同样适用）。
    """
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        viewport=viewport or {"width": 1366, "height": 768},
    )
    page = context.new_page()
    try:
        yield page
    finally:
        try:
            page.close()
        finally:
            context.close()


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


async def _eval_img_js_path(page, img_js_path: Optional[str]) -> List[str]:
    """按 img_js_path 求值图片 URL 数组（长条图阅读器的 JS 全局变量）。

    求值结果须为字符串数组（否则返回 []，调用方回退 DOM 提取）。
    """
    if not img_js_path:
        return []
    try:
        val = await page.evaluate(f"() => {img_js_path}")
        if isinstance(val, list):
            out = [str(v) for v in val if isinstance(v, str) and v]
            return out
    except Exception:
        pass
    return []


def _img_url_page_key(src: str) -> tuple | None:
    """从图片 URL 提取页码排序键（数字元组）。

    漫画站（如 manben）图片 URL 常内嵌页码：`.../2_4711.jpg` → (2,)，
    长条图分片 `.../5_1041_1.jpg` → (5,1041,1)（页码+分片序号），
    翻页式双页阅读器会交换 DOM 顺序，需按 URL 数字序列排序恢复阅读顺序。
    返回 None 表示提取不到。
    """
    m = re.search(r"/(?:(\d+)_)+(\d+)?[^/]*(?:\.\w+)?(?:\?|$)", src or "")
    if not m:
        return None
    nums = [int(x) for x in m.group(0).strip("/").replace(".", "_").split("_") if x.isdigit()]
    return tuple(nums) if nums else None


def _img_sort_key(url: str, fallback_idx: int) -> tuple:
    """图片排序键：(0, 页码元组) 优先（恢复翻页式阅读器被交换的顺序），
    (1, DOM 索引) 兜底（无内嵌页码的普通懒加载站）。"""
    n = _img_url_page_key(url)
    if n is not None:
        return (0,) + n
    return (1, fallback_idx)


def _drawn_key_page_num(key: str):
    """从 drawn_pages 键提取页码整数（连续前缀判定用）。

    '897144'（canvas cropped id）→ 897144；'0_000123' / '1_000456'（img 模式
    页码 / DOM 索引）→ 123 / 456；非数字 → None（中断前缀判定）。
    """
    s = key.split("_", 1)[-1] if "_" in key else key
    return int(s) if s.isdigit() else None


def _continuous_prefix_len(sorted_items) -> int:
    """求已收集项的连续前缀长度：sorted 键的数字部分从首个起连续 +1 则计入，
    遇跳跃 / 非数字键停止。on_batch 增量回调用（保证回调始终是连续前缀，
    GUI 侧 _rendered_count 去重后只渲染新增）。
    """
    nums = []
    for key, _ in sorted_items:
        n = _drawn_key_page_num(key)
        if n is None:
            break
        nums.append(n)
    if not nums:
        return 0
    start = nums[0]
    for i, v in enumerate(nums):
        if v != start + i:
            return i
    return len(nums)


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
    wheel_scroll: bool = False,
    img_selector: Optional[str] = None,
    img_js_path: Optional[str] = None,
    on_batch: Optional[Callable[[List[str]], None]] = None,
) -> List[str]:
    """用 Playwright 渲染页面，提取内容。

    返回内容列表：base64 data URI / 图片 URL / 文本块，取决于 extract_mode。
    on_batch：可选增量回调 on_batch(连续前缀列表)——滚动懒加载中边滚边把已就绪
    的连续前缀回调出去（首图几秒内即出，后续边滚边补），GUI 可边收边渲染；
    最终仍返回完整列表，on_batch 只是增量前置预览。

    参数（源配置 render_config 可覆盖全部）：
        wait_for:          渲染目标选择器（如 "canvas" / ".img"）
        wait_until:        页面就绪策略（"domcontentloaded" / "networkidle" / "load"）
        extra_delay_ms:    渲染后额外等待（ms）
        click_selector:    可选：渲染后点击该元素（展开弹层/触发懒加载）
        scroll_to_bottom:  可选：滚动到页底，触发滚动懒加载
        wheel_scroll:      可选：true 时用 mouse.wheel 触发滚动/翻页（翻页式阅读器
            靠 wheel 事件翻页加载后续图片，window.scrollBy 不派发 wheel 事件）；
            false 时保持原 window.scrollBy 行为（canvas 懒加载站默认）。
        extract_mode:      提取方式
            "canvas"  → 收集所有 canvas 的 toDataURL（加密分片图合并结果）
            "img"     → 收集所有 img 的 data-src/src（普通图片 URL）。
                        img_selector 给出时只收集匹配元素（过滤 UI 图标等非正文图）
            "text"    → 收集页面 body 文本（JS 渲染出的正文）
        img_selector:      可选：extract_mode="img" 时限定收集的选择器（如 "img.indexImg"）
        img_js_path:       可选：extract_mode="img" 时的 JS 表达式，求值为图片 URL 数组
            （如 "window.newImgs"）。长条图阅读器把全部图片 URL 放在 JS 全局变量里，
            window.onload 才构建 DOM，求值全局数组可立即拿到（无需等 onload）；
            求值失败/非数组/空 → 回退 DOM 提取（img_selector）。
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
                # 快路径：img_js_path 全局数组（长条图阅读器）立即拿到全部图 URL，
                # 无需等 window.onload 构建 DOM。domcontentloaded 后同步脚本已执行，
                # 单次求值即可；非该类型章节该全局未定义 → 走常规 DOM 流程。
                js_imgs_fast: List[str] = []
                if img_js_path:
                    js_imgs_fast = await _eval_img_js_path(page, img_js_path)
                    if js_imgs_fast and output_dir:
                        out = Path(output_dir)
                        out.mkdir(parents=True, exist_ok=True)
                        saved = []
                        for i, u in enumerate(js_imgs_fast):
                            if u.startswith("data:"):
                                header, b64 = u.split(",", 1)
                                p = out / f"page_{i+1:03d}.jpg"
                                p.write_bytes(base64.b64decode(b64))
                                saved.append(str(p))
                            else:
                                saved.append(u)
                        return saved
                    if js_imgs_fast:
                        return js_imgs_fast
                # 等渲染目标出现（state=attached：元素挂载即算，不等 visible——
                # 翻页/懒加载阅读器的正文图初始常为隐藏/占位，等待挂载更可靠）
                try:
                    await page.wait_for_selector(wait_for, timeout=12000, state="attached")
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
                drawn_pages: dict = {}  # {cropped_id: data_uri} 或 {idx: img_src}
                if scroll_to_bottom:
                    last_emitted_len = 0  # 已增量回调的连续前缀长度（on_batch 去重）

                    def _emit_on_batch() -> None:
                        """drawn_pages 连续前缀增长 → 增量回调（边滚边补，GUI 边收边渲染）。"""
                        nonlocal last_emitted_len
                        if not on_batch or not drawn_pages:
                            return
                        sorted_items = sorted(
                            drawn_pages.items(),
                            key=lambda kv: _page_sort_key(kv[0]),
                        )
                        prefix_len = _continuous_prefix_len(sorted_items)
                        if prefix_len > last_emitted_len:
                            on_batch([uri for _, uri in sorted_items[:prefix_len]])
                            last_emitted_len = prefix_len

                    async def _collect_drawn() -> None:
                        if extract_mode == "img":
                            # img 模式（翻页式阅读器）：收集已加载正文图的 src。
                            # 用于驱动 early-break（新图不再增长即到底），也累积最终结果。
                            # 排序键：URL 内嵌页码优先（0_NNNNNN…，恢复被交换的顺序），
                            # 否则 DOM 索引（1_NNNNNN）。
                            sel = img_selector or "img"
                            nodes = await page.query_selector_all(sel)
                            for i, img in enumerate(nodes):
                                try:
                                    src = (
                                        await img.get_attribute("data-src")
                                        or await img.get_attribute("src")
                                    )
                                    if src:
                                        nums = _img_url_page_key(src)
                                        if nums:
                                            key = "0_" + "_".join(
                                                f"{x:06d}" for x in nums)
                                        else:
                                            key = f"1_{i:06d}"
                                        drawn_pages[key] = src
                                except Exception:
                                    pass
                            return
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
                    # wheel_scroll=true 时用 mouse.wheel 派发 wheel 事件（翻页式阅读器
                    # 靠 wheel 事件翻页加载后续图片）；false 保持 window.scrollBy。
                    step = max(scroll_step_px, 1000)
                    no_new_pages = 0
                    for _scroll_step in range(120):
                        if wheel_scroll:
                            await page.mouse.wheel(0, step)
                        else:
                            await page.evaluate(f"window.scrollBy(0, {step})")
                        await page.wait_for_timeout(80)  # 80ms/步，更快
                        prev_count = len(drawn_pages)
                        await _collect_drawn()
                        if len(drawn_pages) > prev_count:
                            no_new_pages = 0
                            _emit_on_batch()  # 边滚边回调连续前缀
                        else:
                            no_new_pages += 1
                        # 连续 10 步无新页 → 已近末尾，提前结束滚动
                        if no_new_pages >= 10:
                            break
                    # 到底/结束：短等尾部 canvas 集中绘制，再补一轮收集 + 回调
                    await page.wait_for_timeout(2500)
                    await _collect_drawn()
                    _emit_on_batch()

                # 给 JS 绘制留时间
                await page.wait_for_timeout(extra_delay_ms)


                # 按 extract_mode 分派提取
                if extract_mode == "text":
                    # JS 渲染出的正文/文本
                    text = await page.evaluate("() => document.body.innerText")
                    images = [t.strip() for t in (text or "").splitlines() if t.strip()] or []
                elif extract_mode == "img":
                    # 普通图片 URL（懒加载 data-src / src）。
                    # 优先 JS 全局数组（长条图阅读器，如 window.newImgs，免等 onload）；
                    # 其次 scroll_to_bottom 已边滚边收集（翻页式阅读器）→ 用累积结果，
                    # 键 "0_NNNNNN"（URL 页码）/"1_NNNNNN"（DOM 索引）字典序即阅读顺序
                    # （页码优先，恢复双页阅读器被交换的顺序）；否则直接查询当前 DOM。
                    js_imgs = await _eval_img_js_path(page, img_js_path)
                    if js_imgs:
                        images = js_imgs
                    elif drawn_pages:
                        images = [src for _, src in sorted(drawn_pages.items())]
                    else:
                        sel = img_selector or "img"
                        imgs = await page.query_selector_all(sel)
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
    wheel_scroll: bool = False,
    img_selector: Optional[str] = None,
    img_js_path: Optional[str] = None,
    on_batch: Optional[Callable[[List[str]], None]] = None,
) -> List[str]:
    """同步版本的 fetch_rendered_images（on_batch 透传增量前缀回调）。"""
    return asyncio.run(
        fetch_rendered_images(
            url, wait_for, wait_until, timeout_ms, extra_delay_ms,
            click_selector, scroll_to_bottom, extract_mode, output_dir, proxy,
            page_container_selector, scroll_step_px, scroll_stale_rounds,
            wheel_scroll, img_selector, img_js_path, on_batch,
        )
    )


async def _render_one_url(
    page,
    url: str,
    wait_for: str,
    wait_until: str,
    timeout_ms: int,
    extra_delay_ms: int,
    scroll_to_bottom: bool,
    extract_mode: str,
    wheel_scroll: bool = False,
    img_selector: Optional[str] = None,
    img_js_path: Optional[str] = None,
):
    """在给定 page（复用浏览器）上渲染单个 URL 并提取。

    由 fetch_rendered_pages_batch 调用；单次 goto 一个 URL，收集该页全部图片
    base64 URI（canvas 合并结果）。滚到底边滚边收集，末段深等。
    """
    await page.goto(url, timeout=timeout_ms, wait_until=wait_until)
    # 快路径：img_js_path 全局数组立即拿到全部图 URL（长条图阅读器）
    if img_js_path:
        js_imgs = await _eval_img_js_path(page, img_js_path)
        if js_imgs:
            return js_imgs
    try:
        await page.wait_for_selector(wait_for, timeout=12000, state="attached")
    except Exception:
        pass

    drawn: dict = {}  # {cropped_id: data_uri} 或 {idx: img_src}
    if scroll_to_bottom:
        async def _collect_drawn() -> None:
            if extract_mode == "img":
                sel = img_selector or "img"
                nodes = await page.query_selector_all(sel)
                for i, img in enumerate(nodes):
                    try:
                        src = (
                            await img.get_attribute("data-src")
                            or await img.get_attribute("src")
                        )
                        if src:
                            nums = _img_url_page_key(src)
                            if nums:
                                key = "0_" + "_".join(
                                    f"{x:06d}" for x in nums)
                            else:
                                key = f"1_{i:06d}"
                            drawn[key] = src
                    except Exception:
                        pass
                return
            items = await page.evaluate(
                """() => {
                    const cs = Array.from(document.querySelectorAll('canvas'))
                        .filter(c => c.width > 20 && c.height > 20);
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
                drawn[cid] = uri

        step = 1500
        no_new = 0
        for _ in range(80):
            if wheel_scroll:
                await page.mouse.wheel(0, step)
            else:
                await page.evaluate(f"window.scrollBy(0, {step})")
            await page.wait_for_timeout(50)
            prev = len(drawn)
            await _collect_drawn()
            no_new = 0 if len(drawn) > prev else no_new + 1
            if no_new >= 6:
                break
        await page.wait_for_timeout(1000)
        await _collect_drawn()

    await page.wait_for_timeout(extra_delay_ms)

    # 提取
    images: List[str] = []
    if extract_mode == "text":
        text = await page.evaluate("() => document.body.innerText")
        images = [t.strip() for t in (text or "").splitlines() if t.strip()] or []
    elif extract_mode == "img":
        js_imgs = await _eval_img_js_path(page, img_js_path)
        if js_imgs:
            images = js_imgs
        elif drawn:
            images = [src for _, src in sorted(drawn.items())]
        else:
            sel = img_selector or "img"
            imgs = await page.query_selector_all(sel)
            for img in imgs[:500]:
                try:
                    src = await img.get_attribute("data-src") or await img.get_attribute("src")
                    if src:
                        images.append(src)
                except Exception:
                    pass
    elif drawn:
        images = [uri for _, uri in sorted(drawn.items(), key=lambda kv: _page_sort_key(kv[0]))]
    else:
        # 未滚动：等待并提取当前可见 canvas
        collected = await page.evaluate(
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
        for cid, uri in collected:
            drawn[cid] = uri
        images = [uri for _, uri in drawn.items()]
    return images


async def _fetch_pages_batch(
    urls: List[str],
    render_cfg: dict,
    per_url_cfg: dict,
) -> List[object]:
    """一次浏览器调度，逐 URL 渲染。返回常与 urls 等长（单话失败用无异常标记）。"""
    from playwright.async_api import async_playwright

    def _cfg(c, scope):
        return (scope.get(c) if scope.get(c) is not None else render_cfg.get(c)) or "canvas"

    results = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context_opts = {
                "user_agent": _default_user_agent(),
                "viewport": {"width": 900, "height": 2000},
            }
            used_proxy = _pick_proxy(render_cfg.get("proxy"))
            if used_proxy:
                context_opts["proxy"] = {"server": used_proxy}
            context = await browser.new_context(**context_opts)
            page = await context.new_page()
            try:
                for url in urls:
                    per = per_url_cfg.get(url, {})
                    cfg = {
                        "wait_for": per.get("wait_for", render_cfg.get("wait_for", "canvas")),
                        "wait_until": per.get("wait_until", render_cfg.get("wait_until", "domcontentloaded")),
                        "timeout_ms": int(per.get("timeout_ms", render_cfg.get("timeout_ms", 30000))),
                        "extra_delay_ms": int(per.get("extra_delay_ms", render_cfg.get("extra_delay_ms", 2500))),
                        "scroll_to_bottom": per.get("scroll_to_bottom", render_cfg.get("scroll_to_bottom", False)),
                        "extract_mode": per.get("extract_mode", render_cfg.get("extract_mode", "canvas")),
                        "wheel_scroll": per.get("wheel_scroll", render_cfg.get("wheel_scroll", False)),
                        "img_selector": per.get("img_selector", render_cfg.get("img_selector")),
                        "img_js_path": per.get("img_js_path", render_cfg.get("img_js_path")),
                    }
                    try:
                        imgs = await _render_one_url(page, url, **cfg)
                        # 记录是否成功（失败以 None 标记由调用方决定）
                        results[url] = imgs if imgs else None
                    except Exception as exc:  # noqa: BLE001
                        log.warning("[playwright] 批量渲染失败 %s: %s", url, exc)
                        results[url] = None
            finally:
                await page.close()
                await context.close()
        finally:
            await browser.close()
    return results


def fetch_rendered_pages_batch_sync(
    urls: List[str],
    render_cfg: dict,
    per_url_cfg: Optional[dict] = None,
) -> dict:
    """批量渲染多个 URL，复用同一个 Chromium 实例。

    返回 {url: [base64 data URI 列表]}；单个 URL 渲染失败 → 值为 None。
    相比逐话 fetch_rendered_images_sync（每话启动一次浏览器），
    把 N 话渲染收敛到 1 次浏览器启动，大幅降低下载耗时与内存。
    """
    per_url_cfg = per_url_cfg or {}
    return asyncio.run(_fetch_pages_batch(urls, render_cfg, per_url_cfg))


# ------------------------------------------------------------------ #
# 渲染后提取正文文本（SPA 小说站用）
# ------------------------------------------------------------------ #
@_on_render_thread
def evaluate_js_sync(expr: str) -> object:
    """在常驻浏览器中用 evaluate 执行一段 JS 表达式并取回结果。

    供自定义解密（js_custom 策略）等需要 JS 引擎的场景使用；
    不需要真实页面渲染，仅借用 Chromium 的 JS 引擎。表达式按原样
    evaluate（Playwright 的 evaluate 会把字符串当表达式求值）：
    - 纯表达式如 "1+2" → 3
    - IIFE / 顶层语句已封装由调用方构造（如 "(function(){...})()"）
    返回值经 Playwright 序列化返回（str/number/bool/list/dict）。
    """
    with _sync_browser() as (p, browser):
        with _sync_page(browser) as page:
            return page.evaluate(expr)


@_on_render_thread
def fetch_rendered_text_sync(
    url: str,
    selector: str,
    wait_for: str = "",
    wait_until: str = "domcontentloaded",
    timeout_ms: int = 30000,
    extra_delay_ms: int = 2000,
    proxy: Optional[str] = None,
) -> str:
    """用 Playwright 同步 API 渲染页面后，按 CSS selector 提取全部段落文本。

    供 SPA 小说站（如 18mh）正文在 JS 渲染的页面用。
    使用同步 API（部分站对 async headless 有反爬，sync API 指纹不同）。
    返回 selector 命中节点的合并文本（\\n 分隔），无命中返回 ""。
    """
    with _sync_browser(proxy) as (p, browser):
        with _sync_page(browser) as page:
            page.goto(url, timeout=timeout_ms, wait_until=wait_until)
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=12000)
                except Exception:
                    pass
            page.wait_for_timeout(extra_delay_ms)
            nodes = page.query_selector_all(selector)
            texts = []
            for n in nodes:
                t = (n.inner_text() or "").strip()
                if t:
                    texts.append(t)
            return "\n".join(texts)


@_on_render_thread
def fetch_rendered_items_sync(
    url: str,
    root_selector: str,
    wait_for: str = "",
    wait_until: str = "networkidle",
    timeout_ms: int = 30000,
    extra_delay_ms: int = 2500,
    proxy: Optional[str] = None,
) -> list:
    """用 Playwright 渲染页面，按 root_selector 提取每项的 title/href/src。

    供反爬 SPA 站搜索（如 fdzys 搜索结果由 JS 动态渲染、CSS 类名混淆）。
    返回 [{title, href, src, text}]，每项取 root 元素内的：
      - title: 首选 a[title]，否则 img[alt]，否则元素文本
      - href : 首选 a[href]，否则任意 a[href] 链接
      - src  : img 的 src / data-src / data-original
    无命中返回 []。
    """
    with _sync_browser(proxy) as (p, browser):
        with _sync_page(browser) as page:
            page.goto(url, timeout=timeout_ms, wait_until=wait_until)
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=15000)
                except Exception:
                    pass
            page.wait_for_timeout(extra_delay_ms)
            roots = page.query_selector_all(root_selector)
            items = []
            for node in roots:
                item = {"title": "", "href": "", "src": "", "text": ""}
                try:
                    title_el = node.query_selector("a[title]") or node.query_selector("img[alt]")
                    if title_el:
                        t = title_el.get_attribute("title") or title_el.get_attribute("alt") or ""
                        item["title"] = (t or "").strip()
                    if not item["title"]:
                        a = node.query_selector("a")
                        if a:
                            item["title"] = (a.inner_text() or "").strip()
                    a = node.query_selector("a[href]")
                    if a:
                        item["href"] = (a.get_attribute("href") or "").strip()
                    img = node.query_selector("img")
                    if img:
                        for attr in ("src", "data-src", "data-original"):
                            v = img.get_attribute(attr)
                            if v:
                                item["src"] = v.strip()
                                break
                    item["text"] = (node.inner_text() or "").strip()
                except Exception:  # noqa: BLE001
                    pass
                if item["href"]:
                    items.append(item)
            return items


@_on_render_thread
def fetch_rendered_search_sync(
    home_url: str,
    keyword: str,
    input_selector: str = "input[name='wd'], input[name='searchword']",
    result_selector: str = "",
    wait_until: str = "networkidle",
    timeout_ms: int = 30000,
    extra_delay_ms: int = 3000,
    proxy: Optional[str] = None,
) -> list:
    """Playwright 交互式搜索：先访问首页，在搜索框输入关键词并提交，
    等待 JS 渲染出真实结果后再提取。

    适用：搜索框提交后由 JS 动态加载结果（如 fdzys），直接 GET /search?wd=
    只会拿到热门榜。返回 fetch_rendered_items_sync 同构的 [{title,href,src,text}]。
    """
    with _sync_browser(proxy) as (p, browser):
        with _sync_page(browser) as page:
            page.goto(home_url, timeout=timeout_ms, wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            # 输入关键词并提交
            try:
                page.fill(input_selector, keyword)
                page.press(input_selector, "Enter")
            except Exception:
                # fill 失败（输入框动态加载）→ 退化为直接访问搜索 URL
                from urllib.parse import quote

                page.goto(home_url.rstrip("/") + f"/search?wd={quote(keyword)}",
                          timeout=timeout_ms, wait_until=wait_until)
            page.wait_for_timeout(extra_delay_ms)
            # 提取结果
            roots = page.query_selector_all(result_selector) if result_selector else page.query_selector_all("a[href]")
            items = []
            seen_href = set()
            for node in roots:
                try:
                    item = {"title": "", "href": "", "src": "", "text": ""}
                    a = node if node.evaluate("e => e.tagName === 'A'") else node.query_selector("a[href]")
                    if not a:
                        continue
                    href = a.get_attribute("href") or ""
                    if not href or href in seen_href:
                        continue
                    title = a.get_attribute("title") or (a.inner_text() or "").strip()
                    if not title:
                        continue
                    seen_href.add(href)
                    item["href"] = href
                    item["title"] = title.strip()
                    img = node.query_selector("img")
                    if img:
                        for attr in ("src", "data-src", "data-original"):
                            v = img.get_attribute(attr)
                            if v:
                                item["src"] = v.strip()
                                break
                    item["text"] = (node.inner_text() or "").strip()
                    items.append(item)
                except Exception:  # noqa: BLE001
                    continue
            return items


@_on_render_thread
def fetch_rendered_video_sync(
    url: str,
    wait_until: str = "networkidle",
    timeout_ms: int = 45000,
    extra_delay_ms: int = 8000,
    proxy: Optional[str] = None,
) -> str:
    """渲染播放页，等 iframe 解析站执行后，提取真实视频源（video src）。

    适用：播放页用 iframe 嵌套第三方解析站（如 agedm），解析站 JS
    解密后填充真实视频 URL 到 iframe 的 <video>。返回 video src；失败 ""。
    """
    try:
        with _sync_browser(proxy) as (p, browser):
            with _sync_page(browser) as page:
                # 捕获网络层真实媒体请求（blob/MSE 播放器的 m3u8/mp4 直链）。
                # 部分解析站（voe.sx/eugenemakedraw 等 JWPlayer）会把 HLS 源塞进
                # blob: URL，video src 拿不到 http 直链，但播放器会向 CDN 请求
                # master.m3u8/具体 .mp4 —— 从这些请求里取真实可播地址。
                media_hits: List[str] = []

                def _on_request(request):
                    try:
                        u = request.url
                        low = u.lower()
                        if ".m3u8" in low or (".mp4" in low and "ping" not in low):
                            media_hits.append(u)
                    except Exception:
                        pass

                page.on("request", _on_request)
                page.goto(url, timeout=timeout_ms, wait_until=wait_until)
                page.wait_for_timeout(extra_delay_ms)
                # 遍历全部 frame（含 iframe 解析站）找 video src
                for _ in range(3):  # 多轮重试等解析站渲染
                    for f in page.frames:
                        try:
                            vids = f.query_selector_all("video")
                            found_mp4 = ""
                            for v in vids:
                                # videojs/VHS 会把 HLS 渲染成 blob: src（不以 http 开头，
                                # 原逻辑直接跳过），真实 m3u8 留在 <video> 的 <source> 子节点
                                # 上（5238 播放页）。因此同时读 video 自身 src 与其全部
                                # <source> 子节点 src：优先 .m3u8（HLS 稳定地址），
                                # 再兜底 .mp4（带签名时效的预览变体）。
                                for src in (v.get_attribute("src") or "",
                                            *(sv.get_attribute("src") or ""
                                              for sv in v.query_selector_all("source"))):
                                    if not src.startswith("http"):
                                        continue
                                    if ".m3u8" in src:
                                        return src
                                    if not found_mp4:
                                        found_mp4 = src
                            if found_mp4:
                                return found_mp4
                        except Exception:
                            continue
                    # video src 为 blob 的播放器：等待其真正发出的媒体请求出现
                    if media_hits:
                        return media_hits[0]
                    page.wait_for_timeout(4000)
                if media_hits:
                    return media_hits[0]
                return ""
    except Exception as exc:  # noqa: BLE001
        # 内部异常（greenlet.error / TimeoutError / playwright 错误）归一化为
        # 无视频源（""），不把内部异常抛给上层 fetch_video_episode。
        log.warning("[playwright] 渲染播放页失败 %s: %s", url, exc)
        return ""
