# -*- coding: utf-8 -*-
"""CoverLoader 解密封面通道测试（test_cover_decrypt.py）。

覆盖阅读界面对封面加载的可选 AES 解密：源配置声明 decryption.image 时，
封面字节直接解码失败（私图床密文）→ 按配置 AES 解密后再交 QPixmap；
无解密配置 / 解密失败 → 原样透传（不影响普通源封面与发现页既有逻辑）。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.config import SourceConfig
from gui.components.cover_loader import _decode_pixmap

KEY = b"f5d965df75336270"
IV = b"97b60394abc2fbe1"


def _source(**target_cfg) -> SourceConfig:
    raw = {
        "$schema_version": 2,
        "$id": "t",
        "$type": "video",
        "$name": "t",
        "$enabled": False,
        "transports": {"base_url": "http://x"},
        "endpoints": {"detail": {"fields": {}}, "content": {}},
        "decryption": {"targets": target_cfg},
    }
    return SourceConfig.from_dict(raw)


def _png_bytes() -> bytes:
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
    """AES-CBC PKCS7 加密（与 decryption.image 配置的密码学基元对齐）。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    pad = 16 - len(data) % 16
    padded = data + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(padded) + enc.finalize()


def test_plain_image_without_source(_qapp):
    """普通图直接解码，不解密也不改动存储字节。"""
    plain = _png_bytes()
    pm, store = _decode_pixmap(plain, None)
    assert pm is not None and not pm.isNull()
    assert store == plain


def test_encrypted_cover_decrypted_with_source(_qapp):
    """加密封面字节 + 声明 decryption.image 的源 → 解密后可解码。"""
    src = _source(image={
        "strategy": "aes_cbc", "key": KEY.decode(), "iv": IV.decode(),
        "padding": "pkcs7", "input": "bytes", "output": "bytes",
    })
    plain = _png_bytes()
    cipher = _aes_cbc_encrypt(plain, KEY, IV)
    pm, store = _decode_pixmap(cipher, src)
    assert pm is not None and not pm.isNull()
    assert store == plain  # 书架持久化存解密后字节，重启命中免二次解密


def test_encrypted_cover_without_source_returns_none(_qapp):
    """加密字节无解密配置 → 不解码，原样透传（普通源回归）。"""
    cipher = _aes_cbc_encrypt(_png_bytes(), KEY, IV)
    pm, store = _decode_pixmap(cipher, None)
    assert pm is None
    assert store == cipher


def test_encrypted_cover_wrong_key_returns_none(_qapp):
    """密钥错误 → 解密失败返回 None（保留原 URL 兜底，不抛异常）。"""
    src = _source(image={
        "strategy": "aes_cbc", "key": "00000000", "iv": IV.decode(),
        "padding": "pkcs7", "input": "bytes", "output": "bytes",
    })
    cipher = _aes_cbc_encrypt(_png_bytes(), KEY, IV)
    pm, _ = _decode_pixmap(cipher, src)
    assert pm is None