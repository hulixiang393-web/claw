import glob
import json
import os
from PySide6.QtWidgets import QApplication
from framework.config import SourceConfig
from gui.components.source_editor import SourceEditor

app = QApplication([])

files = sorted(glob.glob("sources/*.json"))
results = []

def is_empty(v):
    return v == "" or v is None or (isinstance(v, (list, dict)) and not v)

def check(a, b, path, lost):
    for k, v in b.items():
        p = path + "/" + k
        if isinstance(v, dict):
            check(a.get(k, {}) if isinstance(a, dict) else {}, v, p, lost)
            continue
        if is_empty(v):
            continue
        if not (isinstance(a, dict) and a.get(k) == v):
            lost.append((p, repr(v)[:60]))

for f in files:
    raw0 = json.load(open(f, encoding="utf-8"))
    dlg = SourceEditor(SourceConfig.from_dict(raw0), None)
    dlg._load_into_form()
    out = dlg._build_dict()
    lost = []
    check(out, raw0, "", lost)
    if lost:
        results.append((os.path.basename(f), "LOST", lost[:8]))
    else:
        results.append((os.path.basename(f), "OK", []))

bad = 0
for name, status, lost in results:
    if status == "OK":
        print(f"{name}: OK")
    else:
        bad += 1
        print(f"{name}: LOST")
        for p, v in lost:
            print("   ", p, "=", v)
print("ALL_OK" if bad == 0 else f"FAILED({bad})")