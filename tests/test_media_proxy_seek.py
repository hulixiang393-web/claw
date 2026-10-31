# -*- coding: utf-8 -*-
"""拖动（seek）相关的传输层契约。

现场问题：omofuna「精品」线是 272MB 单个 mp4，mpv 一拖进度条就永久卡住。
mpv 日志给出根因：

    [9.076][ffmpeg] stream level seek from 26787348 to 45989093
    [45.290][ffmpeg] Seek failed (to 45989093, size 285167868)
    [45.292][ffmpeg] AVIOContext: Statistics: 26787348 bytes read, 0 seeks

`0 seeks` = AVIOContext 一次都没 seek 成功，它只能"读并丢弃字节"去够目标偏移
（拖到 30% 要白读 80MB 才失败）。原因是代理在 HTTP/1.1 keep-alive 上写整片
长流，播放器在这条连接上发出的 seek 请求要等整片写完才会被读到。

修法有两条，本文件分别锁住：
1. 大文件长流响应声明 `Connection: close`（分片仍然 keep-alive）；
2. 拖动请求（带 Range）绝不为缓存抓取排队。
"""
from __future__ import annotations

import inspect

import framework.media_proxy as mp


class _FakeHandler:
    def __init__(self):
        self.headers = {}
        self.sent = []
        self.close_connection = False
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent.append((name, value))

    def end_headers(self):
        pass

    def header(self, name):
        for key, value in self.sent:
            if key.lower() == name.lower():
                return value
        return None


class _FakeResp:
    def __init__(self, headers, status=200):
        self.headers = headers
        self.status_code = status


def test_large_stream_declares_connection_close():
    """272MB 的 mp4 必须声明 Connection: close，否则 seek 请求排不上队。"""
    handler = _FakeHandler()
    resp = _FakeResp({"Content-Type": "video/mp4",
                      "Content-Length": str(285167868)})
    mp._send_stream_headers(handler, resp,
                            close_if_larger=mp._LARGE_STREAM_CLOSE_BYTES)
    assert handler.header("Connection") == "close"
    assert handler.close_connection is True


def test_small_segment_keeps_keep_alive():
    """HLS 分片仍然复用连接（握手成本才是分片的主要开销）。"""
    handler = _FakeHandler()
    resp = _FakeResp({"Content-Type": "video/mp2t", "Content-Length": "1048576"})
    mp._send_stream_headers(handler, resp,
                            close_if_larger=mp._LARGE_STREAM_CLOSE_BYTES)
    assert handler.header("Connection") is None
    assert handler.close_connection is False


def test_threshold_is_above_typical_segment():
    """阈值要明显大于常见 HLS 分片，否则会把分片也踢出 keep-alive。"""
    assert mp._LARGE_STREAM_CLOSE_BYTES >= 4 * 1024 * 1024


def test_only_single_url_route_uses_large_close():
    """/c/ 分片路由不能带这个大文件策略。"""
    source = inspect.getsource(mp.MediaProxy._serve_cache)
    assert "close_if_larger=_LARGE_STREAM_CLOSE_BYTES" not in source


def test_forward_media_uses_large_close():
    source = inspect.getsource(mp.MediaProxy._forward_media)
    assert "close_if_larger=_LARGE_STREAM_CLOSE_BYTES" in source


def test_range_request_does_not_wait_for_cache_fetch():
    """带 Range 的拖动请求不排队等别人的整片抓取。"""
    source = inspect.getsource(mp.MediaProxy._forward_media)
    # 等待逻辑只能出现在非 Range 分支里
    assert "is_range = bool(handler.headers.get(\"Range\"))" in source
    guard = source.split('is_range = bool(handler.headers.get("Range"))', 1)[1]
    head = guard.split("# ", 1)[0]
    assert "_wait_for_cached_fetch" not in head
