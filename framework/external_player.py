"""外部播放器调用（external_player.py）。

播放交给独立播放器进程（VLC 桌面版优先，浏览器兜底），不再内嵌 libvlc：
- 独立进程完整渲染管线，无 Qt 主线程/内嵌 hwnd 干扰 → 1080p+ 流畅
- VLC 桌面版自带"恢复播放位置"（记住每个媒体的进度）→ 续读由播放器自己接管
- 防盗链透传：VLC 命令行 --http-referrer 只对 Referer 生效，且实测 VLC 3.0.23
  无法覆盖 User-Agent（CDN 常拒绝 VLC 默认 UA）。因此**带防盗链头的媒体一律
  走本地代理（framework/media_proxy.py）**：代理把源配置的完整 headers
  （Referer/UA/Cookie…）打在请求上，播放器只播本地回环 URL，全类型可看。

播放器探测顺序：
1. VLC 桌面版（常见安装路径 + PATH）
2. 系统默认打开方式（webbrowser / os.startfile）
"""
import os
import shutil
import subprocess
import webbrowser

from .media_proxy import proxy_url_for

_VLC_CANDIDATES = [
    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
    r"C:\Users\%s\AppData\Local\Programs\VLC\vlc.exe" % os.environ.get("USERNAME", ""),
]

_found_vlc = None


def _locate_vlc() -> str | None:
    """定位 VLC 桌面版可执行文件（候选路径 + PATH）。"""
    global _found_vlc
    if _found_vlc is not None:
        return _found_vlc or None
    for c in _VLC_CANDIDATES:
        if os.path.isfile(c):
            _found_vlc = c
            return c
    v = shutil.which("vlc")
    if v:
        _found_vlc = v
        return v
    _found_vlc = ""
    return None


def open_with_player(url: str, audio: str = "", referer: str = "",
                     user_agent: str = "", headers: dict | None = None) -> str:
    """用外部播放器打开媒体地址。

    url      媒体直链（单流）
    audio    DASH 音频轨地址（非空时以 input-slave 挂入）
    referer / user_agent / headers  防盗链透传。referer/user_agent 是兼容旧
            调用的便捷参数；headers 提供完整头（含 Cookie 等）。任何防盗链
            头存在时走本地代理（VLC 无法设置 UA，只有代理能根治）。
    """
    if not url:
        return ""
    vlc = _locate_vlc()
    if vlc:
        # 汇总防盗链头
        hdrs = dict(headers or {})
        if referer:
            hdrs.setdefault("Referer", referer)
        if user_agent:
            hdrs.setdefault("User-Agent", user_agent)
        if hdrs:
            # 带防盗链 → 本地代理（代理打完整 headers）
            play_url = proxy_url_for(url, hdrs)
            audio_url = proxy_url_for(audio, hdrs) if audio else ""
        else:
            play_url = url
            audio_url = audio
        args = [vlc, "--no-video-title-show", play_url]
        if audio_url:
            args.append(f":input-slave={audio_url}")
        try:
            subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True
            )
            return "已用外部播放器打开"
        except Exception:  # noqa: BLE001 —— VLC 启动失败降级系统默认
            pass
    webbrowser.open(url)
    return "已在浏览器中打开"
