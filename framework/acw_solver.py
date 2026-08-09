# -*- coding: utf-8 -*-
"""完整 acw_sc__v2 解算器：从 challenge HTML 提取 arg1，生成 acw_sc__v2 cookie 值。

算法（已从 17k 的 aliyunwaf_6a6f5ea8 混淆 JS 还原）：
1. 提取 arg1（40 位 hex）与 _0x4818 混淆数组、旋转量、各调用 key
2. RC4 解出方法名字符串与 XOR key（固定 40 位数字串）
3. arg1.unsbox() 置换重排（_0x4b082b 表）
4. 重排结果.hexXor(key) 逐字节 XOR
5. 结果即 acw_sc__v2 cookie 值
"""
from __future__ import annotations

import base64
import re
from urllib.parse import quote

# ---- JS 语义工具 ----


def rc4_chars(data: str, key: str) -> str:
    """JS 语义 RC4：输入字符串按 charCodeAt 处理。"""
    S = list(range(256))
    j = 0
    kb = [ord(c) for c in key]
    for i in range(256):
        j = (j + S[i] + kb[i % len(kb)]) % 256
        S[i], S[j] = S[j], S[i]
    out = []
    i = j = 0
    for ch in data:
        i = (i + 1) % 256
        j = (j + S[i]) % 256
        S[i], S[j] = S[j], S[i]
        out.append(chr(ord(ch) ^ S[(S[i] + S[j]) % 256]))
    return "".join(out)


def _b64_to_str(s: str) -> str:
    """JS: atob → %XX → decodeURIComponent。"""
    s = s.rstrip("=")
    b = base64.b64decode(s + "=" * (-len(s) % 4))
    return b.decode("utf-8", errors="strict")


def acw_decrypt_arr(elem: str, key: str) -> str:
    return rc4_chars(_b64_to_str(elem), key)


# ---- 置换表（_0x4b082b，固定 40 元素）----
_UNSBOX_TABLE = [
    0xF, 0x23, 0x1D, 0x18, 0x21, 0x10, 0x01, 0x26,
    0x0A, 0x09, 0x13, 0x1F, 0x28, 0x1B, 0x16, 0x17,
    0x19, 0x0D, 0x06, 0x0B, 0x27, 0x12, 0x14, 0x08,
    0x0E, 0x15, 0x20, 0x1A, 0x02, 0x1E, 0x07, 0x04,
    0x11, 0x05, 0x03, 0x1C, 0x22, 0x25, 0x0C, 0x24,
]

_KEY_HEX = "3000176000856006061501533003690027800375"  # 17k 固定 key（每 challenge 相同？）


def unsbox(arg1: str) -> str:
    """JS String.prototype.unsbox：按表重排字符（表值=原位置+1 → 新位置 i）。"""
    out = [""] * 40
    for i, v in enumerate(_UNSBOX_TABLE):
        out[i] = arg1[v - 1]
    return "".join(out)


def hex_xor(s: str, key: str) -> str:
    """JS String.prototype.hexXor：逐字节 XOR（每 2 hex 字符一个字节）。"""
    out = []
    for i in range(0, min(len(s), len(key)), 2):
        a = int(s[i : i + 2], 16)
        b = int(key[i : i + 2], 16)
        v = a ^ b
        h = format(v, "x")
        if len(h) == 1:
            h = "0" + h
        out.append(h)
    return "".join(out)


def solve_acw_cookie(arg1: str, key_hex: str = _KEY_HEX) -> str:
    """arg1 → acw_sc__v2 cookie 值。"""
    return hex_xor(unsbox(arg1), key_hex)


def extract_arg1(challenge_html: str) -> str | None:
    m = re.search(r"var arg1='([0-9A-Fa-f]+)'", challenge_html)
    return m.group(1) if m else None


def extract_xor_key(challenge_html: str) -> str | None:
    """从混淆 JS 提取 XOR key（_0x55f3('0x3') 对应值）。"""
    m = re.search(r"var _0x4818=\[([^\]]+)\]", challenge_html)
    if not m:
        return None
    arr = re.findall(r"'((?:\\x[0-9a-f]{2})+)'", m.group(1))
    arr = [bytes(a, "latin1").decode("unicode_escape") for a in arr]
    rot = 0x15B % len(arr)
    arr = arr[rot:] + arr[:rot]
    # 找 '0x3' 的调用 key
    km = re.search(r"_0x55f3\('0x3',\s*'((?:\\x[0-9a-f]{2})+)'\)", challenge_html)
    if not km:
        return None
    key = bytes(km.group(1), "latin1").decode("unicode_escape")
    val = acw_decrypt_arr(arr[3], key)
    if re.fullmatch(r"[0-9]+", val):
        return val
    return None


if __name__ == "__main__":
    import requests

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.17k.com/",
    }
    url = "https://www.17k.com/book/493239.html"
    r = requests.get(url, headers=HEADERS, timeout=15)
    html = r.text
    arg1 = extract_arg1(html)
    print("arg1:", arg1)
    key = extract_xor_key(html)
    print("key:", key)
    if arg1:
        cookie = solve_acw_cookie(arg1, key or _KEY_HEX)
        print("acw_sc__v2:", cookie)
        # 带 cookie 重放
        r2 = requests.get(url, headers=HEADERS, cookies={"acw_sc__v2": cookie}, timeout=15)
        print("重放 status:", r2.status_code, "len:", len(r2.text))
        print("是正常详情页:", "og:title" in r2.text and "aliyunwaf" not in r2.text[:3000])
