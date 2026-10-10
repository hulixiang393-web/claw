from __future__ import annotations

import json
from pathlib import Path

from gui.components import source_editor as se_mod


def test_fill_novel_example_uses_existing_local_source_and_valid_json(_qapp, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    source_path = root / "sources" / "zongheng.json"
    loaded_paths = []
    load_source = se_mod.load_source

    def load_local_source(path):
        loaded_paths.append(Path(path))
        return load_source(path)

    monkeypatch.setattr(se_mod, "load_source", load_local_source)
    editor = se_mod.SourceEditor(sources_dir=root / "sources")
    editor._fill_example()

    assert se_mod.SourceEditor._EXAMPLE_SOURCE["novel"] == "zongheng"
    assert loaded_paths == [source_path]
    assert editor._f_base.text() == "https://www.zongheng.com"

    serialized = json.dumps(editor._build_dict(), ensure_ascii=False)
    assert isinstance(json.loads(serialized), dict)
