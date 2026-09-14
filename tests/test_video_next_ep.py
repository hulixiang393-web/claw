"""视频播放器「下一集」行为测试。

需求：在播放器中点击「下一集」应正常跳到下一集并播放；已经没有下一集时
（末集 / 无分集的 season 页）点击不做任何反应（不报错、不切换）。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _video_detail(n):
    from framework.content import Chapter, Detail

    return Detail(
        source_id="demo", content_type="video", url="http://example.com/v/1",
        title="作品", cover="", chapters=[
            Chapter(f"第{i + 1}集", f"http://e/{i}", cover="")
            for i in range(n)
        ],
    )


def _view(detail):
    from gui.pages.reader.video_view import VideoView

    view = VideoView(object())
    view._source = None
    view._detail = detail
    view._episodes = detail.chapters
    return view


def test_next_ep_advances_to_next_and_plays(_qapp):
    """有下一集：点下一集 → 加载下一集（进入取流/播放流程）。"""
    view = _view(_video_detail(3))
    view._current_idx = 0
    calls = []
    view._load_episode = lambda idx: calls.append(idx)
    view._on_next_ep()
    assert calls == [1]


def test_next_ep_does_nothing_at_last_episode(_qapp):
    """末集：点下一集无任何反应（不触发加载）。"""
    view = _view(_video_detail(2))
    view._current_idx = 1  # 最后 1 集
    calls = []
    view._load_episode = lambda idx: calls.append(idx)
    view._on_next_ep()
    assert calls == []


def test_next_ep_does_nothing_without_episodes(_qapp):
    """无分集（season 页整季单视频）：点下一集无反应。"""
    view = _view(_video_detail(0))
    view._current_idx = -1
    view._detail_url_for_play = "http://e/whole"
    calls = []
    view._load_episode = lambda idx: calls.append(idx)
    view._on_next_ep()
    assert calls == []


def test_next_ep_works_from_selection_mode(_qapp):
    """选集态（多集源等待用户选集）点下一集也正常跳到下一集。"""
    view = _view(_video_detail(5))
    view._current_idx = 0
    view._selection_mode = True
    calls = []
    view._load_episode = lambda idx: calls.append(idx)
    view._on_next_ep()
    assert calls == [1]
    # 退出选集态/同步 UI 属于 _load_episode 内部（此处已 monkeypatch 短路），不在此断言


def test_prev_ep_is_symmetric(_qapp):
    """上一集对称：首集点上一集无反应，中间集点上一切回上一集。"""
    view = _view(_video_detail(3))
    view._current_idx = 0
    calls = []
    view._load_episode = lambda idx: calls.append(idx)
    view._on_prev_ep()
    assert calls == []

    view._current_idx = 2
    view._on_prev_ep()
    assert calls == [1]