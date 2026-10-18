"""原子写盘工具（atomic_io.py）。

统一的「临时文件 + os.replace」落盘实现，供各持久化模块复用：

- 先在同目录创建临时文件，写成功后 os.replace 原子替换目标文件；
- 任一步失败都会清理临时文件后原样抛出，保证目标文件不被半写覆盖；
- 可选 fsync，确保进程崩溃前数据已刷盘。

被 settings_manager / library_store / search_history / reading_progress /
cache_service 复用，保证各模块落盘语义一致。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, IO, Optional

__all__ = [
    "atomic_write_stream",
    "atomic_write_bytes",
    "atomic_write_text",
    "atomic_write_json",
    "read_text_lenient",
]


def atomic_write_stream(
    path: str | Path,
    write: Callable[[IO[bytes]], Any],
    *,
    fsync: bool = False,
) -> None:
    """以回调方式原子写盘：write(handle) 向临时文件句柄写内容。

    适用于无法一次性拿到完整内容（如 gzip 流式序列化）的场景：
    写成功并（可选）fsync 后 os.replace 到 path；失败则删除临时文件并
    原样抛出异常，目标文件保持不变。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            write(handle)
            if fsync:
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_bytes(
    path: str | Path, data: bytes, *, fsync: bool = False
) -> None:
    """原子写入字节串。"""
    atomic_write_stream(path, lambda handle: handle.write(data), fsync=fsync)


def atomic_write_text(
    path: str | Path,
    text: str,
    encoding: str = "utf-8",
    *,
    fsync: bool = False,
) -> None:
    """原子写入文本（按 encoding 编码为字节后落盘）。"""
    atomic_write_bytes(path, text.encode(encoding), fsync=fsync)


def atomic_write_json(
    path: str | Path,
    data: Any,
    *,
    ensure_ascii: bool = False,
    indent: Optional[int] = 2,
    encoding: str = "utf-8",
    fsync: bool = False,
) -> None:
    """原子写入 JSON（序列化失败先抛 TypeError/ValueError，不产生临时文件）。"""
    text = json.dumps(data, ensure_ascii=ensure_ascii, indent=indent)
    atomic_write_text(path, text, encoding=encoding, fsync=fsync)


def read_text_lenient(
    path: str | Path,
    default: Optional[str] = None,
    encoding: str = "utf-8",
    errors: Optional[str] = None,
) -> Optional[str]:
    """宽容读文本：文件缺失、权限、编码异常时返回 default，不抛出。"""
    try:
        return Path(path).read_text(encoding=encoding, errors=errors)
    except (OSError, UnicodeDecodeError, ValueError):
        return default
