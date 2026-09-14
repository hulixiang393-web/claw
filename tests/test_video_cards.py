"""视频阅读卡片网格测试（分集卡片封面 + 相关推荐卡片渲染/点击）。

旧 test_cover_fallback.py 随侧栏 cover_label 布局移除改迁至此：
- 分集卡片封面：ep.cover → 回退 detail.cover → 无封面纯文字块
- 分集卡片演员：detail.actor（源 detail.fields.actor 可选配置）→ 卡片加高显示；
  未配置 → 演员行隐藏，卡片高度回归 158（不影响回归）
- 网格数量 = 集数，点卡按集序切集（选中高亮跟随）
- 推荐卡片：同源 SearchResult 渲染、剔除自身、截断前 8，
  点击发 recommend_open_requested(source_id, url, content_type)
"""
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtCore import Qt  # noqa: E402


def _make_view(detail, source=None):
    """构造完整 VideoView（离屏；content 占位，不触发播放路径）。"""
    from gui.pages.reader.video_view import VideoView

    view = VideoView(object())
    view._source = source
    view._detail = detail
    return view


def _video_detail(chapter_covers=None, cover=""):
    from framework.content import Chapter, Detail

    chapters = [
        Chapter(f"第{i + 1}集", f"http://e/{i}", cover=c or "")
        for i, c in enumerate(chapter_covers or [])
    ]
    return Detail(
        source_id="demo", content_type="video", url="http://example.com/v/1",
        title="作品", cover=cover, chapters=chapters,
    )


def _no_network():
    """禁用 CoverLoader 真实请求（离屏无事件循环回填）。"""
    from unittest.mock import patch

    from gui.components.cover_loader import CoverLoader

    return patch.object(CoverLoader.instance(), "load")


def _rec_result(title, url, cover=""):
    from types import SimpleNamespace

    return SimpleNamespace(title=title, url=url, cover=cover, source_id="demo")


def test_ep_card_count_equals_episodes(_qapp):
    detail = _video_detail(["http://c/ep1.jpg", "http://c/ep2.jpg", "http://c/ep3.jpg"])
    view = _make_view(detail)
    with _no_network():
        view._populate_ep_cards(detail.chapters)
    assert len(view._ep_cards) == 3
    assert [c.p_data for c in view._ep_cards] == [0, 1, 2]
    assert not view.ep_section_label.isHidden()


def test_ep_card_uses_own_cover(_qapp):
    """分集自带封面 → 卡片用该集缩略图。"""
    detail = _video_detail(["http://c/ep1.jpg", "http://c/ep2.jpg"])
    view = _make_view(detail)
    with _no_network() as load:
        view._populate_ep_cards(detail.chapters)
    assert view._ep_cards[0].cover_url == "http://c/ep1.jpg"
    assert view._ep_cards[1].cover_url == "http://c/ep2.jpg"
    assert not view._ep_cards[0]._cover.isHidden()
    load.assert_called()


def test_ep_card_falls_back_to_detail_cover(_qapp):
    """分集无封面 → 回退作品封面（ep.cover → detail.cover）。"""
    detail = _video_detail(["", ""], cover="http://c/detail.jpg")
    view = _make_view(detail)
    with _no_network():
        view._populate_ep_cards(detail.chapters)
    assert view._ep_cards[0].cover_url == "http://c/detail.jpg"
    assert view._ep_cards[1].cover_url == "http://c/detail.jpg"


def test_ep_card_text_only_when_no_cover(_qapp):
    """全无封面 → 纯文字块（封面区隐藏）。"""
    detail = _video_detail(["", ""])
    view = _make_view(detail)
    with _no_network():
        view._populate_ep_cards(detail.chapters)
    assert view._ep_cards[0].cover_url == ""
    assert view._ep_cards[0]._cover.isHidden()


def test_ep_card_click_selects_episode(_qapp):
    """点卡片 → _select_episode(集序)（p_data 集序 + 当前集选中态）。"""
    detail = _video_detail(["", ""])
    view = _make_view(detail)
    view._episodes = detail.chapters
    view._current_idx = 0
    with _no_network():
        view._populate_ep_cards(detail.chapters)
    calls = []
    view._load_episode = lambda idx: calls.append(idx)
    view._ep_cards[1].clicked.emit(view._ep_cards[1])
    assert calls == [1]
    view._select_episode(0)  # 点当前集 → 不重复触发
    assert calls == [1]


def test_ep_card_actor_shown_when_detail_has_actor(_qapp):
    """作品配置了 actor → 分集卡片显示演员行（卡片加高）。"""
    from framework.content import Chapter, Detail

    detail = Detail(
        source_id="demo", content_type="video", url="http://example.com/v/1",
        title="作品", actor="张三, 李四",
        chapters=[Chapter("第1集", "http://e/1", cover="http://c/ep1.jpg"),
                  Chapter("第2集", "http://e/2")],
    )
    view = _make_view(detail)
    with _no_network():
        view._populate_ep_cards(detail.chapters)
    assert view._ep_cards[0].actor == "张三, 李四"
    assert not view._ep_cards[0]._actor_lbl.isHidden()
    assert view._ep_cards[0]._actor_lbl.text() == "张三, 李四"
    assert view._ep_cards[0].minimumHeight() == 178


def test_ep_card_actor_hidden_when_empty(_qapp):
    """作品未配置 actor → 卡片无演员行（高度回归 158，不影响回归）。"""
    detail = _video_detail(["http://c/ep1.jpg"])
    view = _make_view(detail)
    with _no_network():
        view._populate_ep_cards(detail.chapters)
    assert view._ep_cards[0].actor == ""
    assert view._ep_cards[0]._actor_lbl.isHidden()
    assert view._ep_cards[0].minimumHeight() == 158


def test_ep_card_actor_shows_text_only_when_no_cover(_qapp):
    """无封面 + 有演员 → 纯文字块（封面区隐藏 + 演员行居中跟随）。"""
    from framework.content import Chapter, Detail

    detail = Detail(
        source_id="demo", content_type="video", url="http://example.com/v/1",
        title="作品", actor="张三",
        chapters=[Chapter("第1集", "http://e/1"), Chapter("第2集", "http://e/2")],
    )
    view = _make_view(detail)
    with _no_network():
        view._populate_ep_cards(detail.chapters)
    assert view._ep_cards[0]._cover.isHidden()
    assert not view._ep_cards[0]._actor_lbl.isHidden()
    assert view._ep_cards[0]._actor_lbl.alignment() & Qt.AlignCenter
    assert view._ep_cards[0].minimumHeight() == 178


def test_rec_card_actor_empty_and_hidden(_qapp):
    """推荐卡片（SearchResult 无 actor）→ 默认空演员，不显示演员行。"""
    detail = _video_detail(["http://c/ep1.jpg"])
    view = _make_view(detail, source=SimpleNamespace(source_id="demo"))
    view._episodes = detail.chapters
    with _no_network():
        view._populate_ep_cards(detail.chapters)
        res = [_rec_result("同站作品A", "http://example.com/v/a", "http://c/a.jpg")]
        view._render_rec_cards(res)
    assert view._rec_cards[0].actor == ""
    assert view._rec_cards[0]._actor_lbl.isHidden()
    assert view._rec_cards[0].minimumHeight() == 158


def test_recommend_keyword_priority(_qapp):
    """推荐关键词：优先 tags[0]，回退标题短前缀（前 3 字，防只命中自身）。"""
    from framework.content import Detail
    from gui.pages.reader.video_view import VideoView

    d1 = Detail(source_id="d", content_type="video", url="u",
                title="某作品名称超长用于测试推荐关键词", tags=["#悬疑", "剧情"])
    assert VideoView._recommend_keyword_of(d1) == "#悬疑"
    d2 = Detail(source_id="d", content_type="video", url="u",
                title="某作品名称超长用于测试推荐关键词")
    assert VideoView._recommend_keyword_of(d2) == "某作品"
    assert d2.title[:3] != d2.title, "短前缀不应等于完整标题（避免命中自身后剔除为空）"


def test_recommend_keyword_falls_back_when_tags_empty(_qapp):
    """tags 含空白项/空标签 → 视同无 tags，回退标题短前缀。"""
    from framework.content import Detail
    from gui.pages.reader.video_view import VideoView

    d = Detail(source_id="d", content_type="video", url="u",
               title="一拳超人第三季", tags=["", "  ", None])
    assert VideoView._recommend_keyword_of(d) == "一拳超"


def test_rec_render_and_click(_qapp):
    """推荐卡渲染 + 点击 → recommend_open_requested(source_id, url, video)。"""
    from types import SimpleNamespace

    detail = _video_detail(["", ""])
    view = _make_view(detail, source=SimpleNamespace(source_id="demo"))
    view._episodes = detail.chapters
    with _no_network():
        view._populate_ep_cards(detail.chapters)
        res = [
            _rec_result("同站作品A", "http://example.com/v/a", "http://c/a.jpg"),
            _rec_result("同站作品B", "http://example.com/v/b"),
        ]
        view._render_rec_cards(res)
    assert len(view._rec_cards) == 2
    assert not view.rec_section_label.isHidden()
    assert view._rec_cards[0].cover_url == "http://c/a.jpg"
    opened = []
    view.recommend_open_requested.connect(lambda p: opened.append(p))
    view._rec_cards[0].clicked.emit(view._rec_cards[0])
    assert opened and opened[0] == ("demo", "http://example.com/v/a", "video")


def test_rec_filters_self_and_caps(_qapp):
    """推荐：剔除自身 url，截断到前 8 条（_on_recommend_done 收口）。"""
    detail = _video_detail(["", "http://c/ep2.jpg"])
    view = _make_view(detail)
    kw = "悬疑"
    view._recommend_keyword = kw
    res = [_rec_result(f"作品{i}", f"http://example.com/v/{i}") for i in range(10)]
    res.insert(3, _rec_result("自己", detail.url))
    with _no_network():
        view._on_recommend_done("demo", kw, res, None)
    assert len(view._rec_cards) == 8
    assert all(c.p_data.url != detail.url for c in view._rec_cards)
    assert not view.rec_section_label.isHidden()


def test_rec_dedupes_duplicate_urls(_qapp):
    """推荐：同 URL 重复命中（短前缀搜索常见）→ 去重后渲染,不重复占格。"""
    detail = _video_detail(["", "http://c/ep2.jpg"])
    view = _make_view(detail)
    kw = "一拳"
    view._recommend_keyword = kw
    res = [
        _rec_result("同一作A", "http://example.com/v/a"),
        _rec_result("同一作B", "http://example.com/v/a"),  # 重复 URL
        _rec_result("另一作", "http://example.com/v/b"),
    ]
    with _no_network():
        view._on_recommend_done("demo", kw, res, None)
    assert len(view._rec_cards) == 2
    assert [c.p_data.url for c in view._rec_cards] == [
        "http://example.com/v/a", "http://example.com/v/b"]
    assert not view.rec_section_label.isHidden()


def test_rec_empty_hides_section(_qapp):
    """搜索为空 → 推荐区隐藏，不影响分集网格。"""
    detail = _video_detail(["", "http://c/ep2.jpg"])
    view = _make_view(detail)
    with _no_network():
        view._populate_ep_cards(detail.chapters)
        view._render_rec_cards([])
    assert view.rec_section_label.isHidden()
    assert view._rec_cards == []
    assert len(view._ep_cards) == 2