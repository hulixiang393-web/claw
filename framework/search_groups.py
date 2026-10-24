from __future__ import annotations

import re
import threading
import unicodedata
from collections import OrderedDict
from urllib.parse import parse_qs, urlsplit

_LOCK = threading.Lock()
_GROUPS: "OrderedDict[tuple[str, str], list[dict]]" = OrderedDict()
_LIMIT = 128


def normalize_title(title: str) -> str:
    text = unicodedata.normalize("NFKC", str(title or ""))
    text = re.sub(r"[（(].*?[)）]", "", text)
    text = re.sub(r"[\s:：,，.。、!！?？\-—_/\\|·'\"“”]+", "", text)
    text = re.sub(
        r"(连载中|更新中|已完结|完结|全本|全文|正版|无删减|TXT|txt)$", "", text
    )
    return text.lower().strip()


def remember_group(source_id: str, title: str, items: list[dict]) -> None:
    key = (str(source_id or ""), normalize_title(title))
    if not key[0] or not key[1] or len(items) < 2:
        return
    with _LOCK:
        _GROUPS[key] = [dict(item) for item in items]
        _GROUPS.move_to_end(key)
        while len(_GROUPS) > _LIMIT:
            _GROUPS.popitem(last=False)


def lookup_group(source_id: str, title: str = "", url: str = "") -> list[dict]:
    keys = [normalize_title(title)]
    if url:
        query = parse_qs(urlsplit(url).query)
        keys.append(normalize_title((query.get("title") or [""])[0]))
    with _LOCK:
        for text in keys:
            key = (str(source_id or ""), text)
            if text and key in _GROUPS:
                return [dict(item) for item in _GROUPS[key]]
    return []


def clear_groups() -> None:
    with _LOCK:
        _GROUPS.clear()
