"""复用组件（gui/components）。

首页用到的可复用控件。其他界面后续复用。
"""

from .mascot import MascotWidget
from .stats_row import StatsRow
from .recent_searches import RecentSearches
from .mini_progress import MiniProgress
from .empty_state import EmptyState
from .work_card import WorkCard
from .detail_drawer import DetailDrawer
from .source_editor import SourceEditor

__all__ = [
    "MascotWidget",
    "StatsRow",
    "RecentSearches",
    "MiniProgress",
    "EmptyState",
    "WorkCard",
    "DetailDrawer",
    "SourceEditor",
]
