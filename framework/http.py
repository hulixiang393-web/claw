"""HTTP 客户端（http.py）。

消费 transports 配置（base_url/headers/proxy/timeout/retries/interval_ms）。
优先 requests，未装则 urllib 降级。异常统一包装为 RequestError。
对应 design.md §5 三层架构的「执行引擎」层。

全局默认值：HttpClient(defaults=NetworkDefaults(...)) 由 App 层从 settings 构建，
调用方未传参时兜底（对应 app_config.network.*）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Optional

from .errors import RequestError
from .proxy_pool import ProxyPool

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 SpiderFramework/1.0"
)

# 用户手动过 Cloudflare 验证后的 cookie 存储：data/cf_cookies.json
# 格式：{"www.5238.me": {"cf_clearance": "...", "__cf_bm": "..."}}
# 请求 5238 等被 CF 拦截的源时自动注入，纯 HTTP 直连过反爬。
_CF_COOKIES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cf_cookies.json"
)
_cf_cookies_cache: Optional[dict] = None


# --------------------------------------------------------------------------- #
# IPv6 黑洞兜底（框架级，惠及所有源）
# --------------------------------------------------------------------------- #
# 背景：部分机器 IPv6 路由不通——DNS 正常返回 AAAA，但连上任一 IPv6 地址都要
# 等到 TCP 超时（实测 20s+）。urllib3 默认 gai_family=AF_UNSPEC（AAAA 优先），
# 于是「首次直连请求」先白等一轮 IPv6 再回落 IPv4，实测每个进程首次请求慢
# 53~103s；走系统代理时由代理解析，所以慢只发生在 direct 请求上。
#
# 对策：进程启动时用短超时探测一次 IPv6 可达性；不可达就把 urllib3 的地址族
# 固定成 IPv4，直连请求恢复正常速度（实测 103s → 2.7s）。IPv6 正常时不干预。
# 探测失败（异常/无 urllib3）一律保持原状，绝不影响请求可行性。
_IPV4_FORCED = False


def _ipv6_reachable(timeout: float = 0.3) -> bool:
    """IPv6 外网是否可达（连公共 DNS over TLS 的 TCP 即可，不需要真握手）。"""
    import socket

    # Cloudflare / Google 的公共 IPv6 DNS，任一可达即认为 IPv6 通
    for host in ("2606:4700:4700::1111", "2001:4860:4860::8888"):
        try:
            with socket.create_connection((host, 443), timeout=timeout):
                return True
        except OSError:
            continue
        except Exception:  # noqa: BLE001 —— 地址族不支持等也按不可达处理
            return False
    return False


def _force_ipv4_if_ipv6_broken() -> None:
    """IPv6 不可达时把 urllib3 地址族固定为 IPv4。

    设 CLAW_DISABLE_IPV4_FORCE=1 可整体跳过（A/B 排查/个别站点需要 IPv6 时）。
    """
    global _IPV4_FORCED
    if _IPV4_FORCED:
        return
    if os.environ.get("CLAW_DISABLE_IPV4_FORCE") == "1":
        return
    try:
        if _ipv6_reachable():
            return
        from urllib3.util import connection as _u3_connection
        import socket as _socket

        _u3_connection.allowed_gai_family = lambda: _socket.AF_INET
        _IPV4_FORCED = True
    except Exception:  # noqa: BLE001 —— 探测/打补丁失败不影响请求
        _IPV4_FORCED = False


_force_ipv4_if_ipv6_broken()


def _load_cf_cookies() -> dict:
    """读取 data/cf_cookies.json（带缓存）。文件不存在/损坏返回空 dict。"""
    global _cf_cookies_cache
    if _cf_cookies_cache is None:
        try:
            if os.path.exists(_CF_COOKIES_PATH):
                with open(_CF_COOKIES_PATH, encoding="utf-8") as f:
                    _cf_cookies_cache = json.load(f) or {}
            else:
                _cf_cookies_cache = {}
        except Exception:  # noqa: BLE001
            _cf_cookies_cache = {}
    return _cf_cookies_cache


def _cf_cookies_for(url: str) -> Optional[str]:
    """取 url 对应域名的 cf_clearance cookie 串（含 __cf_bm）；无则 None。"""
    try:
        from urllib.parse import urlsplit

        host = (urlsplit(url).hostname or "").lower()
        if not host:
            return None
        cf = _load_cf_cookies()
        entry = cf.get(host) or {}
        # 兼容 "www.5238.me" 与裸域 "5238.me" 两种 key
        if not entry and host.startswith("www."):
            entry = cf.get(host[4:]) or {}
        parts = [f"{k}={v}" for k, v in entry.items() if v]
        return "; ".join(parts) if parts else None
    except Exception:  # noqa: BLE001
        return None


def _with_cf_cookies(headers: Optional[dict], url: str) -> dict:
    """注入 cf_clearance 等 cookie 到请求头（仅当该域名有配置且未显式带 Cookie）。"""
    out = dict(headers or {})
    if "Cookie" in out:
        return out
    cf = _cf_cookies_for(url)
    if cf:
        out["Cookie"] = cf
    return out


def _latin1_header_value(value):
    """把请求头值规范为可被 latin-1 编码的字符串。

    requests/urllib 用 latin-1 编码 header 值；含中文的 Referer（章节 URL 未
    百分号编码，如 ho5ho 的 /中字h漫/...）会抛 UnicodeEncodeError，导致整个
    请求失败（实测 ho5ho 正文图全部「加载失败」）。对非 latin-1 字符按 URL
    规则百分号编码后发送——服务器解码 Referer 后仍是同一 URL，语义等价。
    """
    if not isinstance(value, str):
        return value
    try:
        value.encode("latin-1")
        return value
    except UnicodeEncodeError:
        from urllib.parse import quote

        return quote(value, safe=":/?#[]@!$&'()*+,;=%~")


# --------------------------------------------------------------------------- #
# 系统代理健康探测（V2「兼顾需要/不需要 VPN 的站点」）
# --------------------------------------------------------------------------- #
# 背景：requests 默认 trust_env=True 会把每次请求隐式交给 Windows 注册表/环境
# 变量的系统代理（如加速器 127.0.0.1:65532）。加速器/VPN 关闭后端口已死，但注册
# 表代理项仍在 → 所有请求连向不存在的代理 → **国内直连源也打不开**（"不开 VPN
# 就打不开网站"的根因）。
#
# 对策：系统代理做**端口探活**，活着才用，死了自动直连（国内站恢复可用）；
# 开着 VPN/加速器时端口存活 → 走代理（海外需要 VPN 的站可用且更快）。
# 显式 proxy / 源 transports.proxy / 全局 network.proxy 永远是第一优先，不探测。
_SYSTEM_PROXY_TTL = 10.0  # 探测结果缓存秒数；切换 VPN/加速器后最多延迟这么久自动生效


def _port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    """快速探测 host:port 是否有服务在监听（超时短，避免僵死代理拖慢请求）。"""
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _is_loopback(host: str) -> bool:
    """本机回环地址判定（只有本机代理才值得探活；远程内网代理跳过，避免误杀）。"""
    return host in ("localhost", "127.0.0.1", "::1", "")


def _parse_proxy_host_port(url: str):
    """从代理 URL 提取 (host, port)；无法解析返回 (None, None)（视为不可探测）。"""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url if "://" in url else f"//{url}")
        host = parts.hostname
        port = parts.port
        if port is None:
            scheme = (parts.scheme or "http").lower()
            port = 443 if scheme == "https" else (1080 if scheme.startswith("socks") else 80)
        return (host, port) if host else (None, None)
    except Exception:  # noqa: BLE001 —— 非法代理 URL 一律不探活、不采用
        return (None, None)


def _system_proxy_candidates() -> list:
    """系统代理候选（去重保序）：环境变量 HTTPS/HTTP → Windows 注册表。

    与 requests 的 getproxies 同源（本机「加速器开关」主要落在注册表
    Internet Settings 项，环境变量为空时注册表仍在）。
    """
    import urllib.request

    candidates = []
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = os.environ.get(var)
        if val:
            candidates.append(val)
    try:
        proxies = urllib.request.getproxies()
    except Exception:  # noqa: BLE001 —— 读注册表失败凑请求不受影响
        proxies = {}
    for key in ("https", "http"):
        val = proxies.get(key)
        if val:
            candidates.append(val)
    seen, out = set(), []
    for cand in candidates:
        norm = (cand or "").strip().rstrip("/")
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


_SYSTEM_PROXY_CACHE: dict = {"t": 0.0, "url": None}
_SYSTEM_PROXY_LOCK = threading.Lock()

# 「端口活着但转发不出去」的僵尸代理记忆。
# 端口探活只能证明"有人在听"，证明不了"能把请求送出去"：加速器开着但上游
# 断了、或 65532 被别的程序占用时，探活通过 → 所有请求都交给它 → 每个请求
# 读超时（实测 44s）→ 界面整片空白、播放器也拿不到流。所以这里记下"走系统
# 代理失败过"的主机：同一主机后续直接绕过代理；不同主机连续失败到阈值，就把
# 系统代理整体停用一段时间（到期自动重试，VPN 恢复后自己会回来）。
_PROXY_FAIL_TTL = 900.0
_PROXY_FAIL_HOSTS = 3
_PROXY_FAIL: dict = {"hosts": set(), "until": 0.0}
# 「只有代理能通」的主机记忆（与 _PROXY_FAIL 互为反面）：
# _PROXY_FAIL 记"代理不行、直连行"，_PROXY_OK 记"直连不行、代理行"。
# 两个方向的出口偏好都带 TTL，网络变化后自动重新决策。
_PROXY_OK_TTL = 600.0
_PROXY_OK: dict = {}
# 代理最近一次成功转发的时刻。整体停用只在"代理确实一个都送不出去"时成立：
# 只要近期有任一请求经代理成功，就不许因为几台主机"直连更快"把整个代理关掉
# （否则浏览一批国内站就会把需要代理的站一起停用 3 分钟）。
_PROXY_LAST_OK = {"t": 0.0}
_PROXY_LIVE_WINDOW = 120.0
# 系统代理首次尝试的最长等待：僵尸代理要尽快暴露，别把整页请求耗满 retries×timeout。
# 取 8s：本机加速器实测正常转发常在 4~6s（pornhub/xvideos 首包 6.3s），
# 5s 会把"慢但可用"的代理误判成僵尸，进而错误地把主机记为"必须直连"。
_PROXY_PROBE_TIMEOUT = 8.0
# 「探测失败、直连也不通」时给代理的最后一次机会的超时上限（单次、不重试）：
# 按满超时×重试重跑会把一次请求拖到几十秒，而出口明显已经不健康的概率更高。
_PROXY_RETRY_TIMEOUT = 12.0


def _host_of_url(url: str) -> str:
    """取 URL 主机名（小写）；解析失败返回空串。"""
    from urllib.parse import urlsplit

    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:  # noqa: BLE001 —— 畸形 URL 不参与僵尸代理记忆
        return ""


def note_proxy_preferred(url: str) -> None:
    """把 URL 所属主机标记为「必须走代理」（源配置 transports.prefer_proxy）。

    有些站的直连是**硬失败**：TLS 握手被拒/重置，每次都要等十几秒才报错，
    而经系统代理 200（omofuna 详情页实测：直连 3/3 次 SSLError 15~21s，
    代理可用）。源声明后直接走 `_proxy_preferred` 分支，省掉「一次 8s 探测 +
    一次注定失败的直连预算」。
    """
    host = _host_of_url(url)
    if host:
        _note_proxy_success(host)


def note_direct_preferred(url: str) -> None:
    """把 URL 所属主机标记为「走系统代理不通、直接连」（transports.prefer_direct）。

    与 note_proxy_preferred 相反：有些站经代理是硬失败（全本实测代理 TLS
    6.19s 报错、直连 4s 正常），而框架默认先探代理再回落，每次都要多付一次
    探测/失败预算。源声明后直接按主机绕过代理。
    """
    host = _host_of_url(url)
    if not host:
        return
    with _SYSTEM_PROXY_LOCK:
        _PROXY_FAIL["hosts"].add(host)
        _SYSTEM_PROXY_CACHE["t"] = 0.0
        _SYSTEM_PROXY_CACHE["url"] = None


def system_proxy_url() -> Optional[str]:
    """当前可用的系统代理 URL（探测结果，带 TTL 缓存）；None = 只能直连。

    给需要「换出口重试」的调用方用（典型：第三方播放器解析站，直连和代理
    哪个能用会在一天之内翻转，见 framework/external_player.py）。拿到 URL 后
    作为显式 proxy 传回 get_text，即可强制走代理——显式 proxy 优先于
    「该主机已知必须直连」的记忆，否则换出口重试会被自己的记忆拦掉。
    """
    try:
        return _live_system_proxy()
    except Exception:  # noqa: BLE001 —— 探测失败按"没有代理"处理
        return None


def _proxy_failed_for(host: str) -> bool:
    """该主机是否已被判定"走系统代理不通"。"""
    if not host:
        return False
    with _SYSTEM_PROXY_LOCK:
        return host in _PROXY_FAIL["hosts"]


def _is_http_status_error(exc) -> bool:
    """异常是否为"服务端已明确答复"的 HTTP 状态错误（4xx/5xx）。

    这类失败与出口（代理/直连）无关：代理转发回来的 404 换个出口仍是 404。
    用它把"代理正常但页面不存在"从僵尸代理证据里剔除，否则单页 404 就会把
    整个主机的请求强制改直连（需要代理的站随即打不开）。
    """
    seen = 0
    cur = exc
    while cur is not None and seen < 5:
        resp = getattr(cur, "response", None)
        code = getattr(resp, "status_code", None)
        if code is None:
            code = getattr(cur, "code", None)
        if isinstance(code, int) and code >= 400:
            return True
        if re.search(r"\b(?:4\d{2}|5\d{2})\b[^\n]{0,24}Error", f"{type(cur).__name__}: {cur}"):
            return True
        cur = cur.__cause__ or cur.__context__
        seen += 1
    return False


def _proxy_recently_ok() -> bool:
    """近期是否有请求经系统代理成功转发（有则不允许整体停用代理）。"""
    with _SYSTEM_PROXY_LOCK:
        return (time.monotonic() - _PROXY_LAST_OK["t"]) < _PROXY_LIVE_WINDOW


def _mark_proxy_live() -> None:
    """记一次代理成功转发（用于阻止"浏览国内站就把代理整体关掉"的级联停用）。"""
    with _SYSTEM_PROXY_LOCK:
        _PROXY_LAST_OK["t"] = time.monotonic()


def _note_system_proxy_failure(host: str) -> None:
    """记录一次系统代理失败；不同主机累计到阈值 → 整体停用一段时间。

    前提：近期没有任何请求经代理成功过。若代理确实在转发（只是个别主机直连更
    快/个别请求超时），整体停用会把大量"只有代理能通"的站一起打死——这正是
    「所有需要代理的网站都加载不出来」的级联成因，故只做按主机绕过。
    """
    now = time.monotonic()
    with _SYSTEM_PROXY_LOCK:
        if host:
            _PROXY_FAIL["hosts"].add(host)
        live = (now - _PROXY_LAST_OK["t"]) < _PROXY_LIVE_WINDOW
        if not live and len(_PROXY_FAIL["hosts"]) >= _PROXY_FAIL_HOSTS:
            _PROXY_FAIL["until"] = now + _PROXY_FAIL_TTL
        # 立刻让下一次探活重新决策（可能已整体停用）
        _SYSTEM_PROXY_CACHE["t"] = 0.0
        _SYSTEM_PROXY_CACHE["url"] = None


def _reset_proxy_failures() -> None:
    """清空僵尸代理记忆（测试 / 用户手动切换网络后调用）。"""
    with _SYSTEM_PROXY_LOCK:
        _PROXY_FAIL["hosts"].clear()
        _PROXY_FAIL["until"] = 0.0
        _PROXY_OK.clear()
        _PROXY_LAST_OK["t"] = 0.0
        _SYSTEM_PROXY_CACHE["t"] = 0.0
        _SYSTEM_PROXY_CACHE["url"] = None


def _note_proxy_success(host: str) -> None:
    """该主机只有在系统代理下才通（直连不通）→ 短期内优先走代理。

    实测（同一台机器、同一时刻、各 3 轮）：favcomic / xvideos 直连
    18s 连接被重置，走代理 1.6~2.3s 正常；反之 dm5 / omofuna 直连 0.2~0.9s。
    没有一个出口对所有站都对，所以按主机记「哪个出口成功过」：代理偏好成立时
    直接给足预算走代理，不再先白等一次探测 + 一次必然失败的直连。
    """
    if not host:
        return
    with _SYSTEM_PROXY_LOCK:
        _PROXY_OK[host] = time.monotonic() + _PROXY_OK_TTL
        _PROXY_LAST_OK["t"] = time.monotonic()


def _forget_proxy_preference(host: str) -> None:
    """撤销「只有代理能通」的旧结论（代理这次自己失败了）。

    代理偏好一旦写错，代价是每个请求都要先白等一次超时（omofuna 实测
    详情 12.7s，其中 8s 是等代理读超时）。失败当场撤销，下一次就直接直连。
    """
    if not host:
        return
    with _SYSTEM_PROXY_LOCK:
        _PROXY_OK.pop(host, None)


def _proxy_preferred(host: str) -> bool:
    """该主机是否处于「已知只有代理能通」的有效期内。"""
    if not host:
        return False
    now = time.monotonic()
    with _SYSTEM_PROXY_LOCK:
        until = _PROXY_OK.get(host)
        if until is None:
            return False
        if until <= now:
            _PROXY_OK.pop(host, None)
            return False
        return True


def _system_proxy_suspended() -> bool:
    """系统代理是否处于"整体停用"窗口内（到期自动解除）。"""
    now = time.monotonic()
    with _SYSTEM_PROXY_LOCK:
        until = _PROXY_FAIL["until"]
        if until and until <= now:
            _PROXY_FAIL["hosts"].clear()
            _PROXY_FAIL["until"] = 0.0
            return False
        return bool(until)


# --------------------------------------------------------------------------- #
# 证书链不完整的站点（少发中间证书的 CDN / 被中间设备改写的站点）
# --------------------------------------------------------------------------- #
# requests+certifi 不会像浏览器那样去 AIA 补拉中间证书，于是
# `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate` 直接失败，
# 站点在界面里等同于"打不开"（agedm.io 实测就是这种）。这类站点用其他客户端
# （浏览器/curl -k）都能正常取到内容，所以这里做一次**针对性放宽**：
# 只在证书链验证失败时对该主机改用 verify=False，并在本进程内记住，避免每次
# 都先失败一遍；其他主机的校验强度不受影响。
_INSECURE_HOSTS: set = set()
_INSECURE_LOCK = threading.Lock()


def _insecure_host(host: str) -> bool:
    if not host:
        return False
    with _INSECURE_LOCK:
        return host in _INSECURE_HOSTS


def _note_insecure_host(host: str) -> None:
    if not host:
        return
    with _INSECURE_LOCK:
        _INSECURE_HOSTS.add(host)


def _reset_insecure_hosts() -> None:
    with _INSECURE_LOCK:
        _INSECURE_HOSTS.clear()


def _is_cert_error(exc) -> bool:
    """是否为 TLS 证书链校验失败（区别于超时/连接被拒）。"""
    text = f"{type(exc).__name__}: {exc}".lower()
    return ("certificate_verify_failed" in text or "certificate verify failed" in text
            or "self-signed certificate" in text
            or "unable to get local issuer certificate" in text)


def _live_system_proxy() -> Optional[str]:
    """返回当前**存活可用**的系统代理 URL；未配置/端口已死 → None（上层直连）。

    带 TTL 缓存：常规请求不反复探测端口；VPN/加速器开/关后最迟 ~10s 自动生效。
    解析不出 host:port 的非标准写（如 socks 名）按历史行为直接采用，不误伤。

    另外两层保护（见 _PROXY_FAIL）：
    - 端口探活**不代表能用**，走它失败过的主机会被记录，后续直接绕过；
    - 多主机连续失败 → 系统代理整体停用 _PROXY_FAIL_TTL 秒，到点自动重试。
    """
    if _system_proxy_suspended():
        return None
    now = time.monotonic()
    with _SYSTEM_PROXY_LOCK:
        if now - _SYSTEM_PROXY_CACHE["t"] < _SYSTEM_PROXY_TTL:
            return _SYSTEM_PROXY_CACHE["url"]
    url: Optional[str] = None
    for candidate in _system_proxy_candidates():
        host, port = _parse_proxy_host_port(candidate)
        if not (host and port):
            url = candidate  # 解析不出端口：不误伤非标准写法，直接采用
            break
        if _is_loopback(host) and not _port_open(host, port):
            continue  # 本机加速器端口已死 → 该候选不可用，继续找下一个
        url = candidate  # 本机端口存活 / 远程代理不探活 → 采用
        break
    with _SYSTEM_PROXY_LOCK:
        _SYSTEM_PROXY_CACHE["t"] = now
        _SYSTEM_PROXY_CACHE["url"] = url
    return url


def _urllib_opener(proxy: Optional[str]):
    """构造 urllib opener：有代理走显式 ProxyHandler，否则**强制直连**。

    默认 opener 会隐式读注册表/环境系统代理（僵尸代理 → 全站打不开），
    ProxyHandler({}) 关闭该继承，与 HttpClient 的显式代理策略保持一致。
    """
    import urllib.request

    proxies = {"http": proxy, "https": proxy} if proxy else {}
    return urllib.request.build_opener(urllib.request.ProxyHandler(proxies))


@dataclass
class NetworkDefaults:
    """全局网络默认值（来自 settings，缺省用框架硬编码值）。"""

    timeout: float = 10.0
    retries: int = 3
    interval_ms: int = 0
    proxy: Optional[str] = None
    user_agent: str = DEFAULT_USER_AGENT
    # 激进档反爬增强（可选，默认关闭）：
    # impersonate 为 curl_cffi 伪装档位（如 "chrome"），None 表示不走 TLS/JA3 伪装，
    # 请求仍走原 requests 路径，行为与现有一致（回归安全）。
    impersonate: Optional[str] = None
    # 备选 UA 列表：调用方未显式传 headers 时按请求次数轮换取其一；为空回退 user_agent。
    user_agents: Optional[list] = None

try:
    import requests

    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

try:
    from curl_cffi import requests as crequests

    _CFFI_AVAILABLE = True
except ImportError:  # noqa: BLE001 —— curl_cffi 缺失时仅禁用 TLS 伪装，不影响既有功能
    crequests = None
    _CFFI_AVAILABLE = False


class AntiScrapeError(RequestError):
    """反爬识别：HTTP 403/429/5xx，或响应含验证码/封禁特征词。

    配置了代理池时由 HttpClient 换 IP 重试（不重复请求同一 IP）；
    未配置代理池时按普通失败走原有重试逻辑。
    """


# 反爬状态码：403 禁止、405 方法不允许（部分站如 17k 用作限流/封禁信号）、
# 429 限流、5xx 服务端风控。命中 → AntiScrapeError → 触发代理池换 IP 重试。
ANTI_SCRAPE_STATUSES = frozenset({403, 405, 429})

# 验证码/封禁特征词（对响应文本前 2000 字符小写匹配）
ANTI_SCRAPE_KEYWORDS = (
    "captcha", "geetest", "g-recaptcha", "verify you are human",
    "you have been blocked", "access denied", "access is denied",
    "anti-bot", "riskcontrol", "waf",
    "acw_sc__v2", "aliyunwaf", "var arg1=",
    # Cloudflare 挑战/拦截页（HTTP 200 也会出现）：不识别就会被当正文解析，
    # 最终报成「未解析到正文内容」，真实原因（被 CF 拦）完全看不出。
    "just a moment", "attention required", "checking your browser",
    "cf-browser-verification", "cf_chl_opt", "cf-chl-",
    "enable javascript and cookies to continue",
    "验证码", "人机验证", "滑动验证", "拖动验证",
    # 站点限流/权限页（哔哩轻小说连发几次后返回 200 + 这段文案）：
    # 不识别就会被解析成「空正文」，用户只看到「未解析到正文内容」。
    "沒有可閱讀的章節", "没有可阅读的章节",
    "访问过于频繁", "访问频率过高", "请求过于频繁",
    "频繁操作", "搜索时间间隔",
    "被封", "封禁", "已封", "被禁止访问", "ip被限制", "ip 被限制", "访问被拒绝",
)


def _is_anti_scrape_status(status: int) -> bool:
    """按状态码判定反爬：403/429/5xx。"""
    return status in ANTI_SCRAPE_STATUSES or 500 <= status < 600


def _is_nonretryable_status(exc) -> bool:
    """4xx（排除可重试的 408/425/429）为确定性失败：重试无意义且拖慢末页探测。

    404 是分页探测末页的正常信号，逐页重试 3 次（间隔 0.5/1/2s）会把
    搜索/发现卡到几十秒。requests 抛 HTTPError（带 response），urllib
    抛 urllib.error.HTTPError（带 code）。
    """
    code = None
    resp = getattr(exc, "response", None)
    if resp is not None:
        code = getattr(resp, "status_code", None)
    if code is None:
        code = getattr(exc, "code", None)
    return isinstance(code, int) and 400 <= code < 500 and code not in (408, 425, 429)


def _is_acw_challenge(text: str) -> bool:
    """判定 acw_sc__v2 阿里云盾 challenge 页（arg1 + acw_sc__v2 cookie 脚本）。"""
    snippet = (text or "")[:4000]
    return (
        "var arg1=" in snippet
        and "acw_sc__v2" in snippet
        and ("aliyunwaf" in snippet or "setCookie" in snippet)
    )


def _solve_acw(challenge_html: str) -> str:
    """解算 acw_sc__v2 cookie 值；失败返回空串。"""
    try:
        from .acw_solver import extract_arg1, extract_xor_key, solve_acw_cookie

        arg1 = extract_arg1(challenge_html)
        if not arg1:
            return ""
        key = extract_xor_key(challenge_html)
        return solve_acw_cookie(arg1, key) if key else ""
    except Exception:  # noqa: BLE001
        return ""


def _anti_scrape_hit(text: str) -> str:
    """返回命中的反爬特征词（未命中为空串）。

    命中词直接带进异常信息：用户/日志能看到是「验证码」「沒有可閱讀的章節」
    还是「访问过于频繁」，不用再去猜为什么正文是空的。
    """
    snippet = (text or "")[:6000]
    snippet = re.sub(r"<!--.*?-->", " ", snippet, flags=re.S)
    snippet = re.sub(r"<[^>]*>", " ", snippet)
    snippet = snippet.lower()
    for keyword in ANTI_SCRAPE_KEYWORDS:
        if keyword in snippet:
            return keyword
    return ""


def _is_anti_scrape_text(text: str) -> bool:
    """按特征词判定反爬：验证码/封禁提示等。只扫前 6000 字符降低误报与开销。

    先剔除 HTML 注释与标签：关键词在标签属性里会误报（如
    comment-captcha-dialog.css 的 'captcha' 是正常资源文件名，非反爬）。
    真实反爬页关键词在可见文本/脚本正文，剥标签后仍命中。

    扫描窗口不能太小：小说站的「沒有可閱讀的章節」提示藏在很长的
    meta description 与导航之后（实测 >2000 字符），窗口 2000 会漏判，
    最终报成「未解析到正文内容」，用户看不出真实原因。
    """
    snippet = (text or "")[:6000]
    snippet = re.sub(r"<!--.*?-->", " ", snippet, flags=re.S)
    snippet = re.sub(r"<[^>]*>", " ", snippet)
    snippet = snippet.lower()
    return any(k in snippet for k in ANTI_SCRAPE_KEYWORDS)


def _is_json_document(text: str) -> bool:
    """响应体是否为一份完整 JSON（API 站的数据体）。

    为什么需要这层判断：`ANTI_SCRAPE_KEYWORDS` 里的词（「被封」「封禁」
    「验证码」等）是为 **HTML 挑战页** 设计的。JSON 数据体没有标签可剥，
    词表会直接命中正常内容——纵横搜索的简介「年仅二十便被封为史上最年轻
    的圣女」含「被封」，整源搜索被判成「反爬特征响应（命中：被封）」，
    用户侧就是「纵横小说搜索出错」。真正的反爬页是 HTML，`json.loads`
    必然失败，仍然会被特征词扫到（不放松拦截能力）。

    首字符快速排除 HTML（`<` 开头直接返回 False，不做 24KB 的解析），
    再用 json.loads 确认完整：`<html>…{"a":1}…</html>` 这类混排页
    不会被误当成数据体。
    """
    stripped = (text or "").lstrip("\ufeff \t\r\n")
    if not stripped or stripped[0] not in "[{":
        return False
    try:
        json.loads(stripped)
    except (ValueError, TypeError):
        return False
    return True


def _log_non_json(url: str, text: str) -> None:
    """记录"响应非 JSON"警告（E1 修复）。

    原实现 JSONDecodeError 时静默 return {}，把反爬/错误页当空数据吞掉，
    用户只看到"无地址/空章节"。保持返回 {} 兼容（不破坏调用方），
    但带响应前 200 字符打 warning，便于定位真实原因。
    """
    import logging

    snippet = (text or "")[:200].replace("\n", " ").strip()
    logging.getLogger(__name__).warning(
        "JSON 解析失败 %s：响应非 JSON（前 200 字符）：%s", url, snippet
    )


class HttpClient:
    # 连接池参数：并发下载（漫画多线程取图、批量任务）时同 host 请求远超 requests
    # 默认池（10）→ 会排队等连接。放大池容量，配合 keep-alive 显著提速。
    _POOL_CONNECTIONS = 40
    _POOL_MAXSIZE = 128

    def __init__(self, sleeper=None, defaults: Optional[NetworkDefaults] = None,
                 cache=None):
        self._sleeper = sleeper if sleeper is not None else time.sleep
        self.defaults = defaults or NetworkDefaults()
        self.cache = cache  # 可选 RedisLikeStore 实例（None=禁用），供上层显式查/写
        self._session = None
        self._impersonate_session = None
        self._impersonate_lock = threading.Lock()
        self._ua_lock = threading.Lock()
        self._ua_index = 0
        self._last_url = ""
        # >0 时临时强制直连（系统代理不通的回退路径用，见 _run_with_proxy_switch）
        self._direct_depth = 0
        if _REQUESTS_AVAILABLE:
            self._session = requests.Session()
            # `_system_proxy_enabled`：是否自动采用「存活」的系统代理（见 _effective_proxy）。
            # 生产保持 True（VPN/加速器开着时走代理，快且海外站可达）；测试连 localhost
            # 时置 False 隔离本机系统代理（沿用旧 trust_env=False 的沙箱意图）。
            self._system_proxy_enabled = True
            # 系统代理策略：不要靠 requests 隐式 trust_env 透传——那会无条件走 Windows
            # 注册表代理，VPN/加速器关闭后端口已死而注册表项仍在 → 所有请求连向不存在
            # 的代理 → 国内直连源也打不开。改为显式控制：trust_env=False 切断隐式继承，
            # 由 _effective_proxy 决定（源/全局显式代理优先，否则探测存活系统代理，
            # 存活才用、死了自动直连），行为完全可控、与 requests 版本无关。
            self._session.trust_env = False
            self._bump_connection_pool()

    def _effective_proxy(self, explicit: Optional[str],
                         force_direct: bool = False) -> Optional[str]:
        """解析单次请求的最终代理：显式（源/调用方/代理池 IP）> 全局 network.proxy
        > 存活系统代理 > None（直连）。

        force_direct=True：强制直连——屏蔽全局 network.proxy 与自动系统代理
        （如源配置 transports.direct），但调用方显式传入的 proxy 仍优先（尊重
        更具体的代理意图）。

        显式/全局代理永远优先且不做探测；系统代理只有在 `_system_proxy_enabled`
        且端口存活时才采用（VPN 关 → 自动直连，国内直连源恢复正常打开；
        VPN 开 → 走代理，需要 VPN 的海外源可用）。
        """
        if force_direct:
            return explicit
        if explicit:
            return explicit
        if self.defaults.proxy:
            return self.defaults.proxy
        if self._direct_depth or not self._system_proxy_enabled:
            return None
        return _live_system_proxy()

    def _using_system_proxy(self, proxy: Optional[str]) -> bool:
        """本次请求的出口是否正是"自动探测到的系统代理"（僵尸代理回退只管这种）。

        显式 proxy、全局 network.proxy、测试禁用、强制直连 —— 都是调用方的
        明确意图，不做自动回退，免得把用户配置的代理悄悄绕过。
        """
        if proxy or self.defaults.proxy:
            return False
        if self._direct_depth or not self._system_proxy_enabled:
            return False
        return _live_system_proxy() is not None

    @contextmanager
    def _direct_only(self):
        """临时强制直连（屏蔽系统代理；显式/全局代理仍优先）。"""
        self._direct_depth += 1
        try:
            yield
        finally:
            self._direct_depth -= 1

    def _bump_connection_pool(self) -> None:
        """放大 requests HTTPAdapter 连接池（每 host 并发连接数）。

        默认 pool_maxsize=10 时，8 线程取图 + 主请求并发会互相阻塞等连接；
        放大后同 host 可同时建立更多连接，漫画/批量下载吞吐明显提升。
        """
        try:
            from requests.adapters import HTTPAdapter

            adapter = HTTPAdapter(
                pool_connections=self._POOL_CONNECTIONS,
                pool_maxsize=self._POOL_MAXSIZE,
                max_retries=0,
            )
            self._session.mount("http://", adapter)
            self._session.mount("https://", adapter)
        except Exception:  # noqa: BLE001 —— 池放大失败不影响使用
            pass

    # ------------------------------------------------------------------ #
    def _headers_with_ua(self, headers: Optional[dict]) -> Optional[dict]:
        """调用方未传 headers 时注入 UA（有 UA 则不覆盖）。

        配置了 user_agents 列表则按请求次数轮换取其一，否则用默认 UA。
        同时注入 data/cf_cookies.json 中该域名的 cf_clearance（手动过
        Cloudflare 验证的 cookie），纯 HTTP 直连过 CF 反爬。
        """
        if headers is None:
            ua = self._next_user_agent()
            if ua:
                headers = {"User-Agent": ua}
        if headers:
            headers = _with_cf_cookies(headers, self._last_url or "")
            headers = {k: _latin1_header_value(v) for k, v in headers.items()}
        return headers

    def _next_user_agent(self) -> Optional[str]:
        """线程安全轮换 UA：user_agents 列表按请求次数取模，未配置则回退默认 UA。"""
        uas = self.defaults.user_agents or []
        if uas:
            with self._ua_lock:
                ua = uas[self._ua_index % len(uas)]
                self._ua_index += 1
            return ua
        return self.defaults.user_agent

    def _active_session(self):
        """当前生效的会话：启用 impersonate 用 curl_cffi（惰性创建、复用连接池），
        否则返回 requests 会话。curl_cffi 不可用/初始化失败时优雅降级回 requests。
        """
        if self.defaults.impersonate:
            if self._impersonate_session is None:
                with self._impersonate_lock:
                    if self._impersonate_session is None:
                        self._impersonate_session = self._new_impersonate_session()
            if self._impersonate_session is not None:
                return self._impersonate_session
        return self._session

    def _new_impersonate_session(self):
        """创建 curl_cffi Session（TLS/JA3 伪装）；失败返回 None 走 requests。"""
        if not _CFFI_AVAILABLE:
            return None
        try:
            return crequests.Session(impersonate=self.defaults.impersonate)
        except Exception:  # noqa: BLE001 —— 伪装初始化失败不影响既有请求能力
            return None

    def _response_text(self, resp, encoding: Optional[str]) -> str:
        """统一 requests/curl_cffi 响应解码语义。

        显式 encoding 优先；否则 Content-Type charset；取不到时用 requests 的
        apparent_encoding（curl_cffi 无该属性，getattr 兜底为 utf-8）。
        与既有 requests 分支逻辑字节级一致。
        """
        if encoding:
            resp.encoding = encoding
            return resp.text
        charset = (resp.headers.get("Content-Type") or "").split("charset=")[-1].strip().lower()
        if "charset=" not in resp.headers.get("Content-Type", "") or "/" in charset:
            charset = ""
        if charset and charset not in ("iso-8859-1",):
            resp.encoding = charset
        else:
            try:
                apparent = getattr(resp, "apparent_encoding", None)
            except Exception:  # noqa: BLE001 —— 缺编码检测库时按 utf-8 兜底
                apparent = None
            resp.encoding = apparent or "utf-8"
        return resp.text

    def _set_last_url(self, url: str) -> None:
        """记录最近一次请求 URL，供 _headers_with_ua 注入 CF cookie 用。"""
        self._last_url = url

    # ------------------------------------------------------------------ #
    def _run_with_proxy_switch(self, once, proxy, proxy_pool, url_desc):
        """代理池换 IP 重试外壳：每次用 proxy_pool.next() 取代理，失败（含反爬）
        则 mark_bad 换下一个，最多换 max_switches 次。

        auto 池（触发反爬才启用）：首次请求直连，遇 AntiScrapeError 自动
        engage() 启用代理并用第一个代理重试——只针对触发反爬的源（每源持有
        独立池实例，互不影响）。普通失败（非反爬）不触发启用。

        未配置代理池（或池为空）时行为与原逻辑完全一致：直连/单代理跑一次。
        once(current_proxy)：单次（含原 retries 重试）请求，抛异常表示该 IP 失败。

        唯一的例外是**系统代理**：它只是"探测到的"，不代表可用。走它失败后
        立刻改直连重试，并把该主机记进 _PROXY_FAIL（同主机后续直接绕过）。
        显式 proxy / 全局 network.proxy 不参与这套回退。
        """
        if proxy_pool is None or len(proxy_pool) == 0:
            if not self._using_system_proxy(proxy):
                return once(proxy)
            return self._run_direct_fallback(once, proxy, url_desc)
        last_error: Exception | None = None
        total = proxy_pool.max_switches + 1  # 初始 IP + 最多换 N 次
        for attempt in range(total):
            current_proxy = proxy_pool.next()
            try:
                return once(current_proxy)
            except AntiScrapeError as exc:
                last_error = exc
                if proxy_pool.is_auto and not proxy_pool.engaged:
                    # 触发反爬 → 启用代理池（针对该源），立即用第一个代理重试
                    proxy_pool.engage()
                    continue
                if attempt >= total - 1:
                    break
                proxy_pool.mark_bad()
                if not proxy_pool.available():
                    break  # 代理全部失效：抛最后错误，不再直连兜底
                self._sleeper(min(0.5 * (2 ** attempt), 2.0))
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if proxy_pool.is_auto and not proxy_pool.engaged:
                    raise  # 普通失败（非反爬）：不自动启用代理，直连错误原样抛
                if attempt >= total - 1:
                    break
                proxy_pool.mark_bad()
                if not proxy_pool.available():
                    break
                self._sleeper(min(0.5 * (2 ** attempt), 2.0))
        if isinstance(last_error, RequestError):
            raise last_error
        raise RequestError(f"请求失败 {url_desc}：{last_error}")

    def _retry_insecure(self, url, call):
        """执行 call(verify=...)；证书链不完整时本进程内改用 verify=False 重试。

        只放宽"证书链验证失败"这一种错误：超时、连接被拒、反爬状态码照旧。
        已知主机（_INSECURE_HOSTS）后续直接不验证，不再白跑一次失败请求。
        """
        host = _host_of_url(url)
        verify = not _insecure_host(host)
        try:
            return call(verify)
        except Exception as exc:  # noqa: BLE001
            if not verify or not _is_cert_error(exc):
                raise
            logging.getLogger(__name__).warning(
                "证书链不完整（%s），本进程内改用 verify=False：%s",
                host or url, str(exc)[:120],
            )
            _note_insecure_host(host)
            # 已明确知道这是"刻意放宽"，就别再让 urllib3 反复刷 InsecureRequestWarning
            try:
                import urllib3

                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:  # noqa: BLE001 —— 告警压制失败不影响请求
                pass
            return call(False)

    def _run_direct_fallback(self, once, proxy, url_desc):
        """系统代理出口专用的兜底：先快速探一次，不通立刻改直连。

        "端口活着"不等于"能转发"：僵尸加速器会让每个请求读超时，
        retries×timeout 实测能拖 44s，界面/播放器全拿不到内容。这里把代价
        压到一次 _PROXY_PROBE_TIMEOUT，并把"代理不通但直连能通"的主机记进
        _PROXY_FAIL（同主机后续直接绕过）。

        直连也不通时用完整预算再试一次代理 —— 需要 VPN 的站可能只是慢或者
        本来就得走代理，不能因为一次快速探测被判死，也不计入僵尸代理记忆。
        """
        url = url_desc.rsplit(" ", 1)[-1]
        host = _host_of_url(url)

        # 已知「只有代理能通」（直连被重置/封锁）→ 直接给足预算走代理，
        # 省掉一次 5s 探测 + 一次必然失败的直连（favcomic/xvideos 实测）。
        if _proxy_preferred(host) and not _proxy_failed_for(host):
            try:
                return once(proxy)
            except AntiScrapeError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 代理这条路也不行了，走常规流程
                logging.getLogger(__name__).info(
                    "代理偏好失效（%s），改按直连重试：%s", host or url, str(exc)[:120],
                )
                # 只撤销「该主机只有代理能通」的结论，不记成"代理整体不可用"：
                # 一次代理抖动不配触发全局停用（会把别的需要代理的站一起打死），
                # 撤掉偏好后下一次请求回到"短探测 + 直连优先"的常规决策。
                _forget_proxy_preference(host)

        # 已知"走代理不通、直连能通"的主机：连探测都省掉
        if _proxy_failed_for(host):
            direct_exc = None
            with self._direct_only():
                try:
                    return once(proxy)
                except AntiScrapeError:
                    raise
                except Exception as exc:  # noqa: BLE001 —— 直连又不行了
                    direct_exc = exc
            try:
                return once(proxy)
            except Exception:  # noqa: BLE001 —— 代理也救不回来，抛直连错误
                raise direct_exc

        proxy_exc = None
        try:
            result = once(proxy, 0, min(self.defaults.timeout, _PROXY_PROBE_TIMEOUT))
        except AntiScrapeError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 探测失败，改直连
            proxy_exc = exc
        else:
            # 代理成功转发 → 记一笔"代理是活的"，阻止后续级联整体停用
            _mark_proxy_live()
            return result
        if _is_http_status_error(proxy_exc):
            # 服务端明确答复了状态码（404/400/…）：与出口无关，别把主机记成
            # "必须直连"（换直连只会再失败一次，还白白关掉该主机的代理）。
            raise proxy_exc
        logging.getLogger(__name__).warning(
            "系统代理不通（%s），改直连重试：%s", host or url, str(proxy_exc)[:160],
        )
        direct_exc = None
        with self._direct_only():
            try:
                result = once(proxy)
            except AntiScrapeError:
                raise
            except Exception as exc:  # noqa: BLE001
                direct_exc = exc
            else:
                # 只有"代理不通、直连通"才算僵尸代理证据
                _note_system_proxy_failure(host)
                return result
        # 直连也不通：可能确实需要代理（只是慢），再给代理一次机会——
        # 但只试一次、超时封顶 12s：刚才的探测已经证明出口黑了几秒，
        # 再按「完整预算（retries+1 次 × 满超时）」重试，实测能把一次详情
        # 请求拖到 30s（omofuna 代理黑洞 + 直连 RST）。真需要代理的慢站，
        # 单次 12s 仍能拿到，拿不到就是真不通。
        try:
            result = once(
                proxy, 0, min(self.defaults.timeout, _PROXY_RETRY_TIMEOUT)
            )
        except Exception:  # noqa: BLE001 —— 代理也救不回来，抛直连错误
            raise direct_exc from proxy_exc
        # 直连不通、代理能通 → 记住该主机的出口偏好，下次不再白试直连
        _note_proxy_success(host)
        return result

    # ------------------------------------------------------------------ #
    def get_text(
        self,
        url: str,
        headers: Optional[dict] = None,
        proxy: Optional[str] = None,
        timeout: float | None = None,
        retries: int | None = None,
        interval_ms: int | None = None,
        encoding: Optional[str] = None,
        proxy_pool: Optional[ProxyPool] = None,
        direct: bool = False,
    ) -> str:
        """GET 返回响应文本。retries耗尽抛RequestError。
        encoding: 显式指定响应编码（如utf-8），None则用resp.text自动检测。
        proxy_pool: 代理IP池，反爬/失败时换IP重试（最多换 max_switches 次）。
        direct: 强制直连（屏蔽默认/系统代理，显式 proxy 仍优先），如源配置
        transports.direct——国内直连源在加速器全局代理下被转发拖慢数十倍。
        未传的参数用全局默认值（NetworkDefaults）。
        """
        if timeout is None:
            timeout = self.defaults.timeout
        if retries is None:
            retries = self.defaults.retries
        if interval_ms is None:
            interval_ms = self.defaults.interval_ms
        self._set_last_url(url)
        headers = self._headers_with_ua(headers)
        self._sleeper(interval_ms / 1000.0)
        if proxy is None:
            proxy = None if direct else self.defaults.proxy

        def _once(current_proxy: Optional[str], retries_override: int | None = None,
                  timeout_override: float | None = None) -> str:
            proxy_eff = self._effective_proxy(current_proxy, direct)
            budget = retries if retries_override is None else retries_override
            wait_s = timeout if timeout_override is None else timeout_override
            last_error: Exception | None = None
            for attempt in range(budget + 1):
                try:
                    return self._get_once(url, headers, proxy_eff, wait_s, encoding)
                except AntiScrapeError as exc:
                    if proxy_pool is not None:
                        raise  # 反爬：不重复请求同一IP，立即换IP重试
                    last_error = exc
                    if attempt < budget:
                        self._sleeper(min(0.5 * (2 ** attempt), 2.0))
                except Exception as exc:  # noqa: BLE001
                    if _is_nonretryable_status(exc):
                        raise RequestError(f"请求失败 GET {url}：{exc}")
                    last_error = exc
                    if attempt < budget:
                        self._sleeper(min(0.5 * (2 ** attempt), 2.0))
            # 反爬/网络异常已是 RequestError（含 AntiScrapeError）→ 原样透传，
            # 保留类型供调用方区分反爬（否则包成纯 RequestError 会抹掉类型）。
            if isinstance(last_error, RequestError):
                raise last_error
            raise RequestError(f"请求失败 GET {url}：{last_error}")

        return self._run_with_proxy_switch(_once, proxy, proxy_pool, f"GET {url}")

    def get_bytes(
        self,
        url: str,
        headers: Optional[dict] = None,
        proxy: Optional[str] = None,
        timeout: float | None = None,
        retries: int | None = None,
        proxy_pool: Optional[ProxyPool] = None,
        direct: bool = False,
    ) -> bytes:
        """GET 返回响应字节（图片等二进制内容）。重试耗尽抛 RequestError。
        proxy_pool: 代理IP池，反爬/失败时换IP重试。
        direct: 强制直连（屏蔽默认/系统代理，显式 proxy 仍优先）。"""
        if timeout is None:
            timeout = self.defaults.timeout
        if retries is None:
            retries = self.defaults.retries
        if proxy is None:
            proxy = None if direct else self.defaults.proxy
        self._set_last_url(url)
        headers = self._headers_with_ua(headers)
        self._sleeper(0.0)

        def _once(current_proxy: Optional[str], retries_override: int | None = None,
                  timeout_override: float | None = None) -> bytes:
            proxy_eff = self._effective_proxy(current_proxy, direct)
            budget = retries if retries_override is None else retries_override
            wait_s = timeout if timeout_override is None else timeout_override
            last_error: Exception | None = None
            for attempt in range(budget + 1):
                try:
                    session = self._active_session()
                    if session is not None:
                        proxies = {"http": proxy_eff, "https": proxy_eff} if proxy_eff else None
                        resp = self._retry_insecure(url, lambda verify: session.get(
                            url, headers=headers, proxies=proxies,
                            timeout=wait_s, verify=verify,
                        ))
                        if _is_anti_scrape_status(resp.status_code):
                            raise AntiScrapeError(f"反爬响应 HTTP {resp.status_code} {url}")
                        resp.raise_for_status()
                        return resp.content
                    import urllib.request

                    req = urllib.request.Request(url, headers=headers or {})
                    with _urllib_opener(proxy_eff).open(req, timeout=wait_s) as resp:
                        if resp.status >= 400:
                            raise RequestError(f"HTTP {resp.status} {url}")
                        return resp.read()
                except AntiScrapeError as exc:
                    if proxy_pool is not None:
                        raise
                    last_error = exc
                    if attempt < budget:
                        self._sleeper(min(0.5 * (2 ** attempt), 2.0))
                except Exception as exc:  # noqa: BLE001
                    if _is_nonretryable_status(exc):
                        raise RequestError(f"请求失败 GET {url}：{exc}")
                    last_error = exc
                    if attempt < budget:
                        self._sleeper(min(0.5 * (2 ** attempt), 2.0))
            if isinstance(last_error, RequestError):
                raise last_error
            raise RequestError(f"请求失败 GET {url}：{last_error}")

        return self._run_with_proxy_switch(_once, proxy, proxy_pool, f"GET {url}")

    def get_json(
        self,
        url: str,
        headers: Optional[dict] = None,
        proxy: Optional[str] = None,
        timeout: float | None = None,
        retries: int | None = None,
        proxy_pool: Optional[ProxyPool] = None,
        direct: bool = False,
    ) -> dict:
        """GET 并解析 JSON 响应。proxy_pool: 代理IP池，反爬/失败时换IP重试。
        direct: 强制直连（屏蔽默认/系统代理），语义同 get_text。"""
        import json

        text = self.get_text(
            url, headers, proxy, timeout, retries, proxy_pool=proxy_pool, direct=direct
        )
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            _log_non_json(url, text)
            return {}

    def post_json(
        self,
        url: str,
        json_body: Optional[dict] = None,
        headers: Optional[dict] = None,
        proxy: Optional[str] = None,
        timeout: float | None = None,
        retries: int | None = None,
        proxy_pool: Optional[ProxyPool] = None,
        direct: bool = False,
    ) -> dict:
        """POST JSON 并解析响应 JSON。供解密 custom_endpoint 等调用。
        proxy_pool: 代理IP池，反爬/失败时换IP重试。
        direct: 强制直连（屏蔽默认/系统代理，显式 proxy 仍优先）。"""
        import json as _json

        if timeout is None:
            timeout = self.defaults.timeout
        if retries is None:
            retries = self.defaults.retries
        if proxy is None:
            proxy = None if direct else self.defaults.proxy
        self._set_last_url(url)
        headers = self._headers_with_ua(headers)
        self._sleeper(0.0)

        def _once(current_proxy: Optional[str], retries_override: int | None = None,
                  timeout_override: float | None = None) -> dict:
            proxy_eff = self._effective_proxy(current_proxy, direct)
            budget = retries if retries_override is None else retries_override
            wait_s = timeout if timeout_override is None else timeout_override
            last_error: Exception | None = None
            for attempt in range(budget + 1):
                try:
                    post_headers = dict(headers or {})
                    post_headers.setdefault("Content-Type", "application/json")
                    if self._session is not None:
                        resp = self._retry_insecure(url, lambda verify: self._session.post(
                            url,
                            json=json_body or {},
                            headers=post_headers,
                            timeout=wait_s,
                            verify=verify,
                            proxies={"http": proxy_eff, "https": proxy_eff} if proxy_eff else None,
                        ))
                        if _is_anti_scrape_status(resp.status_code):
                            raise AntiScrapeError(f"反爬响应 HTTP {resp.status_code} {url}")
                        resp.raise_for_status()
                        text = resp.text
                    else:
                        import urllib.request

                        body = _json.dumps(json_body or {}).encode("utf-8")
                        req = urllib.request.Request(
                            url, data=body, headers=post_headers
                        )
                        with _urllib_opener(proxy_eff).open(req, timeout=wait_s) as resp:
                            text = resp.read().decode("utf-8", errors="replace")
                    # JSON 数据体不做特征词扫描（词表是给 HTML 挑战页的，
                    # 小说简介里的「被封/验证码」会误报，见 _is_json_document）
                    if not _is_json_document(text) and _is_anti_scrape_text(text):
                        raise AntiScrapeError(f"反爬特征响应 POST（命中：{_anti_scrape_hit(text)}）{url}")
                    try:
                        return _json.loads(text)
                    except _json.JSONDecodeError:
                        _log_non_json(url, text)
                        return {}
                except AntiScrapeError as exc:
                    if proxy_pool is not None:
                        raise
                    last_error = exc
                    if attempt < budget:
                        self._sleeper(min(0.5 * (2 ** attempt), 2.0))
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if attempt < budget:
                        self._sleeper(min(0.5 * (2 ** attempt), 2.0))
            if isinstance(last_error, RequestError):
                raise last_error
            raise RequestError(f"请求失败 POST {url}：{last_error}")

        return self._run_with_proxy_switch(_once, proxy, proxy_pool, f"POST {url}")

    def post_text(
        self,
        url: str,
        json_body: Optional[dict] = None,
        headers: Optional[dict] = None,
        proxy: Optional[str] = None,
        timeout: float | None = None,
        retries: int | None = None,
        proxy_pool: Optional[ProxyPool] = None,
        direct: bool = False,
        encoding: Optional[str] = None,
    ) -> str:
        """POST JSON 并返回响应原始文本（供 text/event-stream 的 SSE 流解析）。
        与 post_json 唯一的区别是：不尝试 JSON 解析，直接返回文本。
        direct: 强制直连（屏蔽默认/系统代理，显式 proxy 仍优先）。
        encoding: 显式解码编码（SSE 响应常无 charset，requests 会按 iso-8859-1
        兜底导致中文乱码；源声明了 charset 时传入即可）。"""
        import json as _json

        if timeout is None:
            timeout = self.defaults.timeout
        if retries is None:
            retries = self.defaults.retries
        if proxy is None:
            proxy = None if direct else self.defaults.proxy
        self._set_last_url(url)
        headers = self._headers_with_ua(headers)
        self._sleeper(0.0)

        def _once(current_proxy: Optional[str], retries_override: int | None = None,
                  timeout_override: float | None = None) -> str:
            proxy_eff = self._effective_proxy(current_proxy, direct)
            budget = retries if retries_override is None else retries_override
            wait_s = timeout if timeout_override is None else timeout_override
            last_error: Exception | None = None
            for attempt in range(budget + 1):
                try:
                    post_headers = dict(headers or {})
                    post_headers.setdefault("Content-Type", "application/json")
                    if self._session is not None:
                        resp = self._retry_insecure(url, lambda verify: self._session.post(
                            url,
                            json=json_body or {},
                            headers=post_headers,
                            timeout=wait_s,
                            verify=verify,
                            proxies={"http": proxy_eff, "https": proxy_eff} if proxy_eff else None,
                        ))
                        if _is_anti_scrape_status(resp.status_code):
                            raise AntiScrapeError(f"反爬响应 HTTP {resp.status_code} {url}")
                        resp.raise_for_status()
                        text = self._response_text(resp, encoding)
                    else:
                        import urllib.request

                        body = _json.dumps(json_body or {}).encode("utf-8")
                        req = urllib.request.Request(
                            url, data=body, headers=post_headers
                        )
                        with _urllib_opener(proxy_eff).open(req, timeout=wait_s) as resp:
                            text = resp.read().decode(encoding or "utf-8", errors="replace")
                    # JSON 数据体不做特征词扫描（词表是给 HTML 挑战页的，
                    # 小说简介里的「被封/验证码」会误报，见 _is_json_document）
                    if not _is_json_document(text) and _is_anti_scrape_text(text):
                        raise AntiScrapeError(f"反爬特征响应 POST（命中：{_anti_scrape_hit(text)}）{url}")
                    return text
                except AntiScrapeError as exc:
                    if proxy_pool is not None:
                        raise
                    last_error = exc
                    if attempt < budget:
                        self._sleeper(min(0.5 * (2 ** attempt), 2.0))
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if attempt < budget:
                        self._sleeper(min(0.5 * (2 ** attempt), 2.0))
            if isinstance(last_error, RequestError):
                raise last_error
            raise RequestError(f"请求失败 POST {url}：{last_error}")

        return self._run_with_proxy_switch(_once, proxy, proxy_pool, f"POST {url}")

    def post_form(
        self,
        url: str,
        form_data: Optional[dict] = None,
        headers: Optional[dict] = None,
        proxy: Optional[str] = None,
        timeout: float | None = None,
        retries: int | None = None,
        encoding: Optional[str] = None,
        proxy_pool: Optional[ProxyPool] = None,
        interval_ms: int | None = None,
        direct: bool = False,
    ) -> str:
        """POST 表单并返回响应文本。encoding 指定响应编码（如utf-8）。
        proxy_pool: 代理IP池，反爬/失败时换IP重试。
        direct: 强制直连（屏蔽默认/系统代理，显式 proxy 仍优先）。"""
        from urllib.parse import urlencode

        if timeout is None:
            timeout = self.defaults.timeout
        if retries is None:
            retries = self.defaults.retries
        if proxy is None:
            proxy = None if direct else self.defaults.proxy
        if interval_ms is None:
            interval_ms = self.defaults.interval_ms
        headers = self._headers_with_ua(headers)
        self._sleeper(interval_ms / 1000.0)

        def _once(current_proxy: Optional[str], retries_override: int | None = None,
                  timeout_override: float | None = None) -> str:
            proxy_eff = self._effective_proxy(current_proxy, direct)
            budget = retries if retries_override is None else retries_override
            wait_s = timeout if timeout_override is None else timeout_override
            last_error: Exception | None = None
            for attempt in range(budget + 1):
                try:
                    post_headers = dict(headers or {})
                    post_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
                    if self._session is not None:
                        resp = self._retry_insecure(url, lambda verify: self._session.post(
                            url,
                            data=urlencode(form_data or {}),
                            headers=post_headers,
                            timeout=wait_s,
                            verify=verify,
                            proxies={"http": proxy_eff, "https": proxy_eff} if proxy_eff else None,
                        ))
                        if _is_anti_scrape_status(resp.status_code):
                            raise AntiScrapeError(f"反爬响应 HTTP {resp.status_code} {url}")
                        resp.raise_for_status()
                        if encoding:
                            resp.encoding = encoding
                        text = resp.text
                    else:
                        import urllib.request

                        body = urlencode(form_data or {}).encode("utf-8")
                        req = urllib.request.Request(url, data=body, headers=post_headers)
                        with _urllib_opener(proxy_eff).open(req, timeout=wait_s) as resp:
                            if encoding:
                                text = resp.read().decode(encoding, errors="replace")
                            else:
                                text = resp.read().decode("utf-8", errors="replace")
                    # JSON 数据体不做特征词扫描（词表是给 HTML 挑战页的，
                    # 小说简介里的「被封/验证码」会误报，见 _is_json_document）
                    if not _is_json_document(text) and _is_anti_scrape_text(text):
                        raise AntiScrapeError(f"反爬特征响应 POST（命中：{_anti_scrape_hit(text)}）{url}")
                    return text
                except AntiScrapeError as exc:
                    if proxy_pool is not None:
                        raise
                    last_error = exc
                    if attempt < budget:
                        self._sleeper(min(0.5 * (2 ** attempt), 2.0))
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if attempt < budget:
                        self._sleeper(min(0.5 * (2 ** attempt), 2.0))
            if isinstance(last_error, RequestError):
                raise last_error
            raise RequestError(f"请求失败 POST {url}：{last_error}")

        return self._run_with_proxy_switch(_once, proxy, proxy_pool, f"POST {url}")

    def _get_once(self, url, headers, proxy, timeout, encoding=None) -> str:
        text = self._get_once_raw(url, headers, proxy, timeout, encoding)
        # acw_sc__v2 阿里云盾 challenge：自动解算 cookie 并重放一次
        if _is_acw_challenge(text):
            cookie = _solve_acw(text)
            if cookie:
                acw_headers = dict(headers or {})
                cookies = acw_headers.pop("Cookie", "")
                cookies = (cookies + "; " if cookies else "") + f"acw_sc__v2={cookie}"
                acw_headers["Cookie"] = cookies
                text = self._get_once_raw(url, acw_headers, proxy, timeout, encoding)
                if not _is_acw_challenge(text):
                    return text
        # JSON 数据体不做特征词扫描（词表是给 HTML 挑战页的，小说简介里的
        # 「被封/验证码」会误报，见 _is_json_document）
        if not _is_json_document(text) and _is_anti_scrape_text(text):
            raise AntiScrapeError(f"反爬特征响应（命中：{_anti_scrape_hit(text)}）{url}")
        # Cloudflare 等边缘节点对瞬时流量偶发返回 200 空 body（首个连接常发，
        # 无 Set-Cookie/无内容，第 2 次请求即正常）。空文本对 HTML 解析无意义，
        # 视为瞬时失败走 retries 重试（间隔 0.5s/1s/2s 避峰），避免把活源误判为空页。
        if not text.strip():
            raise RequestError(f"空响应 GET {url}")
        return text

    def _get_once_raw(self, url, headers, proxy, timeout, encoding=None) -> str:
        session = self._active_session()
        if session is not None:
            proxies = {"http": proxy, "https": proxy} if proxy else None
            resp = self._retry_insecure(url, lambda verify: session.get(
                url, headers=headers, proxies=proxies, timeout=timeout, verify=verify
            ))
            if _is_anti_scrape_status(resp.status_code):
                raise AntiScrapeError(f"反爬响应 HTTP {resp.status_code} {url}")
            resp.raise_for_status()
            return self._response_text(resp, encoding)
        # urllib 降级
        import urllib.request
        import urllib.error

        req = urllib.request.Request(url, headers=headers or {})
        try:
            with _urllib_opener(proxy).open(req, timeout=timeout) as resp:
                if resp.status >= 400:
                    raise RequestError(f"HTTP {resp.status} {url}")
                charset = (resp.headers.get("Content-Type") or "").split("charset=")[-1].strip().lower()
                # 同 requests 分支：Content-Type 无 charset 时 split 结果无效（如 "text/html"）
                if "charset=" not in resp.headers.get("Content-Type", "") or "/" in charset:
                    charset = ""
                if not charset or charset == "iso-8859-1":
                    charset = "utf-8"
                if encoding:
                    charset = encoding
                return resp.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            if _is_anti_scrape_status(exc.code):
                raise AntiScrapeError(f"反爬响应 HTTP {exc.code} {url}") from exc
            raise

    def close(self) -> None:
        if self._impersonate_session is not None:
            self._impersonate_session.close()
            self._impersonate_session = None
        if self._session is not None:
            self._session.close()
            self._session = None

    def get_status(
        self,
        url: str,
        headers: Optional[dict] = None,
        timeout: float | None = None,
        direct: bool = False,
    ) -> int:
        """GET 返回 HTTP 状态码（探活用）。

        与 get_text 不同：任何 HTTP 响应（含 401/403/404）都返回码值，
        只有网络层失败（连接拒/超时/DNS）才抛 RequestError——
        服务端已响应即证明地址可达，鉴权是否通过由后续真实请求判定。
        direct: 强制直连（屏蔽默认/系统代理，显式 proxy 仍优先）。
        """
        if timeout is None:
            timeout = self.defaults.timeout
        proxy = self._effective_proxy(None, direct)
        headers = self._headers_with_ua(headers)
        if self._session is not None:
            resp = self._session.get(
                url, headers=headers, timeout=timeout,
                proxies={"http": proxy, "https": proxy} if proxy else None,
            )
            return int(resp.status_code)
        import urllib.request
        import urllib.error

        req = urllib.request.Request(url, headers=headers)
        try:
            with _urllib_opener(proxy).open(req, timeout=timeout) as resp:
                return int(resp.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)  # HTTP 响应即可达（含 401/40x）

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
