import json
from PySide6.QtWidgets import QApplication
from framework.config import SourceConfig
from gui.components import source_presets
from gui.components.source_editor import SourceEditor

app = QApplication([])

raw0 = source_presets.default_source_template("video")
raw0["$id"] = "demo.video"
raw0["$name"] = "演示视频源"
raw0["$enabled"] = False
dlg = SourceEditor(SourceConfig.from_dict(raw0), None)
dlg._load_into_form()
out = dlg._build_dict()
base = dlg._raw
live_lost = []

def walk(a, b, path):
    for k, v in b.items():
        p = path + "/" + k
        if isinstance(v, dict):
            walk(a.get(k, {}) if isinstance(a, dict) else {}, v, p)
        else:
            if not (isinstance(a, dict) and a.get(k) == v):
                live_lost.append((p, type(v).__name__, v))

for top in ("endpoints", "transports", "constraints", "diagnostics", "media"):
    walk(out.get(top, {}), base.get(top, {}), top)
print("template_roundtrip_lost", len(live_lost))
for p, t, v in live_lost:
    print("  LOST", p, "=", repr(v)[:60])
print("OUT_TRANSPORTS", json.dumps(out.get("transports", {}), ensure_ascii=False))

dlg._combo_select(dlg._dec_preset, "AES-CBC · 图片分片解密")
dlg._action_decrypt_preset(False)
print("img_strategy", dlg._dec_img_strategy.currentText(),
      "| key", dlg._dec_img_key.text(),
      "| pad", dlg._dec_img_padding.currentText())
print("ch_strategy", dlg._dec_ch_strategy.currentText(),
      "| ch_map", dlg._dec_ch_map.text())

p = [x for x in source_presets.RENDER_PRESETS if x["name"] == "视频·播放配置页（m3u8）"][0]
SourceEditor._load_cfg_widgets(dlg._ep_rc_specs, p.get("config") or {})
vals = []
for key, w, kind in dlg._ep_rc_specs:
    if kind == "combo":
        vals.append((key, w.currentText()))
    elif kind == "text":
        vals.append((key, w.text()))
print("ep_preset_vals", vals)
print("SMOKE_OK")