"""外部播放器调用（external_player.py）。

播放交给独立播放器进程（VLC 桌面版优先，浏览器兜底），不再内嵌 libvlc：
- 独立进程完整渲染管线，无 Qt 主线程/内嵌 hwnd 干扰 → 1080p+ 流畅
- VLC 桌面版自带"恢复播放位置"（记住每个媒体的进度）→ 续读由播放器自己接管
- 支持 Referer/UA 透传（CDN 防盗链直链也能播）与 DASH 双流 input-slave

播放器探测顺序：
1. VLC 桌面版（常见安装路径 + PATH）
2. 系统默认打开方式（webbrowser / os.startfile）
"""
import os
import shutil
import subprocess
import sys
import webbrowser

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
                     user_agent: str = "") -> str:
    """用外部播放器打开媒体地址。

    url      媒体直链（单流）
    audio    DASH 音频轨地址（非空时以 input-slave 挂入）
    referer / user_agent  防盗链透传（VLC 命令行参数；浏览器兜底时忽略）

    返回提示文案（状态浮层用），抛出说明用不用管。
    """
    if not url:
        return ""
    vlc = _locate_vlc()
    if vlc:
        args = [vlc, "--no-video-title-show", url]
        if audio:
            args.append(f":input-slave={audio}")
        if referer:
            args.append(f"--http-referrer={referer}")
        if user_agent:
            args.append(f"--http-user-agent={user_agent}")
        try:
            # 独立进程启动，不等待不回调；同 URL 会被 VLC 复用实例（单实例）
            subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True
            )
            return "已用外部播放器打开"
        except Exception:  # noqa: BLE001 —— VLC 启动失败降级系统默认
            pass
    webbrowser.open(url)
    return "已在浏览器中打开"
