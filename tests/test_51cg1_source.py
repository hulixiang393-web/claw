import json
from pathlib import Path

from framework.parser import Parser


def test_51cg1_inline_dplayers_become_ordered_episodes():
    source = json.loads(
        (Path(__file__).resolve().parent.parent / "sources" / "51cg1.json")
        .read_text(encoding="utf-8")
    )
    episode = source["endpoints"]["content"]["episode"]
    html = (
        '<div class="dplayer" data-video_title="大瓜 001" '
        'data-config=\'{"video":{"url":"https:\\/\\/cdn.example/a.m3u8"}}\'></div>'
        '<div class="dplayer" data-video_title="大瓜 002" '
        'data-config=\'{"video":{"url":"https:\\/\\/cdn.example/b.m3u8"}}\'></div>'
    )

    items = Parser().parse_items(
        Parser().parse(html),
        episode["list"]["root_selector"],
        episode["list"]["fields"],
        "https://51cg1.com",
    )
    assert [item["title"] for item in items] == ["大瓜 001", "大瓜 002"]
    assert [item["url"] for item in items] == [
        "https:\\/\\/cdn.example/a.m3u8",
        "https:\\/\\/cdn.example/b.m3u8",
    ]


if __name__ == "__main__":
    test_51cg1_inline_dplayers_become_ordered_episodes()
    print("51cg1 source self-check: ok")
