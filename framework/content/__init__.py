"""内容服务包（B4 拆分）：组合 Content 并 re-export 公共名。

对外保持原 `framework.content` 的公共接口不变：Content / Chapter /
BookStackChapterResult / Detail / chapter_label / _strip_html_tags，
以及原模块级命名空间（time/_re/json/... 供测试 monkeypatch 兼容）。
"""

from . import shared as _shared

# 复刻旧 framework.content 模块级命名空间（含下划线名），
# 使 `framework.content.time` / monkeypatch("framework.content.X") 等继续可用。
globals().update(
    {k: v for k, v in vars(_shared).items() if not k.startswith("__")}
)

from .base import ContentBase
from .detail import DetailMixin
from .chapter import ChapterMixin
from .comic import ComicMixin
from .video import VideoMixin


class Content(DetailMixin, ChapterMixin, ComicMixin, VideoMixin, ContentBase):
    """内容服务门面：组合各职责 mixin（B4 拆分，行为不变）。"""

    pass
