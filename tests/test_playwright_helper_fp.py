"""playwright_helper 环境补丁（反指纹）与 CF cookie 注入测试（全 mock，不联网）。"""
import asyncio

import pytest

import framework.playwright_helper as ph


class FakePage:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeContext:
    """记录 add_init_script / add_cookies 调用的假 context（sync 版）。"""

    def __init__(self):
        self.init_scripts = []
        self.cookies = []
        self.closed = False

    def add_init_script(self, script):
        self.init_scripts.append(script)

    def add_cookies(self, cookies):
        self.cookies.extend(cookies)

    def new_page(self):
        return FakePage()

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, ctx=None):
        self._ctx = ctx or FakeContext()
        self.opts = None
        self.contexts = []

    def new_context(self, **kwargs):
        self.opts = kwargs
        self.contexts.append(self._ctx)
        return self._ctx


class FakeAsyncContext(FakeContext):
    """async 版假 context：方法可 await。"""

    async def add_init_script(self, script):
        self.init_scripts.append(script)

    async def add_cookies(self, cookies):
        self.cookies.extend(cookies)

    async def new_page(self):
        return FakePage()


class FakeAsyncBrowser:
    def __init__(self, ctx=None):
        self._ctx = ctx or FakeAsyncContext()
        self.opts = None

    async def new_context(self, **kwargs):
        self.opts = kwargs
        return self._ctx


class TestAntifpInitScript:
    def test_contains_key_anti_fingerprint_fragments(self):
        for frag in (
            "navigator.webdriver", "plugins", "hardwareConcurrency",
            "languages", "deviceMemory", "platform", "vendor",
            "chrome", "loadTimes", "runtime",
        ):
            assert frag in ph._ANTIFP_INIT_SCRIPT, frag


class TestNewBrowserContext:
    def test_sync_applies_init_script_and_cookies(self):
        browser = FakeBrowser()
        cookies = [{"name": "cf_clearance", "value": "abc",
                    "domain": "5238.me", "path": "/"}]
        ctx = ph._new_browser_context(
            browser,
            viewport={"width": 1366, "height": 768},
            ua="UA-TEST",
            cookies=cookies,
        )
        assert browser.opts["user_agent"] == "UA-TEST"
        assert browser.opts["viewport"] == {"width": 1366, "height": 768}
        assert ctx.init_scripts == [ph._ANTIFP_INIT_SCRIPT]
        assert "navigator.webdriver" in ctx.init_scripts[0]
        assert ctx.cookies == cookies

    def test_sync_default_ua_and_no_cookies(self):
        browser = FakeBrowser()
        ctx = ph._new_browser_context(browser, viewport={"width": 1366, "height": 768})
        assert browser.opts["user_agent"] == ph._default_user_agent()
        assert ctx.cookies == []

    def test_async_applies_init_script_and_cookies(self):
        browser = FakeAsyncBrowser()
        cookies = [{"name": "__cf_bm", "value": "bm",
                    "domain": "5238.me", "path": "/"}]
        ctx = asyncio.run(ph._new_browser_context_async(
            browser,
            viewport={"width": 900, "height": 2000},
            cookies=cookies,
        ))
        assert browser.opts["viewport"] == {"width": 900, "height": 2000}
        assert browser.opts["user_agent"] == ph._default_user_agent()
        assert ctx.init_scripts == [ph._ANTIFP_INIT_SCRIPT]
        assert "hardwareConcurrency" in ctx.init_scripts[0]
        assert ctx.cookies == cookies

    def test_sync_page_applies_patch_injects_all_cookies(self, monkeypatch):
        ctx = FakeContext()
        browser = FakeBrowser(ctx)
        cookies = [{"name": "cf_clearance", "value": "abc",
                    "domain": "5238.me", "path": "/"}]
        monkeypatch.setattr(ph, "_context_cookies_for", lambda urls: cookies)
        with ph._sync_page(browser) as page:
            assert browser.opts["viewport"] == {"width": 1366, "height": 768}
            assert browser.opts["user_agent"] == ph._default_user_agent()
            assert ctx.init_scripts == [ph._ANTIFP_INIT_SCRIPT]
            assert "plugins" in ctx.init_scripts[0]
            assert ctx.cookies == cookies
        assert page.closed
        assert ctx.closed


class TestContextCookiesFor:
    def test_matches_www_and_bare_domain(self, monkeypatch):
        monkeypatch.setattr(ph, "_load_cf_cookies", lambda: {
            "www.5238.me": {"cf_clearance": "cf1", "__cf_bm": "bm1"},
            "plain.example": {"cf_clearance": "cf2"},
        })
        got = ph._context_cookies_for(["https://5238.me/comic/1"])
        pairs = {(c["name"], c["value"]) for c in got}
        assert ("cf_clearance", "cf1") in pairs
        assert ("__cf_bm", "bm1") in pairs
        # key 带 www.，访问裸域 → 同时带 www/裸域两个实例
        domains = sorted({c["domain"] for c in got
                          if c["name"] == "cf_clearance" and c["value"] == "cf1"})
        assert domains == ["5238.me", "www.5238.me"]
        # 反向：key 为裸域，访问 www. 前缀也命中
        got2 = ph._context_cookies_for(["https://www.plain.example/x"])
        assert {c["domain"] for c in got2} == {"plain.example"}
        assert ("cf_clearance", "cf2") in {(c["name"], c["value"]) for c in got2}

    def test_multiple_urls_collect_union(self, monkeypatch):
        monkeypatch.setattr(ph, "_load_cf_cookies", lambda: {
            "a.com": {"c1": "v1"},
            "b.com": {"c2": "v2"},
        })
        got = ph._context_cookies_for(["https://a.com/1", "https://b.com/2"])
        assert {(c["name"], c["value"]) for c in got} == {("c1", "v1"), ("c2", "v2")}

    def test_missing_file_or_no_match_no_error(self, monkeypatch):
        monkeypatch.setattr(ph, "_load_cf_cookies", lambda: {})
        assert ph._context_cookies_for(None) == []
        assert ph._context_cookies_for([]) == []
        assert ph._context_cookies_for(["https://whatever.example/x"]) == []
        # 文件有内容但域名不匹配 → 也不报错
        monkeypatch.setattr(ph, "_load_cf_cookies", lambda: {"other.com": {"a": "b"}})
        assert ph._context_cookies_for(["https://whatever.example/x"]) == []

    def test_invalid_url_no_error(self, monkeypatch):
        monkeypatch.setattr(ph, "_load_cf_cookies", lambda: {"a.com": {"c": "v"}})
        assert ph._context_cookies_for(["not a url"]) == []