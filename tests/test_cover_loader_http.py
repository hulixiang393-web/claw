# -*- coding: utf-8 -*-
"""CoverLoader 收敛 HttpClient 测试（test_cover_loader_http.py）。

覆盖：正文图/封面下载优先走 HttpClient.get_bytes（携带精确 Referer），
显式 referer 优先于域名规则兜底；HttpClient 失败回退 QNAM 原路径；AES 解密
通道在收敛后仍生效；未注入时用模块级共享实例。全部 mock，不联网。
"""
import os
import sys
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtCore import QEventLoop, QTimer

from framework.errors import RequestError
from gui.components import cover_loader as cl


def _wait_until(cond, timeout=4000):
    """轮询事件循环直到条件成立（worker 结果经 Qt 信号队列回主线程）。"""
    loop = QEventLoop()

    def _poll():
        if cond():
            loop.quit()
        else:
            QTimer.singleShot(25, _poll)

    QTimer.singleShot(25, _poll)
    QTimer.singleShot(timeout, loop.quit)
    loop.exec()


def _png_bytes():
    """生成一张真实 PNG 字节（离屏可解码）。"""
    from PySide6.QtCore import QBuffer
    from PySide6.QtGui import QImage

    img = QImage(8, 8, QImage.Format_RGB32)
    img.fill(0xFF3366AA)
    buf = QBuffer()
    buf.open(QBuffer.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def _aes_cbc_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-CBC PKCS7 加密（与 decryption.image 配置对齐）。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    pad = 16 - len(data) % 16
    padded = data + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(padded) + enc.finalize()


class _FakeHttp:
    """记录 get_bytes 调用参数；result 可为字节或异常。"""

    def __init__(self, result=b""):
        self.result = result
        self.calls = []

    def get_bytes(self, url, headers=None, proxy=None, timeout=None, retries=None, proxy_pool=None, direct=False):
        self.calls.append(dict(url=url, headers=dict(headers or {}), proxy=proxy, timeout=timeout, retries=retries, direct=direct))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_http_path_uses_exact_referer(_qapp):
    """正文图收敛后携带精确章节 URL Referer 走 HttpClient.get_bytes。"""
    loader = cl._CoverLoader()
    http = _FakeHttp(_png_bytes())
    loader.use_http(http)
    got = {}
    loader.load(
        "http://img.cdn/1.jpg",
        lambda pm: got.setdefault("pm", pm),
        referer="http://c.example/m/1/2.html",
        cache=False,
    )
    _wait_until(lambda: "pm" in got)
    assert got["pm"] is not None and not got["pm"].isNull()
    assert http.calls, "应走 HttpClient.get_bytes"
    call = http.calls[0]
    assert call["url"] == "http://img.cdn/1.jpg"
    assert call["headers"]["Referer"] == "http://c.example/m/1/2.html"
    assert call["headers"]["User-Agent"]


def test_http_path_domain_referer_fallback(_qapp):
    """未显式 referer → 域名规则兜底（cdndm5.com → manben 站点根）。"""
    loader = cl._CoverLoader()
    http = _FakeHttp(_png_bytes())
    loader.use_http(http)
    got = {}
    loader.load("http://manhua.cdndm5.com/1.jpg", lambda pm: got.setdefault("pm", pm), cache=False)
    _wait_until(lambda: "pm" in got)
    assert http.calls[0]["headers"]["Referer"] == "https://www.manben.com/"


def test_http_failure_falls_back_to_qnam(_qapp):
    """HttpClient 失败 → 回退 QNAM 原路径（use_http=False 防死循环）。"""
    loader = cl._CoverLoader()
    loader.use_http(_FakeHttp(RequestError("boom")))
    dispatched = []
    loader._dispatch_qnam = lambda item: dispatched.append(item)
    loader.load("http://img/1.jpg", lambda pm: None, cache=False)
    _wait_until(lambda: bool(dispatched))
    assert dispatched
    item = dispatched[0]
    assert item[0] == "http://img/1.jpg"
    assert item[8] is False


def test_http_path_keeps_aes_decrypt_channel(_qapp):
    """收敛后 AES 解密通道不变：解密后的密文可解码出 pixmap。"""
    from framework.config import SourceConfig

    raw = {
        "$schema_version": 2,
        "$id": "t",
        "$type": "comic",
        "$name": "t",
        "$enabled": False,
        "transports": {"base_url": "http://x"},
        "endpoints": {"detail": {"fields": {}}, "content": {}},
        "decryption": {
            "targets": {
                "image": {
                    "strategy": "aes_cbc",
                    "key": "f5d965df75336270",
                    "iv": "97b60394abc2fbe1",
                    "padding": "pkcs7",
                    "input": "bytes",
                    "output": "bytes",
                }
            }
        },
    }
    src = SourceConfig.from_dict(raw)
    cipher = _aes_cbc_encrypt(_png_bytes(), b"f5d965df75336270", b"97b60394abc2fbe1")
    loader = cl._CoverLoader()
    http = _FakeHttp(cipher)
    loader.use_http(http)
    got = {}
    loader.load("http://img/1.jpg", lambda pm: got.setdefault("pm", pm), source=src, cache=False)
    _wait_until(lambda: "pm" in got)
    assert http.calls
    assert got["pm"] is not None and not got["pm"].isNull()


def test_defaults_to_shared_instance(_qapp):
    """未注入时使用模块级共享 HttpClient 实例。"""
    loader = cl._CoverLoader()
    http = _FakeHttp(_png_bytes())
    got = {}
    with patch.object(cl, "_shared_http_client", return_value=http):
        loader.load("http://img/2.jpg", lambda pm: got.setdefault("pm", pm), cache=False)
        _wait_until(lambda: "pm" in got)
    assert http.calls


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))