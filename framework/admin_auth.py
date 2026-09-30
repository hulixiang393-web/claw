from __future__ import annotations

import json
from pathlib import Path

from .folder_lock import hash_password, new_salt, verify_password


class AdminAuth:
    def __init__(self, path: str | Path):
        self._path = Path(path)

    def is_configured(self) -> bool:
        return self._path.is_file()

    def configure(self, password: str) -> bool:
        if self.is_configured() or not password:
            return False
        salt = new_salt()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"pw": hash_password(password, salt), "salt": salt}, indent=2),
            encoding="utf-8",
        )
        return True

    def verify(self, password: str) -> bool:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return verify_password(password, data.get("pw"), data.get("salt"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def change(self, old_password: str, new_password: str) -> bool:
        if not new_password or not self.verify(old_password):
            return False
        salt = new_salt()
        self._path.write_text(
            json.dumps({"pw": hash_password(new_password, salt), "salt": salt}, indent=2),
            encoding="utf-8",
        )
        return True
