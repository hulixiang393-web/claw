"""media_tuner 通用调优框架测试。"""
import time

import pytest

from framework.media_tuner import MediaTuner, classify


class TestClassify:
    def test_hls_url(self):
        p = classify("https://cdn.example.com/video/index.m3u8?token=1")
        assert p.kind == "hls"
        assert p.buffer_ms >= 3000

    def test_dash_url(self):
        p = classify("https://cdn.example.com/stream/video.mpd")
        assert p.kind == "dash"
        assert p.buffer_ms >= 3000

    def test_live_flv(self):
        p = classify("https://live.example.com/live/room.flv")
        assert p.kind == "live"

    def test_mp4_direct(self):
        p = classify("https://cdn.example.com/movie/01.mp4")
        assert p.kind == "mp4"
        assert p.buffer_ms < 2000  # 渐进式下载不用大缓冲

    def test_unknown(self):
        p = classify("https://cdn.example.com/weird/path")
        assert p.kind == "unknown"
        assert 2000 <= p.buffer_ms <= 3000

    def test_empty_url(self):
        p = classify("")
        assert p.kind == "unknown"


class TestMediaTuner:
    def test_stall_count_window(self):
        tuner = MediaTuner(window_s=0.2)
        tuner.on_buffering(30)
        assert tuner.stall_count() == 1

    def test_repeated_buffering_counted_once(self):
        tuner = MediaTuner()
        # 同一次缓冲：百分比持续变化但不回到 100，只计一次
        tuner.on_buffering(20)
        tuner.on_buffering(35)
        tuner.on_buffering(60)
        assert tuner.stall_count() == 1

    def test_new_buffer_after_resume(self):
        tuner = MediaTuner()
        tuner.on_buffering(20)
        tuner.on_buffering(100)  # 恢复
        tuner.on_buffering(10)   # 再次缓冲
        assert tuner.stall_count() == 2

    def test_should_upgrade(self):
        tuner = MediaTuner(threshold=2)
        tuner.on_buffering(10)
        tuner.on_buffering(100)
        tuner.on_buffering(10)
        assert tuner.should_upgrade()

    def test_upgrade_steps(self):
        tuner = MediaTuner()
        # hls：3500 → 4000 → 6000 → 8000（封顶）
        steps = []
        ms = tuner.next_buffer_ms("https://x.com/a.m3u8")
        while ms is not None:
            steps.append(ms)
            ms = tuner.next_buffer_ms("https://x.com/a.m3u8")
        assert steps == [4000, 6000, 8000], steps

    def test_upgrade_capped_by_profile(self):
        tuner = MediaTuner()
        # mp4 上限 6000：第二级 6000 后封顶
        steps = []
        ms = tuner.next_buffer_ms("https://x.com/a.mp4")
        while ms is not None:
            steps.append(ms)
            ms = tuner.next_buffer_ms("https://x.com/a.mp4")
        assert steps == [4000, 6000], steps
        assert tuner.next_buffer_ms("https://x.com/a.mp4") is None

    def test_reset_clears_stats(self):
        tuner = MediaTuner(threshold=1)
        tuner.on_buffering(10)
        assert tuner.should_upgrade()
        tuner.reset()
        assert not tuner.should_upgrade()

    def test_percent_100_never_counts(self):
        tuner = MediaTuner()
        tuner.on_buffering(100)
        assert tuner.stall_count() == 0

    def test_buffering_drop_by_10_counts_new(self):
        tuner = MediaTuner()
        tuner.on_buffering(80)
        tuner.on_buffering(40)  # 回退 >=10 → 新的缓冲
        assert tuner.stall_count() == 2

    def test_buffering_growth_same_session(self):
        tuner = MediaTuner()
        tuner.on_buffering(20)
        tuner.on_buffering(35)
        tuner.on_buffering(60)  # 增长：同一轮缓冲，不重复计数
        assert tuner.stall_count() == 1
        tuner.on_buffering(30)  # 回退 → 新一轮
        assert tuner.stall_count() == 2

    def test_thread_safe(self):
        tuner = MediaTuner()
        import threading

        errs = []

        def worker():
            try:
                for _ in range(50):
                    tuner.on_buffering(10)
                    tuner.on_buffering(100)
                    tuner.stall_count()
            except Exception as exc:  # noqa: BLE001
                errs.append(exc)

        ts = [threading.Thread(target=worker) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert not errs
