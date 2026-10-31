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

from framework.errors import ContentMissingError

log = logging.getLogger(__name__)

# 解密脚本依赖（crypto-js / 站点 common.js）本地缓存：按 URL 存盘，过期重下。
_DEPS_TTL = 7 * 86400.0
# 第三方播放器整条解析的时间预算：超预算就判失败交给换线，
# 不让一个挂掉的解析站把整次取流拖成几十秒。
_RESOLVE_BUDGET = 16.0
# 单次 HTTP 上限（页面/脚本共用）
_HTTP_TIMEOUT = 6.0

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


class ExternalPlayer:
    """按源配置解析第三方播放器页面里的加密直链。"""

    def __init__(self, http, log_=None):
        self._http = http
        self._log = log_ or log
        self._started = time.monotonic()
        self._budget = _RESOLVE_BUDGET

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
        page = self._http.get_text(
            player_url, headers=headers, retries=1,
            timeout=max(3.0, min(_HTTP_TIMEOUT, self._left())),
        )
        if not page:
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
        return url


def resolve_external_player(http, cfg: dict, player: dict, episode_url: str) -> str:
    """模块级便捷入口（供 content.video 调用）。"""
    return ExternalPlayer(http).resolve(cfg, player, episode_url)
