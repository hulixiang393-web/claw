"""视频阅读后台任务（B4 拆分）：取流/换源探测/投屏/推荐等 QRunnable 与信号。

全部类原样迁移自 video_view.py，行为不改变。投屏线程类用守护线程而非
QThreadPool（线程存活即自持引用，避免 Python 侧先 GC 悬垂）。
"""

from __future__ import annotations

import inspect
import sys
import threading
import time

from PySide6.QtCore import QObject, QRunnable, Qt, Signal

from framework.stream_resolver import StreamBudget, StreamCancelledError
from framework.transcode_session import TranscodeSession

from gui.pages.reader.video_common import _safe_error_text


def _transcode_session_class():
    """延迟解析 TranscodeSession：优先取 video_view 模块命名空间中的同名符号。

    拆分前该类在 video_view 命名空间下被引用，测试通过 monkeypatch
    `video_view.TranscodeSession` 注入假实现；这里保持同一解析路径，
    确保行为契约（可注入性）不变。
    """
    module = sys.modules.get("gui.pages.reader.video_view")
    if module is not None:
        return getattr(module, "TranscodeSession", TranscodeSession)
    return TranscodeSession


class _VideoSignals(QObject):
    # (ep_url, video, audio, err, quality, gen)：末位 gen 是发起取流时的
    # 投屏代数（_cast_gen），仅投屏路径消费；本地路径的槽只收前 5 参，
    # Qt 对参数少于信号的槽自动丢弃多余尾参（连接规则与 C++ 槽一致）。
    finished = Signal(object, object, object, object, object, object)


class _FetchStreamTask(QRunnable):
    """后台获取播放流（fetch_video_streams，覆盖 HLS/MP4/DASH 双流）。"""

    def __init__(self, content, source, ep_url, quality="best", gen=None,
                 source_lock=False):
        super().__init__()
        self.signals = _VideoSignals()
        self._content = content
        self._source = source
        self._url = ep_url
        self._quality = quality
        self._gen = gen
        self._source_lock = source_lock
        self._cancelled = threading.Event()
        self._context = StreamBudget()

    def cancel(self) -> None:
        self._cancelled.set()
        self._context.cancel()
        invalidate = getattr(self._content, "invalidate_video_stream_cache", None)
        if callable(invalidate):
            try:
                invalidate(self._source, self._url, self._quality, True)
            except Exception:
                pass

    def run(self) -> None:
        video, audio, err = "", "", None
        try:
            if self._cancelled.is_set():
                return
            kwargs = {"quality": self._quality, "merged": True}
            try:
                parameters = inspect.signature(
                    self._content.fetch_video_streams
                ).parameters
            except (TypeError, ValueError):
                parameters = {}
            accepts_kwargs = any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            if "context" in parameters or accepts_kwargs:
                kwargs["context"] = self._context
            if "source_lock" in parameters or accepts_kwargs:
                kwargs["source_lock"] = self._source_lock
            video, audio = self._content.fetch_video_streams(
                self._source, self._url, **kwargs
            )
        except Exception as exc:  # noqa: BLE001
            err = _safe_error_text(exc)
        if self._cancelled.is_set():
            return
        try:
            self.signals.finished.emit(self._url, video, audio, err,
                                        self._quality, self._gen)
        except RuntimeError:
            pass


class _ProbeSignals(QObject):
    progress = Signal(int, int, str, object)
    finished = Signal(object, int, object)


class _ProbeSourcesTask(QRunnable):
    def __init__(self, content, candidates, generation, max_sources=5,
                 timeout_ms=30000, quality="best"):
        super().__init__()
        self.signals = _ProbeSignals()
        self._content = content
        limit = max(1, min(5, int(max_sources)))
        self._candidates = []
        seen = set()
        for candidate in candidates:
            source_id = str(candidate.get("source_id") or candidate.get("sid") or "")
            if not source_id or source_id in seen:
                continue
            seen.add(source_id)
            self._candidates.append(candidate)
            if len(self._candidates) >= limit:
                break
        self._generation = generation
        self._timeout_ms = max(1, int(timeout_ms))
        self._quality = quality
        self._cancelled = threading.Event()
        self._context = None
        self._context_lock = threading.Lock()

    def cancel(self) -> None:
        self._cancelled.set()
        with self._context_lock:
            context = self._context
        if context is not None:
            context.cancel()

    def _fetch(self, source, url, context):
        kwargs = {"quality": self._quality, "merged": True}
        try:
            parameters = inspect.signature(self._content.fetch_video_streams).parameters
        except (TypeError, ValueError):
            parameters = {}
        if "context" in parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            kwargs["context"] = context
        return self._content.fetch_video_streams(source, url, **kwargs)

    def run(self) -> None:
        results = {}
        total = len(self._candidates)
        for number, candidate in enumerate(self._candidates, 1):
            if self._cancelled.is_set():
                return
            source_id = str(candidate.get("source_id") or candidate.get("sid") or "")
            try:
                self.signals.progress.emit(number, total, source_id, self)
            except RuntimeError:
                return
            source = candidate.get("source")
            url = candidate.get("url") or candidate.get("episode_url") or ""
            context = StreamBudget(time.monotonic() + self._timeout_ms / 1000.0)
            with self._context_lock:
                self._context = context
            done = threading.Event()
            value = []
            error = []

            def fetch():
                try:
                    value.append(self._fetch(source, url, context))
                except Exception as exc:
                    error.append(exc)
                finally:
                    done.set()

            thread = threading.Thread(target=fetch, daemon=True)
            thread.start()
            timed_out = not done.wait(self._timeout_ms / 1000.0)
            if timed_out:
                context.cancel()
                done.wait()
            with self._context_lock:
                self._context = None
            if self._cancelled.is_set():
                return
            if timed_out:
                results[source_id] = "timeout"
                continue
            if error:
                if isinstance(error[0], StreamCancelledError):
                    return
                results[source_id] = "failed"
                continue
            pair = value[0] if value else ("", "")
            video = pair[0] if isinstance(pair, (tuple, list)) and pair else pair
            audio = pair[1] if isinstance(pair, (tuple, list)) and len(pair) > 1 else ""
            results[source_id] = "ok" if video or audio else "failed"
        try:
            self.signals.finished.emit(results, self._generation, self)
        except RuntimeError:
            pass


class _CastSignals(QObject):
    # 第二参是产生该信号的 _CastPollTask：停投+再投后旧任务的迟到信号在
    # 槽内按身份比对丢弃（F5-1）。失败原因放**首参**，1 参的旁路槽
    # （如测试里的 seen.append）经 Qt 丢尾参拿到的仍是原因而非任务对象。
    state_changed = Signal(object, object)  # (poll_state() 结果, 任务实例)
    poll_failed = Signal(str, object)       # (连续失败原因, 任务实例)


class _CastPollTask:
    """投屏状态后台轮询：每 interval 秒调一次 session.poll_state()。

    **只发 Qt 信号，绝不碰控件**——控件更新全在 VideoView 的主线程槽里。
    连续 max_failures 次抛错才判定失败并停止轮询，中途任一次成功即清零计数
    （网络抖一下不该把会话判死）。

    用普通守护线程而非 QRunnable：线程池只持**裸指针**，Python 侧先 GC 就把
    C++ QRunnable 释放了，池随后再排到它 → 悬垂指针（全量回归里直接 AV 崩）。
    改用 threading 后，线程的 target 是本对象的 run()，线程活着就自持引用，
    不依赖任何外部持有者。
    """

    def __init__(self, session, interval: float = 1.0, max_failures: int = 3):
        self.signals = _CastSignals()
        self._session = session
        self._interval = interval
        self._max_failures = max_failures
        self._running = True
        self.failures = 0
        self._thread = None

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        """起后台线程（幂等）。"""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self.run, daemon=True,
                                        name="cast-poll")
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def tick(self) -> bool:
        """单轮轮询；返回 False 表示应停止轮询。

        poll_state() 是网络调用（单次最长 ~10s），期间可能被 stop()（停投）
        甚至「停投 + 再投」——返回后必须复核 _running 才允许 emit，否则迟到
        的 PLAYING 会撤掉**新**会话的 8s 超时表、迟到的失败会误杀新会话（F5-1）。
        """
        if not self._running:
            return False
        try:
            state = self._session.poll_state()
        except Exception as exc:  # noqa: BLE001 —— 网络/SOAP 全部按轮询失败计
            if not self._running:  # 停投后才到达的失败：不再上报
                return False
            self.failures += 1
            if self.failures >= self._max_failures:
                self._running = False
                try:
                    self.signals.poll_failed.emit(
                        str(exc) or exc.__class__.__name__, self)
                except RuntimeError:
                    pass
                return False
            return True
        if not self._running:  # 网络调用期间被停：迟到结果不再 emit
            return False
        self.failures = 0
        try:
            self.signals.state_changed.emit(state, self)
        except RuntimeError:
            pass
        return True

    def run(self) -> None:
        while self._running:
            if not self.tick():
                break
            # 分段睡眠：stop() 能在一个 step 内生效，不用等满整个 interval
            deadline = time.monotonic() + self._interval
            while self._running:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                time.sleep(min(0.1, left))


class _CastStartSignals(QObject):
    finished = Signal(int, object, object)  # (代数, 会话, 异常|None)（worker emit）


class _CastStartTask:
    """CastSession.start() 的后台执行体（spec §7：Qt 主线程只处理信号）。

    start 内含 LAN 端点启动 + SetAVTransportURI + Play 三次同步网络调用
    （DlnaClient 单次超时 5s，最坏 ~10s），放 GUI 线程会把界面冻结十几秒。
    与 _CastPollTask 同理用守护线程而非 QRunnable：线程 target 是本对象的
    run()，线程活着即自持引用，不依赖外部持有者（线程池只持 QRunnable 裸
    指针，Python 侧先 GC 会悬垂）。完成只 emit 信号排队回主线程，槽按代数
    判定结果是否仍有效（启动期间 _stop_cast 作废的流程在槽里收尾）。
    """

    def __init__(self, session, renderer, idx, video, title, headers,
                  ad_block, force_proxy, gen: int, cast_tuning=None):
        self.signals = _CastStartSignals()
        self.session = session
        self.renderer = renderer
        self.idx = idx
        self.video = video
        self.title = title
        self.headers = headers
        self.ad_block = ad_block
        self.force_proxy = force_proxy
        self.gen = gen
        self.cast_tuning = cast_tuning or {}
        self._thread = None

    def start(self) -> None:
        """起后台线程（幂等）。"""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self.run, daemon=True,
                                        name="cast-start")
        self._thread.start()

    def run(self) -> None:
        exc = None
        try:
            args = (self.renderer, self.idx, self.video, self.title,
                    self.headers, self.ad_block, self.force_proxy)
            if self.cast_tuning:
                self.session.start(*args, cast_tuning=self.cast_tuning)
            else:
                self.session.start(*args)
        except Exception as e:  # noqa: BLE001 —— 网络/SOAP/设备拒绝
            exc = e
        try:
            self.signals.finished.emit(self.gen, self.session, exc)
        except RuntimeError:
            pass


class _CastTranscodeSignals(QObject):
    finished = Signal(int, object, object, object)


class _CastTranscodeTask:
    def __init__(self, session, proxy, renderer, idx, video, title, headers, gen):
        self.signals = _CastTranscodeSignals()
        self.session = session
        self.proxy = proxy
        self.renderer = renderer
        self.idx = idx
        self.video = video
        self.title = title
        self.headers = headers
        self.gen = gen
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self.run, daemon=True,
                                        name="cast-transcode")
        self._thread.start()

    def run(self):
        transcode = None
        exc = None
        try:
            from framework.cast_session import _local_ip_for_renderer

            public_base = self.proxy.start_lan_endpoint(
                _local_ip_for_renderer(self.renderer)
            )
            transcode = _transcode_session_class()(media_proxy=self.proxy)
            playlist_url = transcode.start(self.video, self.headers, public_base)
            self.session.start_transcoded(
                self.renderer, self.idx, playlist_url, self.title,
                transcode_session=transcode,
            )
        except Exception as error:  # noqa: BLE001
            exc = error
            if transcode is not None:
                try:
                    transcode.stop()
                except Exception:
                    pass
        try:
            self.signals.finished.emit(self.gen, self.session, transcode, exc)
        except RuntimeError:
            pass


class _RecommendSignals(QObject):
    finished = Signal(object, object, object, object)  # (source_id, keyword, results, err)


class _RecommendTask(QRunnable):
    """后台搜索当前作品的同源相关视频（推荐加载，失败静默）。"""

    def __init__(self, search, source, keyword):
        super().__init__()
        self.signals = _RecommendSignals()
        self._search = search
        self._source = source
        self._keyword = keyword

    def run(self) -> None:
        # 独立 HttpClient：requests.Session 非线程安全，后台线程不共享
        # 全局 Search 的实例（与 search_type 的 worker 同模式）
        http = None
        try:
            http = self._search._http.__class__(
                sleeper=getattr(self._search._http, "_sleeper", None),
                defaults=self._search._http.defaults,
            )
        except Exception:  # noqa: BLE001
            http = None
        results, err = [], None
        try:
            results = self._search.search_one(self._source, self._keyword, http=http)
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
        finally:
            if http is not None:
                try:
                    http.close()
                except Exception:  # noqa: BLE001
                    pass
        try:
            self.signals.finished.emit(
                getattr(self._source, "source_id", ""), self._keyword, results or [], err
            )
        except RuntimeError:
            pass
