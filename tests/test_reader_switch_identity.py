from types import SimpleNamespace

import pytest

from framework.content import Chapter, Detail
from framework.library_store import LibraryStore
from framework.reading_progress import ReadingProgress
from gui.pages.reader_page import ReaderPage, _SwitchSourceTask


@pytest.fixture
def reader_case(_qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QThreadPool

    tasks = []
    monkeypatch.setattr(QThreadPool, "globalInstance", lambda: SimpleNamespace(start=tasks.append))
    source = SimpleNamespace(source_id="age", source_name="AGE", raw={})
    content = SimpleNamespace(switch_source=lambda *args: (content.result, []))
    progress = ReadingProgress(tmp_path / "progress.json")
    reader = ReaderPage(SimpleNamespace(get=lambda sid: source), content, reading_progress=progress)
    yield reader, content, progress, tasks
    reader.deleteLater()
    _qapp.processEvents()


def finish(task):
    import threading
    from PySide6.QtWidgets import QApplication

    thread = threading.Thread(target=task.run)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    QApplication.instance().processEvents()


def test_switch_source_task_uses_variant_detail_url(_qapp):
    calls = []
    moved = Detail(source_id="ikanpp", url="target", title="t", content_type="video")
    content = SimpleNamespace(
        switch_source_url=lambda source, url: calls.append((source, url)) or (moved, []),
        switch_source=lambda *args: (_ for _ in ()).throw(AssertionError("sid path used")),
    )
    source = SimpleNamespace(source_id="ikanpp")
    _SwitchSourceTask(
        content, source, "origin", "b", ("ikanpp", "book"), 1, "target"
    ).run()
    assert calls == [(source, "target")]


def detail(name, route=""):
    return Detail(source_id="age", url="https://example.test/" + name + route,
                  title=name, content_type="video", chapters=[Chapter(name + "集", name + route + "/ep")])


def opened(reader, value):
    reader.open("age", value.url, "video")
    reader._on_detail(value, None, "video", "", "age")


def test_private_switch_late_after_public_detail_never_leaks(reader_case, tmp_path):
    reader, content, progress, tasks = reader_case
    private, public = detail("private"), detail("public")
    opened(reader, private)
    reader._on_source_changed((private, "2"))
    old = tasks[-1]
    opened(reader, public)
    content.result = detail("private", "?sid=2")
    finish(old)
    assert reader.video_view._detail is public
    assert reader.video_view.library_url == public.url
    text = reader.video_view.play_label.text()
    content.switch_source = lambda *args: (_ for _ in ()).throw(RuntimeError("private failure"))
    finish(old)
    assert reader.video_view.play_label.text() == text
    reader.video_view._progress_options()["on_handoff"]()
    record = progress.resume(public.url)
    assert record["title"] == "public"
    assert record["chapter_url"] == public.chapters[0].url
    store = LibraryStore(tmp_path / "library.json")
    store.create_folder("private", locked=True)
    store.add("age", private.url, private.title, "video", folder="private")
    assert all(rec["title"] != "private" for rec in progress.recent_videos(store))


def test_same_work_out_of_order_success_and_error(reader_case):
    reader, content, progress, tasks = reader_case
    original = detail("private")
    opened(reader, original)
    reader._on_source_changed((original, "2"))
    older = tasks[-1]
    reader._on_source_changed((original, "3"))
    newer = tasks[-1]
    content.result = detail("private", "?sid=3")
    finish(newer)
    selected = reader.video_view._detail
    text = reader.video_view.play_label.text()
    content.result = detail("private", "?sid=2")
    finish(older)
    assert reader.video_view._detail is selected
    content.switch_source = lambda *args: (_ for _ in ()).throw(RuntimeError("late failure"))
    finish(older)
    assert reader.video_view.play_label.text() == text
    assert reader.video_view.library_url == original.url
    assert selected.url.endswith("?sid=3")
    reader.video_view._progress_options()["on_handoff"]()
    assert progress.resume(original.url)["detail_url"] == selected.url


def test_older_failure_does_not_cancel_new_pending_switch(reader_case):
    reader, content, progress, tasks = reader_case
    original = detail("private")
    opened(reader, original)
    reader._on_source_changed((original, "2"))
    old = tasks[-1]
    reader._on_source_changed((original, "3"))
    current = tasks[-1]
    reader.video_view.play_label.setText("new pending")
    content.switch_source = lambda *args: (_ for _ in ()).throw(RuntimeError("old failure"))
    finish(old)
    assert reader._switch_task is current
    assert reader.video_view.play_label.text() == "new pending"
    content.switch_source = lambda *args: (detail("private", "?sid=3"), [])
    finish(current)
    assert reader.video_view._detail.url.endswith("?sid=3")


@pytest.mark.parametrize("invalidate", ["close", "shutdown_video", "reopen", "open_epub"])
def test_switch_invalidated_by_lifecycle(reader_case, invalidate, monkeypatch):
    reader, content, progress, tasks = reader_case
    original = detail("private")
    opened(reader, original)
    reader._on_source_changed((original, "2"))
    old = tasks[-1]
    if invalidate == "reopen":
        opened(reader, original)
    elif invalidate == "open_epub":
        monkeypatch.setattr(reader.epub_view, "open", lambda *args, **kwargs: None)
        reader.open_epub("D:/example.epub")
    else:
        getattr(reader, invalidate)()
    before = reader.video_view._detail
    content.result = detail("private", "?sid=2")
    finish(old)
    assert reader.video_view._detail is before
    text = reader.video_view.play_label.text()
    content.switch_source = lambda *args: (_ for _ in ()).throw(RuntimeError("late failure"))
    finish(old)
    assert reader.video_view.play_label.text() == text
