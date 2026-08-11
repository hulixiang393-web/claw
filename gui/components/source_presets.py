"""源预设库（source_presets.py）。

给源编辑器提供「开箱即用」素材，全部为纯数据 / 纯函数，不依赖 Qt：

1. default_source_template(ctype)  新建源时的默认配置（字符集、请求头、重试退避、
   并发与超时、自检等静态默认值）。
2. auto_network_defaults(...)      按已填的基础信息自动补齐简单逻辑
   （Referer 自动取站点根域、字符集默认 utf-8、常用请求头默认值）。
3. DECRYPT_PRESETS                内置常用解密逻辑模板：Base64 变体 / XOR /
   AES-CBC / AES-ECB / 字库映射 / 自定义解密接口，附 JS 参考片段。
4. RENDER_PRESETS                 常用 Playwright 渲染配置预设（按 mode 维度）。

本文件只做数据与规则，编辑器负责把结果应用到控件。
"""

from __future__ import annotations

from urllib.parse import urlsplit

# 默认 User-Agent（PC Chrome）
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 类型 → 正文块名（与编辑器一致）
_BLOCK = {"novel": "chapter", "comic": "page", "video": "episode"}


def default_source_template(ctype: str) -> dict:
    """新建源骨架，带安全默认值（后处理/解码/渲染均可在编辑器再改）。"""
    block = _BLOCK.get(ctype, "chapter")
    return {
        "$schema_version": 2,
        "$id": "",
        "$type": ctype,
        "$name": "",
        "$enabled": False,
        "$weight": 1.0,
        "$metadata": {"homepage": "", "description": "", "tags": []},
        "transports": {
            "base_url": "",
            "timeout": 30,
            "retries": 3,
            "interval_ms": 500,
            "follow_redirects": True,
            "charset": "utf-8",
            "retry_backoff": {"base_sec": 1.0, "max_sec": 60.0, "jitter": 0.3},
            "headers": {
                "User-Agent": DEFAULT_UA,
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        },
        "endpoints": {"detail": {"fields": {}}, "content": {}},
        "constraints": {
            "search": {"max_pages": 1, "max_results": 20},
            "detail": {"max_pages": 1, "timeout_per_page_sec": 30},
            block: {"max_pages": 50, "max_items": 500},
            "max_concurrency": 4,
            "global": {"total_timeout_sec": 600},
        },
        "diagnostics": {
            "selfcheck": {"strategy": "soft", "max_failures": 5, "interval_hours": 24}
        },
    }


def origin_of(url: str) -> str:
    """取站点根（scheme://host）作为默认 Referer。"""
    p = urlsplit(url.strip())
    if p.scheme and p.netloc:
        return f"{p.scheme}://{p.netloc}"
    return url.strip()


def auto_network_defaults(base_url="", referer="", charset="",
                          accept="", accept_lang="") -> dict:
    """按用户已填信息给出建议补齐的请求默认值（仅在对应项为空时建议）。

    返回可填 `{referer, charset, accept, accept_lang}` 的子集，供编辑器就地应用。
    """
    out = {}
    if not (referer or "").strip() and base_url.strip():
        out["referer"] = origin_of(base_url)
    if not (charset or "").strip():
        out["charset"] = "utf-8"
    if not (accept or "").strip():
        out["accept"] = "*/*"
    if not (accept_lang or "").strip():
        out["accept_lang"] = "zh-CN,zh;q=0.9"
    return out


# --------------------------------------------------------------------------- #
# 内置解密逻辑预设
# --------------------------------------------------------------------------- #
# 每条：name（下拉文案）、target（image/content/chapter，对应解密 Tab 的一段）、
# strategy（策略标识）、values（模板默认参数）、note（用途说明）、
# js（可直接借鉴/放入页内解析的 JS 参考片段）。
DECRYPT_PRESETS: list[dict] = [
    {
        "name": "Base64 · 标准解码",
        "target": "content",
        "strategy": "base64_decode",
        "note": "正文以标准 base64 编码。",
        "values": {},
        "js": (
            "// 先取正文文本，去掉换行空白再解码\n"
            "var b64 = document.body.innerText.replace(/\\s+/g, '');\n"
            "var raw = atob(b64);\n"
            "// 按字节还原为 utf-8 文本\n"
            "var bytes = Uint8Array.from(raw, function (c) { return c.charCodeAt(0); });\n"
            "new TextDecoder('utf-8').decode(bytes);"
        ),
    },
    {
        "name": "Base64 · URL-safe 变体",
        "target": "content",
        "strategy": "base64_decode",
        "note": "URL-safe base64（- 代替 +、_ 代替 /），需先还原再解码。",
        "values": {},
        "js": (
            "function b64UrlToStr(s) {\n"
            "  s = s.replace(/-/g, '+').replace(/_/g, '/');\n"
            "  while (s.length % 4) s += '=';\n"
            "  return decodeURIComponent(escape(atob(s)));\n"
            "}\n"
            "// 用法：b64UrlToStr(ct);"
        ),
    },
    {
        "name": "XOR · 图片字节异或",
        "target": "image",
        "strategy": "xor",
        "note": "图片字节流做异或混淆，填入密钥（UTF-8）。",
        "values": {"key": "demo_key", "output": "png"},
        "js": (
            "// 思路：对解码后的字节逐字节与密钥循环异或\n"
            "function xorDecrypt(buf, key) {\n"
            "  var kb = new TextEncoder().encode(key), out = new Uint8Array(buf.length);\n"
            "  for (var i = 0; i < buf.length; i++) out[i] = buf[i] ^ kb[i % kb.length];\n"
            "  return out;\n"
            "}"
        ),
    },
    {
        "name": "AES-CBC · 图片分片解密",
        "target": "image",
        "strategy": "aes_cbc",
        "note": "图片分片 AES-CBC 加密，需密钥/IV（UTF-8 或 base64）。",
        "values": {"key": "0123456789abcdef", "iv": "0000000000000000", "padding": "pkcs7"},
        "js": (
            "// 若站内用 CryptoJS，可参考取其密钥逻辑\n"
            "// (function () {\n"
            "//   var key = CryptoJS.enc.Utf8.parse('0123456789abcdef');\n"
            "//   var iv  = CryptoJS.enc.Utf8.parse('0000000000000000');\n"
            "//   var dec = CryptoJS.lib.WordArray.create(buf);\n"
            "//   return CryptoJS.AES.decrypt(dec, key, {iv: iv, padding: CryptoJS.pad.Pkcs7})\n"
            "//        .toString(CryptoJS.enc.Hex);\n"
            "// })()"
        ),
    },
    {
        "name": "AES-ECB · 图片分片解密",
        "target": "image",
        "strategy": "aes_ecb",
        "note": "AES-ECB（无 IV）图片混淆。",
        "values": {"key": "0123456789abcdef", "padding": "pkcs7"},
        "js": (
            "// ECB 无需 IV：同一 WordArray/AES 解后转回字节数组写盘即可\n"
            "// var key = CryptoJS.enc.Utf8.parse('0123456789abcdef');\n"
            "// var dec = CryptoJS.AES.decrypt(ct, key, {padding: CryptoJS.pad.Pkcs7});"
        ),
    },
    {
        "name": "字库映射 · 章节标题混淆",
        "target": "chapter",
        "strategy": "translit",
        "note": "标题用了字体字形混淆（如番茄），填映射模块路径。",
        "values": {"map_module": "tools.fontmap"},
        "js": (
            "// 思路：站内一般内嵌 charmap 或经 css unicode-range 控制字形\n"
            "// 取到字符 -> 序号映射后 write 到共享模块：\n"
            "// tools.fontmap = { map: {...}, decode: function(s){...} };"
        ),
    },
{
        "name": "自定义解密接口 · 正文",
        "target": "content",
        "strategy": "custom_endpoint",
        "note": "把正文 POST 到解密接口换取明文，填接口与请求/响应字段。",
        "values": {
            "endpoint": "/api/decrypt",
            "request_field": "data",
            "response_field": "data.text",
        },
        "js": (
            "// 后端解密接口示例（配合 request_field / response_field）\n"
            "// POST /api/decrypt {\"data\": \"<密文>\"}\n"
            "// -> {\"data\": {\"text\": \"<明文>\"}}"
        ),
    },
    {
        "name": "自定义 JS 解密 · 通用",
        "target": "content",
        "strategy": "js_custom",
        "note": "填一段 JS 函数（entry 默认 decrypt），data 为内容串、params 放密钥，返回明文。",
        "values": {
            "entry": "decrypt",
            "script": (
                "function decrypt(data, params) {\n"
                "  // 把站点内解密逻辑抄来这里，密钥从 params 取\n"
                "  var key = params.key || '';\n"
                "  return data; // 改为真正的解密结果\n"
                "}"
            ),
            "params": {"key": ""},
        },
        "js": (
            "// 通用约定：\n"
            "//   入口函数签名 decrypt(data, params)，返回明文（文本）或 base64（字节）。\n"
            "//   params 从源 JSON decryption.targets.<t>.params 传入（可放密钥/偏移）。\n"
            "//   支持任意 JS：XOR、AES(CryptoJS)、字符映射、自研算法均可。\n"
            "function decrypt(data, params) {\n"
            "  // 示例：先对字符做偏移再返回\n"
            "  var out = '', k = (params.key || 'k').charCodeAt(0);\n"
            "  for (var i = 0; i < data.length; i++)\n"
            "    out += String.fromCharCode(data.charCodeAt(i) ^ k);\n"
            "  return out;\n"
            "}"
        ),
    },
]


# --------------------------------------------------------------------------- #
# 常用渲染（Playwright）预设
# --------------------------------------------------------------------------- #
# 每条：name、modes（适用正文模式，与编辑器渲染组 mode 一致：search/chapter/page/episode）、
# config（render_config，键须与对应渲染组的 specs 对齐，缺失键自动忽略）。
RENDER_PRESETS: list[dict] = [
    {
        "name": "常规待渲染页·等 DOM",
        "modes": ["search", "chapter", "page", "episode"],
        "config": {
            "wait_until": "domcontentloaded",
            "timeout_ms": 30000,
            "extra_delay_ms": 2000,
        },
    },
    {
        "name": "资源加载完·networkidle",
        "modes": ["search", "chapter", "page", "episode"],
        "config": {
            "wait_until": "networkidle",
            "timeout_ms": 45000,
            "extra_delay_ms": 4000,
        },
    },
    {
        "name": "漫画·懒加载滚动到底",
        "modes": ["page"],
        "config": {
            "extract_mode": "img",
            "scroll_to_bottom": True,
            "wheel_scroll": True,
            "scroll_step_px": 2000,
            "scroll_stale_rounds": 5,
            "wait_until": "load",
            "timeout_ms": 45000,
            "extra_delay_ms": 3000,
        },
    },
    {
        "name": "漫画·局部 canvas 渲染",
        "modes": ["page"],
        "config": {
            "extract_mode": "canvas",
            "wait_for": "canvas",
            "timeout_ms": 45000,
            "extra_delay_ms": 3000,
        },
    },
    {
        "name": "视频·播放配置页（m3u8）",
        "modes": ["episode"],
        "config": {
            "wait_until": "networkidle",
            "timeout_ms": 60000,
            "extra_delay_ms": 8000,
        },
    },
    {
        "name": "SPA 搜索·交互输入关键词",
        "modes": ["search"],
        "config": {
            "interact": True,
            "input_selector": "input[name=wd]",
            "wait_until": "domcontentloaded",
            "timeout_ms": 30000,
            "extra_delay_ms": 2000,
            "filter_keyword": True,
        },
    },
]