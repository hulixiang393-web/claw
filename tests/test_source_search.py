from pathlib import Path
from urllib.parse import urlsplit

from framework.config import load_source
from framework.parser import Parser
from framework.search import Search


ROOT = Path(__file__).resolve().parents[1]


class FakeHttp:
    def __init__(self, token_html, result_html):
        self.token_html = token_html
        self.result_html = result_html
        self.calls = []
        self.defaults = type(
            "Defaults",
            (),
            {"timeout": 10, "retries": 0, "interval_ms": 0},
        )()

    def get_text(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.token_html

    def post_form(self, url, form_data, **kwargs):
        self.calls.append(("POST", url, form_data, kwargs))
        return self.result_html


def test_bookstack_search_config_supports_pagination_template():
    from framework.config import load_source

    source = load_source(ROOT / "sources" / "bookstack.json")
    paginator = source.get_search_config()["paginator"]
    assert paginator["url_template"] == "/search/result?wd={keyword}&tab=book&page={page}"
    assert source.raw["constraints"]["search"]["max_pages"] == 20


def test_bookstack_search_matches_nested_result_items():
    from framework.config import load_source

    source = load_source(ROOT / "sources" / "bookstack.json")
    html = """
    <div class="result-list"><div class="layout"><ul><li class="clearfix">
      <a href="/books/python"><h4>Python 指南</h4></a>
      <img src="/cover/python.png"><span class="author">作者</span>
    </li></ul></div></div>
    """
    class BookHttp(FakeHttp):
        def __init__(self):
            super().__init__("", html)

        def get_text(self, url, **kwargs):
            self.calls.append(("GET", url, kwargs))
            return self.result_html

    results = Search(BookHttp(), Parser()).search_one(source, "python")

    assert [(result.title, result.url) for result in results] == [
        ("Python 指南", "https://www.bookstack.cn/books/python")
    ]
