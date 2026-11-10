# -*- coding: utf-8 -*-
"""第三方播放器解析（external_player）。

有些站点自己不托管视频：播放页 `player_aaaa` 只给一个不透明 id（如 32 位 hex），
真实地址在第三方播放器页面里用 JS 解密（omofuna 精品线路实测：`from=mp4` →
`https://art.v2player.top:8989/player/?url=<hash>&…`，页面内
`playData('<base64 密文>','<hex 参数>')` 才是真正的 CDN 直链，解密依赖
crypto-js + 混淆过的 common.js）。

浏览器渲染这条路实测 47s 仍拿不到；这里改为「HTTP 取页面 + Node 里跑站点自己的
解密函数」：拿到页面 → 正则取出 playData 的两个参数 → 在 Node 沙箱里加载站点的
crypto-js/common.js 后调用 playData → 得到直链。整条链路约 1s，且不依赖浏览器。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from urllib.parse import quote, unquote, urlsplit

from framework import http as http_mod
from framework.errors import ContentMissingError

log = logging.getLogger(__name__)

# 解密脚本依赖（crypto-js / 站点 common.js）本地缓存：按 URL 存盘，过期重下。
_DEPS_TTL = 7 * 86400.0
# 第三方播放器整条解析的时间预算：超预算就判失败交给换线，
# 不让一个挂掉的解析站把整次取流拖成几十秒。
_RESOLVE_BUDGET = 16.0
# 单次 HTTP 上限（页面/脚本共用）。art.v2player.top 实测同一个页面
# 6.1~10.9s 才吐完（12KB HTML），6s 上限会稳定超时 → 解析必失败。
_HTTP_TIMEOUT = 12.0
# 解析结果短期缓存：同一个播放页 hash 反复进入时不必再走一遍
# 「取页面 + Node 解密」（实测一次 7~12s）。
_STREAM_CACHE: dict = {}
_STREAM_CACHE_TTL = 900.0

# 解析结果**落盘**缓存：内存缓存一重启就没了，而解析站（art.v2player.top）
# 一挂常常是几十分钟到几小时。这时「用上次解析出的直链」远好过「整条精品线
# 不可用」——直链是 CDN 地址，通常几小时内仍有效。
_DISK_CACHE_NAME = "resolved.json"
_DISK_CACHE_MAX = 200
# 兜底窗口（源可用 stale_ttl_s 覆盖）：超过这个年龄的直链不再当兜底。
_STALE_TTL = 6 * 3600.0
# 解析站传输层失败后的冷却：期间不再尝试取页面，直接走兜底或换线。
# 只对「连不上」生效；页面结构变化（找不到 playData）不进冷却，否则结构一变
# 就再也看不到真实报错。
_HOST_DOWN_TTL = 180.0
_HOST_DOWN: dict = {}
# 每个主机上次成功的出口（"direct"/"proxy"）：解析站的 TLS/端口常常只有一个
# 出口能用，且会在一天之内翻转，所以按主机记忆、下次优先试上次成功的那个。
_HOST_ROUTE: dict = {}

_NODE_RUNNER = r"""
const fs = require('fs');
const vm = require('vm');

const dir = process.argv[2];
const cipher = process.argv[3];
const key = process.argv[4];

function fnStub() {
  return function () { return null; };
}

const ctx = {
  console,
  decodeURIComponent,
  encodeURIComponent,
  String, Number, Math, JSON, parseInt, parseFloat, Date, Array, Object,
  RegExp, Error,
  setInterval: () => 0,
  clearInterval: () => {},
  setTimeout: () => 0,
  clearTimeout: () => {},
};
ctx.window = ctx;
ctx.self = ctx;
ctx.globalThis = ctx;
ctx.document = new Proxy({}, {
  get(target, prop) {
    if (prop === 'readyState') return 'complete';
    if (prop === 'cookie') return '';
    if (prop === 'body' || prop === 'head') {
      return new Proxy({}, { get: () => fnStub(), set: () => true });
    }
    return fnStub();
  },
  set() { return true; },
});
ctx.navigator = { userAgent: 'Mozilla/5.0', platform: 'Win32' };
ctx.location = { href: 'https://localhost/player/', search: '', origin: 'https://localhost' };

vm.createContext(ctx);
for (const name of process.argv[5].split(',')) {
  if (!name) continue;
  try {
    vm.runInContext(fs.readFileSync(dir + '/' + name, 'utf8'), ctx);
  } catch (e) {
    console.error('SCRIPT_WARN:' + name + ':' + (e && e.message));
  }
}
const fn = ctx.playData;
if (typeof fn !== 'function') {
  console.log('NO_PLAYDATA');
  process.exit(2);
}
try {
  console.log('RESULT:' + fn(cipher, key));
} catch (e) {
  console.log('ERR:' + (e && e.message));
  process.exit(3);
}
"""


def node_available() -> str:
    """返回 node 可执行文件路径；没有则空串。"""
    return shutil.which("node") or shutil.which("node.exe") or ""


def _cache_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or tempfile.gettempdir()
    path = os.path.join(base, "claw", "external-player")
    os.makedirs(path, exist_ok=True)
    return path


def _short_error(exc) -> str:
    """异常 → 「类型: 原因」，把 URL 从原因里摘掉。

    原来的报错是 `请求失败 GET <很长的播放器 URL>：<原因>`，日志/界面按 90~120
    字截断时**原因永远被截掉**——线上只能看到半条 URL，看不出到底是超时、
    502 还是证书错（2026-11-02 现场就是这样，白查一轮）。
    """
    text = str(exc)
    text = re.sub(r"^(?:请求失败\s+)?(?:GET|POST)\s+\S+?：", "", text)
    text = re.sub(r"https?://\S+", "<url>", text)
    text = " ".join(text.split())
    return f"{type(exc).__name__}: {text[:180]}" if text else type(exc).__name__


def _host_down_remaining(host: str) -> float:
    """该主机距离冷却结束还有多少秒（0 = 没在冷却）。"""
    if not host:
        return 0.0
    until = _HOST_DOWN.get(host) or 0.0
    return max(0.0, until - time.monotonic())


def _mark_host_down(host: str) -> None:
    if host:
        _HOST_DOWN[host] = time.monotonic() + _HOST_DOWN_TTL


def _disk_path() -> str:
    return os.path.join(_cache_dir(), _DISK_CACHE_NAME)


def _disk_read() -> dict:
    try:
        with open(_disk_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _disk_write(data: dict) -> None:
    """原子写（先写临时文件再替换），最多保留 _DISK_CACHE_MAX 条。"""
    target = _disk_path()
    tmp = target + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _disk_get(key: str):
    """返回 (url, 年龄秒)；没有则 (None, 0)。"""
    entry = _disk_read().get(key)
    if not isinstance(entry, dict):
        return None, 0.0
    url = str(entry.get("url") or "")
    if not url.startswith(("http://", "https://")):
        return None, 0.0
    try:
        age = max(0.0, time.time() - float(entry.get("ts") or 0.0))
    except (TypeError, ValueError):
        return None, 0.0
    return url, age


def _disk_put(key: str, url: str, stale_ttl: float) -> None:
    now = time.time()
    data = _disk_read()
    data[key] = {"url": url, "ts": now}
    if len(data) > _DISK_CACHE_MAX:
        ordered = sorted(
            ((k, v) for k, v in data.items() if isinstance(v, dict)),
            key=lambda item: float(item[1].get("ts") or 0.0),
            reverse=True,
        )
        data = dict(ordered[:_DISK_CACHE_MAX])
    # 顺手清掉远超兜底窗口的旧条目，别让文件无限长
    horizon = max(stale_ttl, _STALE_TTL) * 2
    for name in [k for k, v in data.items()
                 if isinstance(v, dict) and now - float(v.get("ts") or 0.0) > horizon]:
        data.pop(name, None)
    _disk_write(data)


class ExternalPlayer:
    """按源配置解析第三方播放器页面里的加密直链。"""

    def __init__(self, http, log_=None):
        self._http = http
        self._log = log_ or log
        self._started = time.monotonic()
        self._budget = _RESOLVE_BUDGET
        self._http_timeout = _HTTP_TIMEOUT

    def _left(self) -> float:
        """本次解析剩余时间预算（秒）。"""
        return self._budget - (time.monotonic() - self._started)

    # ------------------------------------------------------------------ #
    def _download(self, url: str, headers: dict, name: str) -> str:
        """取脚本依赖并缓存到本地（7 天）。返回本地文件名。"""
        target = os.path.join(_cache_dir(), name)
        try:
            if time.time() - os.path.getmtime(target) < _DEPS_TTL:
                return name
        except OSError:
            pass
        content = self._http.get_text(
            url, headers=headers, retries=1,
            timeout=max(3.0, min(_HTTP_TIMEOUT, self._left())),
        )
        if not content:
            raise ContentMissingError(f"解密依赖为空：{url}")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(content)
        return name

    def _fetch_page(self, url: str, headers: dict) -> str:
        """取播放器页面：按「上次成功的出口」优先，另一个出口再试一次。

        现场（2026-11-02）：`art.v2player.top:8989` 直连 TCP 超时、经系统代理
        502/RemoteDisconnected，而前一天同一个域名直连 200。出口好坏会翻转，
        所以不能先把主机钉死在某一个出口上（旧实现声明 prefer_direct 之后
        就只走直连，等于放弃了另一半成功率）。
        """
        host = urlsplit(url).hostname or ""
        order = ["direct", "proxy"]
        if _HOST_ROUTE.get(host) == "proxy":
            order = ["proxy", "direct"]
        errors = []
        for mode in order:
            if self._left() <= 1.5:
                break
            # 单个出口的超时不超过剩余预算（下限 2s，别把最后一次机会压成 0.2s）
            timeout = max(2.0, min(self._http_timeout, self._left()))
            try:
                if mode == "direct":
                    text = self._http.get_text(
                        url, headers=headers, retries=0, timeout=timeout, direct=True
                    )
                else:
                    proxy = http_mod.system_proxy_url()
                    if not proxy:
                        continue
                    text = self._http.get_text(
                        url, headers=headers, retries=0, timeout=timeout, proxy=proxy
                    )
            except Exception as exc:  # noqa: BLE001 —— 换出口再试
                from framework.stream_resolver import StreamCancelledError

                if isinstance(exc, (StreamCancelledError, TimeoutError)):
                    raise
                errors.append(exc)
                continue
            if text:
                _HOST_ROUTE[host] = mode
                # 同一出口后续请求免探测（下次仍以它为优先，但保留换出口机会）
                if mode == "direct":
                    http_mod.note_direct_preferred(url)
                else:
                    http_mod.note_proxy_preferred(url)
                self._log.info(
                    "external_player 页面出口=%s 主机=%s", mode, host or "?"
                )
                return text
        if errors:
            _mark_host_down(host)
            raise ContentMissingError(
                "第三方播放器页面获取失败：" + "；".join(_short_error(e) for e in errors)
            )
        raise ContentMissingError("第三方播放器页面为空")

    def _probe_playable(self, url: str, headers: dict) -> bool:
        """兜底直链先确认还能取到字节（CDN 的签名地址会过期）。

        两个出口都试：兜底链路的出口偏好和解析站不一样（解析站在国内机房里，
        CDN 可能在境外），只按直连判断会把本来能播的地址误判成失效。
        """
        for mode in ("direct", "proxy"):
            if self._left() <= 1.0:
                return False
            timeout = max(1.5, min(3.0, self._left()))
            try:
                if mode == "direct":
                    data = self._http.get_bytes(
                        url, headers=dict(headers, Range="bytes=0-2047"),
                        retries=0, timeout=timeout, direct=True,
                    )
                else:
                    proxy = http_mod.system_proxy_url()
                    if not proxy:
                        continue
                    data = self._http.get_bytes(
                        url, headers=dict(headers, Range="bytes=0-2047"),
                        retries=0, timeout=timeout, proxy=proxy,
                    )
            except Exception:  # noqa: BLE001 —— 换个出口再判
                continue
            if data:
                return True
        return False

    def _run_node(self, files: list[str], cipher: str, key: str) -> str:
        node = node_available()
        if not node:
            raise ContentMissingError("该线路需要 Node.js 解密（未检测到 node）")
        workdir = _cache_dir()
        runner = os.path.join(workdir, "runner.js")
        if not os.path.exists(runner):
            with open(runner, "w", encoding="utf-8") as handle:
                handle.write(_NODE_RUNNER)
        proc = subprocess.run(
            [node, runner, workdir, cipher, key, ",".join(files)],
            capture_output=True,
            text=True,
            timeout=max(5.0, min(20.0, self._left())),
            encoding="utf-8",
            errors="replace",
        )
        out = (proc.stdout or "").strip()
        if out.startswith("RESULT:"):
            return out[len("RESULT:"):].strip()
        if out.startswith("ERR:"):
            raise ContentMissingError(f"第三方播放器解密失败：{out[4:120]}")
        if "NO_PLAYDATA" in out:
            raise ContentMissingError("第三方播放器页面结构变化（未找到 playData）")
        raise ContentMissingError(
            f"第三方播放器解密无结果（node 退出码 {proc.returncode}）"
        )

    # ------------------------------------------------------------------ #
    def resolve(self, cfg: dict, player: dict, episode_url: str, page_html: str = "") -> str:
        """返回真实媒体直链；cfg 来自 play_url.external_player。"""
        if not cfg:
            return ""
        started = time.monotonic()
        # 源可用 budget_s 收紧本次解析预算（第三方播放器挂掉时尽快换线）
        try:
            self._budget = float(cfg.get("budget_s") or _RESOLVE_BUDGET)
        except (TypeError, ValueError):
            self._budget = _RESOLVE_BUDGET
        try:
            self._http_timeout = float(cfg.get("http_timeout_s") or _HTTP_TIMEOUT)
        except (TypeError, ValueError):
            self._http_timeout = _HTTP_TIMEOUT
        self._started = started

        sources = [str(item).lower() for item in (cfg.get("from") or [])]
        from_tag = str((player or {}).get("from") or "").lower()
        if sources and from_tag not in sources:
            return ""
        raw = str((player or {}).get(cfg.get("url_path") or "url") or "")
        if not raw:
            return ""
        # player_aaaa.url 常是**逐字符 percent 编码**过的（omofuna 实测：
        # `%66%64%65…` 就是 hash 的 fde8…），先解一层再拼查询串，
        # 否则会二次编码成 %2566…，播放器页面拿不到正确参数。
        if "%" in raw:
            raw = unquote(raw)
        # 已经是直链（含协议）就不用再走播放器
        if raw.startswith(("http://", "https://")):
            return raw

        template = str(cfg.get("player_url") or "")
        if not template:
            return ""
        vod = (player or {}).get("vod_data") or {}
        image_base = str(cfg.get("image_base") or "")
        pic = str(vod.get("vod_pic") or "")
        if pic.startswith("mac://"):
            bare = pic[len("mac://"):].lstrip("/")
            pic = f"{image_base.rstrip('/')}/{bare}"
        elif pic.startswith("//"):
            pic = "https:" + pic
        elif pic and not pic.startswith(("http://", "https://")):
            pic = f"{image_base.rstrip('/')}/{pic.lstrip('/')}"
        origin = ""
        try:
            parts = urlsplit(episode_url)
            origin = f"{parts.scheme}://{parts.netloc}"
        except Exception:  # noqa: BLE001 —— 畸形 URL 就留空
            origin = ""

        player_url = (
            template.replace("{enc_url}", quote(raw, safe=""))
            .replace("{url}", quote(raw, safe=""))
            .replace("{sid}", str((player or {}).get("sid") or ""))
            .replace("{nid}", str((player or {}).get("nid") or ""))
            .replace("{name}", quote(str(vod.get("vod_name") or ""), safe=""))
            .replace("{pic}", quote(pic, safe=""))
            .replace("{ph}", quote(image_base, safe=""))
            .replace("{h}", quote(origin, safe=""))
            .replace("{dmid}", str((player or {}).get("dmid") or vod.get("dmid") or ""))
        )
        headers = dict(cfg.get("headers") or {})
        headers.setdefault("Referer", origin or "https://www.omofuna.com/")
        # 同一播放页 hash 短期命中缓存：解析站一次要 7~12s，重进/拖动进度条
        # 再走一遍太慢（config: cache_ttl_s，默认 900s，0 = 不缓存）。
        try:
            ttl = float(cfg.get("cache_ttl_s", _STREAM_CACHE_TTL))
        except (TypeError, ValueError):
            ttl = _STREAM_CACHE_TTL
        if ttl > 0:
            hit = _STREAM_CACHE.get(player_url)
            if hit and time.monotonic() - hit[0] < ttl:
                self._log.info("external_player 缓存命中：%s", hit[1][:90])
                return hit[1]
            disk_url, disk_age = _disk_get(player_url)
            if disk_url and disk_age < ttl:
                self._log.info(
                    "external_player 落盘缓存命中（%.0f 分钟前）：%s",
                    disk_age / 60.0, disk_url[:90],
                )
                _STREAM_CACHE[player_url] = (time.monotonic(), disk_url)
                return disk_url
        try:
            stale_ttl = float(cfg.get("stale_ttl_s", _STALE_TTL))
        except (TypeError, ValueError):
            stale_ttl = _STALE_TTL
        try:
            url = self._resolve_from_page(cfg, player_url, headers, ttl, stale_ttl)
        except ContentMissingError as exc:
            # 解析站不可用（连不上/502/证书）时，用上一次成功解析出的直链兜底：
            # CDN 地址通常几小时内仍有效，比"整条精品线不可用"好得多。
            stale_url, stale_age = _disk_get(player_url)
            if not stale_url or stale_age > stale_ttl:
                raise
            if not self._probe_playable(stale_url, headers):
                self._log.warning(
                    "external_player 兜底直链已失效（%.0f 分钟前）：%s",
                    stale_age / 60.0, stale_url[:90],
                )
                raise
            self._log.warning(
                "external_player 解析站不可用（%s），改用 %.0f 分钟前解析出的直链：%s",
                exc, stale_age / 60.0, stale_url[:90],
            )
            return stale_url
        return url

    def _resolve_from_page(self, cfg: dict, player_url: str, headers: dict,
                           ttl: float, stale_ttl: float) -> str:
        """取播放器页面 → 解出直链（不含兜底逻辑）。"""
        if cfg.get("prefer_direct"):
            # 解析站主机常与站点主域不同出口：源声明后把依赖也标记成直连优先，
            # 省掉一次注定失败的代理探测；但 `_fetch_page` 仍保留换出口重试。
            try:
                for dep in [player_url] + [str(item) for item in (cfg.get("scripts") or [])]:
                    http_mod.note_direct_preferred(dep)
            except Exception:  # noqa: BLE001 —— 只是优化，失败照常请求
                pass
        host = urlsplit(player_url).hostname or ""
        cooling = _host_down_remaining(host)
        if cooling > 0:
            raise ContentMissingError(
                f"第三方播放器主机 {host} 冷却中（{cooling:.0f}s 前刚连不上）"
            )
        try:
            attempts = max(1, int(cfg.get("attempts") or 1))
        except (TypeError, ValueError):
            attempts = 1
        page = ""
        last_error: Exception | None = None
        for attempt in range(attempts):
            if self._left() <= 1.5:
                break
            try:
                page = self._fetch_page(player_url, headers)
            except ContentMissingError as exc:
                last_error = exc
                if attempt + 1 < attempts and self._left() > 3.0:
                    time.sleep(min(1.2, 0.4 * (attempt + 1)))
                continue
            if page:
                break
        if not page:
            if last_error is not None:
                raise last_error
            raise ContentMissingError("第三方播放器页面为空")

        pattern = str(cfg.get("cipher_regex") or r"playData\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)")
        match = re.search(pattern, page)
        if not match:
            raise ContentMissingError("第三方播放器页面未找到 playData 密文")
        cipher, key = match.group(1), match.group(2)

        scripts = list(cfg.get("scripts") or [])
        if not scripts:
            raise ContentMissingError("未配置第三方播放器解密依赖脚本")
        names = []
        cache_headers = dict(headers)
        for url in scripts:
            name = os.path.basename(urlsplit(url).path) or "dep.js"
            self._download(url, cache_headers, name)
            names.append(name)
        url = self._run_node(names, cipher, key)
        if not url.startswith(("http://", "https://")):
            raise ContentMissingError(f"第三方播放器返回的不是直链：{url[:80]}")
        self._log.info("external_player 解析成功：%s", url[:90])
        if ttl > 0:
            now = time.monotonic()
            for stale in [k for k, (t, _) in _STREAM_CACHE.items() if now - t >= ttl]:
                _STREAM_CACHE.pop(stale, None)
            _STREAM_CACHE[player_url] = (now, url)
            _disk_put(player_url, url, stale_ttl)
        return url


def resolve_external_player(http, cfg: dict, player: dict, episode_url: str) -> str:
    """模块级便捷入口（供 content.video 调用）。"""
    return ExternalPlayer(http).resolve(cfg, player, episode_url)
