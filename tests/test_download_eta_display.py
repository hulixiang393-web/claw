from gui.pages.download_page import _format_remaining_seconds


def test_eta_display_is_stable_in_five_second_buckets():
    assert _format_remaining_seconds(18.9) == 20
    assert _format_remaining_seconds(17.1) == 15
    assert _format_remaining_seconds(2.0) == 0
