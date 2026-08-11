import sys, json, glob

def dget(d, *keys):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur

for f in sorted(glob.glob("sources/*.json")):
    d = json.load(open(f, encoding="utf-8"))
    print("==", f, "type=", d.get("$type"), "enabled=", d.get("$enabled"))
    c = d.get("endpoints", {}).get("content", {})
    for blk, v in c.items():
        if not isinstance(v, dict):
            continue
        lst = v.get("list")
        if isinstance(lst, dict):
            fields = lst.get("fields") or {}
            print("   block", blk, "list.fields:", json.dumps(fields, ensure_ascii=False)[:200])
        else:
            print("   block", blk, "list:", lst)
    cons = d.get("constraints", {}) or {}
    if isinstance(cons, dict):
        for k, v in cons.items():
            if isinstance(v, dict):
                mx = {kk: vv for kk, vv in v.items() if "max" in kk}
                if mx:
                    print("   cons", k, mx)
    det = d.get("endpoints", {}).get("detail", {})
    if isinstance(det, dict):
        print("   detail url_pattern at top:", "url_pattern" in det,
              "| in fields:", "url_pattern" in (det.get("fields") or {}),
              "| fields:", json.dumps(det.get("fields"), ensure_ascii=False)[:160])
    disc = d.get("endpoints", {}).get("discovery", {})
    if isinstance(disc, dict):
        print("   discovery pag:", ("list_paginator" in disc, "paginator" in disc))
    auth = d.get("auth")
    print("   auth:", json.dumps(auth, ensure_ascii=False)[:160] if auth else None)
    search = d.get("endpoints", {}).get("search", {})
    if isinstance(search, dict) and "render_config" in (search.get("item") or {}):
        print("   search.item.render_config present")
