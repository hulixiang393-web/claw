"""一次性排查：从缓存库里捞出最近播放的 HLS 上游地址（含 host）。

目的：拿到 agedm 那条 3000k 流的真实 CDN 分片地址，做单/多连接吞吐实测。
看完即删。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path(r"D:\code\claw\data\shelf.sqlite3")


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    tables = [r[0] for r in conn.execute(
        "select name from sqlite_master where type='table'")]
    print("tables:", tables)
    for t in tables:
        cols = [r[1] for r in conn.execute(f"pragma table_info({t})")]
        urlish = [c for c in cols if "url" in c.lower() or "key" in c.lower()]
        if not urlish:
            continue
        print(f"\n== {t} cols={cols}")
        try:
            rows = conn.execute(f"select {', '.join(urlish)} from {t} "
                                f"order by rowid desc limit 5").fetchall()
        except sqlite3.Error as exc:
            print("   ", exc)
            continue
        for r in rows:
            print("   ", [str(v)[:120] for v in r])
    return 0


if __name__ == "__main__":
    sys.exit(main())
