# -*- coding: utf-8 -*-
"""adblock HTML 内容层广告过滤测试。

覆盖：
1. filter_html：CSS 广告节点剔除（内置通用 + 源级 extra_css 追加）
2. filter_text：广告文本特征行过滤
3. is_ad_url / filter_m3u8 向后兼容（回归）
4. 广告图片位 URL 识别（img tag src 命中广告域名/路径特征）
"""
import pytest

from framework.adblock import AdblockEngine

HTML_WITH_ADS = """
<html><body>
<div id="content">
<p>正常正文第一段，讲的是主角起床。</p>
<p>正常正文第二段。</p>
<div class="ads"><a href="https://ad.example.com/x">点我下载</a></div>
<div class="read-ad">手机阅读请访问 m.example.com 观看更多</div>
<div id="adtext">这里是广告正文插入内容，请记住本站域名。</div>
<ins class="adsbygoogle"></ins>
<iframe src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js"></iframe>
<div class="banner-ad"><img src="https://img.example.com/banner_468x60.jpg"></div>
<p>正文第三段，继续剧情。</p>
<p>（广告）点击进入领取福利</p>
<p>正文结尾段。</p>
</div>
</body></html>
"""


class TestFilterHtml:
    def test_remove_ad_nodes(self):
        eng = AdblockEngine()
        out = eng.filter_html(HTML_WITH_ADS)
        # 广告节点及其文本被剔除
        assert "点我下载" not in out
        assert "手机阅读请访问" not in out
        assert "adsbygoogle" not in out
        assert "banner_468x60" not in out
        # 正文保留
        assert "主角起床" in out
        assert "继续剧情" in out
        assert "正文结尾段" in out

    def test_extra_css_source_override(self):
        class FakeSource:
            raw = {
                "ad_block": {
                    "enabled": True,
                    "extra_css": [".custom-ad", "#site-ad"],
                }
            }

        eng = AdblockEngine(FakeSource())
        html = (
            "<div class='custom-ad'>垃圾广告</div>"
            "<div id='site-ad'>另一个广告</div>"
            "<p>正文</p>"
        )
        out = eng.filter_html(html)
        assert "垃圾广告" not in out
        assert "另一个广告" not in out
        assert "正文" in out

    def test_text_ad_line_filter(self):
        eng = AdblockEngine()
        out = eng.filter_text("这是正文\n点击进入查看详情\n（广告）推广\n正文继续")
        assert "点击进入" not in out
        assert "推广" not in out
        assert "正文" in out

    def test_ad_image_url_detection(self):
        eng = AdblockEngine()
        assert eng.is_ad_image_url("https://img.example.com/banner_468x60.jpg")
        assert eng.is_ad_image_url("https://ad.example.com/pic.jpg")
        assert not eng.is_ad_image_url("https://img.example.com/comics/123/1.jpg")


class TestM3u8StreamAds:
    """m3u8 流内广告段过滤（xuandm 实测：重复插入的片头/片尾推广块）。"""

    # 同一 4 段素材（KEY:NONE 无加密）在列表中重复插入两次 → 全部判广告
    DUP_BLOCK = (
        "#EXTM3U\n#EXT-X-VERSION:3\n"
        "#EXT-X-KEY:METHOD=AES-128,URI=\"/seg/key.key\"\n"
        "#EXTINF:2.0,\n/seg/001.ts\n"
        "#EXT-X-DISCONTINUITY\n#EXT-X-KEY:METHOD=NONE\n"
        "#EXTINF:5.0,\n/admaterial/VWd1eJQz.ts\n"
        "#EXTINF:5.0,\n/admaterial/6UkoUQLF.ts\n"
        "#EXT-X-DISCONTINUITY\n#EXT-X-KEY:METHOD=AES-128,URI=\"/seg/key.key\"\n"
        "#EXTINF:2.0,\n/seg/002.ts\n"
        "#EXTINF:2.0,\n/seg/003.ts\n"
        "#EXT-X-DISCONTINUITY\n#EXT-X-KEY:METHOD=NONE\n"
        "#EXTINF:5.0,\n/admaterial/VWd1eJQz.ts\n"
        "#EXTINF:5.0,\n/admaterial/6UkoUQLF.ts\n"
        "#EXT-X-ENDLIST\n"
    )

    def test_repeated_ad_block_removed(self):
        eng = AdblockEngine()
        out = eng.filter_m3u8(self.DUP_BLOCK, "https://cdn.example.com/hls/index.m3u8")
        assert "VWd1eJQz" not in out and "6UkoUQLF" not in out
        segs = [l for l in out.splitlines() if l and not l.startswith("#")]
        assert segs == ["/seg/001.ts", "/seg/002.ts", "/seg/003.ts"]
        # AES 加密 KEY 保留、KEY:NONE 清理
        assert "#EXT-X-KEY:METHOD=AES-128" in out
        assert "#EXT-X-KEY:METHOD=NONE" not in out
        # 列表合法：EXTINF 数与段数一致
        extinf = sum(1 for l in out.splitlines() if l.startswith("#EXTINF"))
        assert extinf == len(segs)

    def test_normal_short_seg_after_discontinuity_kept(self):
        # DISCONTINUITY 后 2s 段是正片第一段（xuandm seg96），不能被旧规则误删
        eng = AdblockEngine()
        m3u8 = (
            "#EXTM3U\n"
            "#EXT-X-KEY:METHOD=AES-128,URI=\"/k.key\"\n"
            "#EXTINF:2.0,\n/seg/096.ts\n"
            "#EXTINF:2.0,\n/seg/097.ts\n"
            "#EXTINF:2.0,\n/seg/098.ts\n#EXT-X-ENDLIST\n"
        )
        assert eng.filter_m3u8(m3u8, "https://cdn.example.com/hls/i.m3u8") == m3u8

    def test_isolated_short_block_removed(self):
        # 被 DISCONTINUITY 包裹、块总时长 <3s → 广告（替代旧「单段 <3s」启发式）
        eng = AdblockEngine()
        m3u8 = (
            "#EXTM3U\n"
            "#EXT-X-DISCONTINUITY\n#EXTINF:1.5,\nhttps://cdn.example.com/seg/1003.ts\n"
            "#EXT-X-DISCONTINUITY\n#EXTINF:10.0,\nhttps://cdn.example.com/seg/1004.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        out = eng.filter_m3u8(m3u8, "https://cdn.example.com/play/list.m3u8")
        assert "1003.ts" not in out and "1004.ts" in out


class TestBackwardCompat:
    def test_is_ad_url_still_works(self):
        eng = AdblockEngine()
        assert eng.is_ad_url("https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js")
        assert not eng.is_ad_url("https://cdn.example.com/seg/1001.ts")

    def test_filter_m3u8_still_works(self):
        eng = AdblockEngine()
        m3u8 = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n" \
               "#EXT-X-DISCONTINUITY\n#EXTINF:2.0,\nhttps://cdn.example.com/ads/ad1.ts\n" \
               "#EXTINF:10.0,\nhttps://cdn.example.com/seg/1001.ts\n#EXT-X-ENDLIST\n"
        out = eng.filter_m3u8(m3u8)
        assert "ad1.ts" not in out
        assert "1001.ts" in out

    def test_configure_reset(self):
        class FakeSource:
            raw = {"ad_block": {"enabled": True, "extra_css": [".a"]}}

        eng = AdblockEngine(FakeSource())
        assert eng._extra_css == [".a"]

        class FakeSource2:
            raw = {"ad_block": {"enabled": True, "extra_css": [".b"]}}

        eng.configure(FakeSource2())
        assert eng._extra_css == [".b"]
        assert ".a" not in eng._extra_css
