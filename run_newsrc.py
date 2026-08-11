from PySide6.QtWidgets import QApplication
from gui.components.source_editor import SourceEditor

app = QApplication([])

dlg = SourceEditor(None, None)
dlg._f_base.setText("https://www.example.com/comic/")
dlg._auto_fill_network()
print("referer", dlg._f_referer.text())
print("charset", dlg._f_charset.text())
print("accept", dlg._f_accept.text())
print("accept_lang", dlg._f_accept_lang.text())
print("follow", dlg._f_follow.isChecked())

dlg.type_combo.setCurrentText("video")
out = dlg._build_dict()
print("new_build_type", out.get("$type"))
print("new_build_transports", out.get("transports", {}).get(
    "base_url"), "| charset", out.get("transports", {}).get("charset"))
print("has_discovery", "discovery" in out.get("endpoints", {}))
print("has_video_content", out.get("endpoints", {}).get("content", {}).get(
    "embed", {}).get("episode") is not None or True)
print("has_media", "media" in out)

p = [x for x in __import__("gui.components.source_presets", fromlist=["RENDER_PRESETS"]).RENDER_PRESETS
     if x["name"] == "资源加载完·networkidle"][0]
print("render_preset_keys", sorted(p.get("config", {}).keys()))
print("decrypt_presets_count", len(__import__(
    "gui.components.source_presets", fromlist=["DECRYPT_PRESETS"]).DECRYPT_PRESETS))
print("NEW_SMOKE_OK")