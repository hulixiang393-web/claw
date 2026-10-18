from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

try:
    from tools.virtual_dlna_renderer import find_ffmpeg
except ModuleNotFoundError:
    from virtual_dlna_renderer import find_ffmpeg


def _run(ffmpeg_path: str, args: list[str], cwd: Path | None = None) -> tuple[bool, str]:
    completed = subprocess.run([ffmpeg_path, "-hide_banner", "-loglevel", "error", *args], capture_output=True, check=False, cwd=cwd)
    return completed.returncode == 0, completed.stderr.decode("utf-8", "replace").strip()[-1000:]


def generate_sample_set(output: Path, ffmpeg_path: str) -> dict[str, Path]:
    output.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}
    capability: dict[str, str] = {}
    mp4 = output / "sample.mp4"
    ok, error = _run(
        ffmpeg_path,
        [
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=1280x720:r=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:sample_rate=48000",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(mp4),
        ],
    )
    if ok:
        result["mp4"] = mp4
    else:
        capability["mp4"] = error or "H.264/AAC encoder unavailable"
        result["capability"] = capability
        return result

    hls_ts_dir = output / "hls_ts"
    hls_ts_dir.mkdir(exist_ok=True)
    ok, error = _run(
        ffmpeg_path,
        [
            "-y",
            "-i",
            str(mp4),
            "-c",
            "copy",
            "-f",
            "hls",
            "-hls_time",
            "1",
            "-hls_list_size",
            "0",
            str(hls_ts_dir / "index.m3u8"),
        ],
    )
    if ok:
        result["hls_ts"] = hls_ts_dir / "index.m3u8"
    else:
        capability["hls_ts"] = error or "HLS TS muxer unavailable"

    hls_fmp4_dir = output / "hls_fmp4"
    hls_fmp4_dir.mkdir(exist_ok=True)
    ok, error = _run(
        ffmpeg_path,
        [
            "-y",
            "-i",
            str(mp4),
            "-c",
            "copy",
            "-f",
            "hls",
            "-hls_time",
            "1",
            "-hls_list_size",
            "0",
            "-hls_segment_type",
            "fmp4",
            str(hls_fmp4_dir / "index.m3u8"),
        ],
    )
    if ok:
        result["hls_fmp4"] = hls_fmp4_dir / "index.m3u8"
    else:
        capability["hls_fmp4"] = error or "HLS fMP4 muxer unavailable"

    dash_dir = output / "dash"
    dash_dir.mkdir(exist_ok=True)
    ok, error = _run(
        ffmpeg_path,
        [
            "-y",
            "-i",
            str(mp4),
            "-c",
            "copy",
            "-f",
            "dash",
            "-init_seg_name",
            "init-$RepresentationID$.m4s",
            "-media_seg_name",
            "chunk-$RepresentationID$-$Number%05d$.m4s",
            "manifest.mpd",
        ],
        cwd=dash_dir,
    )
    if ok:
        result["dash"] = dash_dir / "manifest.mpd"
    else:
        capability["dash"] = error or "DASH muxer unavailable"

    webm = output / "sample.webm"
    ok, error = _run(
        ffmpeg_path,
        [
            "-y",
            "-i",
            str(mp4),
            "-c:v",
            "libvpx-vp9",
            "-c:a",
            "libopus",
            str(webm),
        ],
    )
    if ok:
        result["webm"] = webm
    else:
        capability["webm"] = error or "WebM encoder unavailable"

    if capability:
        result["capability"] = capability
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ffmpeg")
    args = parser.parse_args()
    ffmpeg_path = find_ffmpeg(args.ffmpeg)
    samples = generate_sample_set(args.output, ffmpeg_path)
    for name, path in samples.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
