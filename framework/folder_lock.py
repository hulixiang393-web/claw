"""收藏夹密码派生（UI 门禁用）。

收藏数据仍明文存 data/library.json —— 密码只控制「能否看到卡片」，
不用于内容加密（用户已明确选择 UI 门禁方案）。
"""

import base64
import hashlib
import hmac
import os
import secrets

_ITERATIONS = 200_000
_SALT_BYTES = 16
_DERIVED_KEY_BYTES = 32


def new_salt() -> str:
    return os.urandom(_SALT_BYTES).hex()


def hash_password(password: str, salt_hex: str) -> str:
    if not salt_hex:
        return ""
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        (password or "").encode("utf-8"),
        bytes.fromhex(salt_hex),
        _ITERATIONS,
        dklen=_DERIVED_KEY_BYTES,
    )
    return base64.b64encode(derived).decode("ascii")


def verify_password(password: str, pw_b64, salt_hex) -> bool:
    if not pw_b64 or not salt_hex:
        return False
    try:
        candidate = hash_password(password, salt_hex)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, str(pw_b64))


def new_recovery_code() -> str:
    return "-".join(secrets.token_hex(4).upper() for _ in range(4))


def hash_recovery_code(code: str, salt_hex: str) -> str:
    return hash_password(code, salt_hex)


def verify_recovery_code(code: str, code_b64, salt_hex) -> bool:
    return verify_password(code, code_b64, salt_hex)
