from framework.settings_manager import SettingsManager
from gui.pages.reader.novel_view import NovelView


def test_reader_layout_settings_load_and_persist(_qapp, tmp_path):
    settings = SettingsManager(tmp_path / "settings.json")
    settings.set("reader", "font_size", 20)
    settings.set("reader", "font_family", "song")
    settings.set("reader", "line_spacing", 2.1)
    settings.set("reader", "content_width", 820)
    settings.set("reader", "paragraph_indent", False)
    settings.set("reader", "show_images", False)
    settings.save()

    view = NovelView(object(), settings=settings)
    assert view._reader_font_size == 20
    assert view._reader_font_family == "song"
    assert view._line_spacing == 2.1
    assert view._content_width == 820
    assert view._paragraph_indent is False
    assert view._show_images is False
    assert view._font_actions[20].isChecked()
    assert view._font_family_actions["song"].isChecked()
    assert view._spacing_actions[2.1].isChecked()
    assert view._width_actions[820].isChecked()

    view._set_reader_font_size(24)
    view._set_reader_font_family("kai")
    view._set_line_spacing(1.5)
    view._set_content_width(1100)
    view._set_paragraph_indent(True)
    view.set_show_images(True)

    saved = SettingsManager(tmp_path / "settings.json")
    assert saved.get("reader", "font_size") == 24
    assert saved.get("reader", "font_family") == "kai"
    assert saved.get("reader", "line_spacing") == 1.5
    assert saved.get("reader", "content_width") == 1100
    assert saved.get("reader", "paragraph_indent") is True
    assert saved.get("reader", "show_images") is True
    view.deleteLater()


def test_reader_mode_menu_and_toolbar_stay_in_sync(_qapp):
    view = NovelView(object())
    view.bg_btn.ensurePolished()
    assert view.bg_btn.sizeHint().width() >= view.bg_btn.fontMetrics().horizontalAdvance("背景") + 20
    assert view._mode == "scroll"
    assert view._mode_actions["scroll"].isChecked()
    assert not view._mode_actions["pager"].isChecked()

    view._set_mode("pager")
    assert view._mode == "pager"
    assert view.body_stack.currentWidget() is view.pager_widget
    assert view._mode_actions["pager"].isChecked()
    assert view.mode_btn.text() == "滚动模式"

    view._toggle_mode()
    assert view._mode == "scroll"
    assert view.body_stack.currentWidget() is view.scroll
    assert view._mode_actions["scroll"].isChecked()
    assert view.mode_btn.text() == "翻页模式"
    view.deleteLater()


def test_reader_language_button_toggles_and_persists(_qapp, tmp_path):
    settings = SettingsManager(tmp_path / "settings.json")
    view = NovelView(object(), settings=settings)

    assert view._reader_language == "zh-cn"
    assert view.language_btn.text() == "简体"

    view._set_reader_language("zh-tw")

    assert view._reader_language == "zh-tw"
    assert view.language_btn.text() == "繁体"
    saved = SettingsManager(tmp_path / "settings.json")
    assert saved.get("reader", "language") == "zh-tw"
    view.deleteLater()


def test_reader_language_conversion_does_not_change_cached_text(_qapp):
    view = NovelView(object())
    source = "繁體小說內容：這是一段測試文字。"

    view._reader_language = "zh-cn"
    simplified = view._reader_convert_text(source)
    view._reader_language = "zh-tw"

    assert simplified == "繁体小说内容：这是一段测试文字。"
    assert view._reader_convert_text(simplified) == source
    view.deleteLater()
