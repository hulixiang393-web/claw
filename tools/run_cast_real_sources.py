from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from framework.cast_session import CastSession
from framework.config import SourceConfig, load_source
from framework.content import Content
from framework.decrypter import Decrypter
from framework.discovery import Discovery
from framework.dlna import DlnaClient
from framework.http import HttpClient
from framework.media_proxy import MediaProxy
from framework.parser import Parser
from framework.selfcheck import StructureChecker
from tools.virtual_dlna_renderer import VirtualRenderer, find_ffmpeg


ALLOWED_FAILURE_CLASSES = {
    "source_unavailable",
    "proxy_unreachable",
    "unsupported_format",
    "ffmpeg_decode_error",
    "protocol_error",
    "decoded",
}

REAL_SOURCE_IDS = ("dm84", "omofuna")


def _safe_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"https?://[^\s)]+", "<redacted-url>", text)
    text = re.sub(r"([?&](?:token|key|sign|signature|auth|expires)=[^&\s]+)", r"\1=<redacted>", text, flags=re.I)
    return text[:500]


def _safe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {_safe_text(exc)}"


def _source_record(source: SourceConfig) -> dict:
    http = HttpClient()
    parser = Parser()
    checker = StructureChecker(http, parser)
    content = Content(http, parser, checker, decrypter=Decrypter(http))
    discovery = Discovery(http, parser, checker)
    return {
        "source_id": source.source_id,
        "media_type": source.content_type,
        "source": source,
        "content": content,
        "http": http,
        "discovery": discovery,
        "episode_url": "",
        "title": source.source_name,
        "source_available": True,
    }


def _discover_one(source: SourceConfig) -> dict:
    record = _source_record(source)
    try:
        discovery = record["discovery"]
        config = source.get_discovery_config()
        list_url = config.get("list_url") or source.base_url
        works = discovery.list_works(source, list_url, 1)
        if not works:
            raise RuntimeError("discovery returned no works")
        work = works[0]
        detail = record["content"].fetch_detail(source, work.url)
        if not detail.chapters:
            raise RuntimeError("detail returned no playable chapters")
        record["episode_url"] = detail.chapters[0].url
        record["title"] = detail.title or work.title or source.source_name
        return record
    except Exception as exc:
        record["source_available"] = False
        record["error"] = _safe_error(exc)
        return record


def discover_project_real_sources() -> list[dict]:
    results = []
    sources_dir = ROOT / "sources"
    for source_id in REAL_SOURCE_IDS:
        path = sources_dir / f"{source_id}.json"
        if not path.is_file():
            results.append({
                "source_id": source_id,
                "media_type": "video",
                "source_available": False,
                "error": "source configuration is missing",
            })
            continue
        try:
            source = load_source(path)
        except Exception as exc:
            results.append({
                "source_id": source_id,
                "media_type": "video",
                "source_available": False,
                "error": _safe_error(exc),
            })
            continue
        if not source.enabled:
            results.append({
                "source_id": source_id,
                "media_type": source.content_type,
                "source_available": False,
                "error": "source is disabled",
            })
            continue
        results.append(_discover_one(source))
    return results


def _probe_fields(result: dict | None) -> dict:
    if not isinstance(result, dict):
        return {}
    return {
        key: result[key]
        for key in (
            "returncode",
            "timed_out",
            "container",
            "duration",
            "video_codec",
            "audio_codec",
            "stderr_summary",
        )
        if key in result
    }


def classify_failure(result: dict) -> str:
    if result.get("decoded") is True:
        return "decoded"
    if result.get("proxy_error"):
        return "proxy_unreachable"
    if result.get("source_available") is False:
        return "source_unavailable"
    if result.get("protocol_error"):
        return "protocol_error"
    probe = result.get("probe") if isinstance(result.get("probe"), dict) else result
    decode = result.get("decode") if isinstance(result.get("decode"), dict) else result
    probe_returncode = result.get("probe_returncode", probe.get("returncode"))
    decode_returncode = result.get("decode_returncode", decode.get("returncode"))
    if probe_returncode not in (None, 0):
        return "unsupported_format"
    if result.get("unsupported_format"):
        return "unsupported_format"
    has_decode_evidence = any(
        key in result or key in decode
        for key in ("returncode", "timed_out", "stderr_summary")
    )
    if decode_returncode not in (None, 0) or (result.get("decoded") is False and has_decode_evidence):
        return "ffmpeg_decode_error"
    return "protocol_error"


def _public_result(source: dict, **fields) -> dict:
    result = {
        "source_id": source.get("source_id", "unknown"),
        "media_type": source.get("media_type", "video"),
        "http_status": fields.get("http_status"),
        "first_byte_ms": fields.get("first_byte_ms"),
        "probe": _probe_fields(fields.get("probe")),
        "decode": _probe_fields(fields.get("decode")),
    }
    if fields.get("error"):
        result["error"] = _safe_text(fields["error"])
    if fields.get("proxy_error"):
        result["proxy_error"] = True
    if fields.get("protocol_error"):
        result["protocol_error"] = True
    result["decoded"] = bool(fields.get("decoded"))
    result["classification"] = classify_failure({**fields, **result})
    return result


def _measure_proxy_url(url: str, timeout: float) -> tuple[int | None, float | None]:
    started = time.perf_counter()
    request = Request(url, headers={"User-Agent": "claw-cast-real-source-test/1"})
    with urlopen(request, timeout=timeout) as response:
        first = response.read(1)
        if not first:
            raise RuntimeError("proxy returned an empty response")
        return response.status, (time.perf_counter() - started) * 1000


def run_real_source_case(source: dict, ffmpeg_path: str | None = None) -> dict:
    if source.get("source_available") is False:
        return _public_result(source, source_available=False, error=source.get("error"))
    ffmpeg = ffmpeg_path or find_ffmpeg()
    proxy = MediaProxy()
    renderer = VirtualRenderer(ffmpeg_path=ffmpeg, fetch_timeout=30.0)
    session = None
    try:
        source_config = source["source"]
        content = source["content"]
        video, audio = content.fetch_video_streams(
            source_config,
            source["episode_url"],
            quality="best",
            merged=True,
        )
        if not video:
            return _public_result(source, source_available=False, error="project resolver returned no video stream")
        renderer.start()
        session = CastSession(
            proxy,
            lambda current_renderer: DlnaClient(current_renderer, timeout=30.0),
        )
        session.start(
            renderer._descriptor(),
            0,
            video,
            source.get("title") or source_config.source_name,
            source_config.request_headers(),
            source_config.raw.get("ad_block"),
            True,
        )
        lan_url = session._current_url
        http_status, first_byte_ms = _measure_proxy_url(lan_url, 30.0)
        diagnostics = renderer.diagnostics()
        state = session.poll_state()
        decoded = state.get("state") == "PLAYING" and diagnostics.get("decoded") is True
        return _public_result(
            source,
            http_status=http_status,
            first_byte_ms=first_byte_ms,
            probe=diagnostics.get("probe"),
            decode=diagnostics.get("decode"),
            decoded=decoded,
            protocol_error=not decoded and not diagnostics.get("decode"),
        )
    except FileNotFoundError as exc:
        return _public_result(source, error=exc, decode={"returncode": None})
    except Exception as exc:
        diagnostics = renderer.diagnostics()
        message = _safe_text(exc)
        proxy_error = isinstance(exc, (ConnectionError, TimeoutError, OSError)) and not diagnostics
        return _public_result(
            source,
            http_status=diagnostics.get("http_status"),
            first_byte_ms=diagnostics.get("first_byte_ms"),
            probe=diagnostics.get("probe"),
            decode=diagnostics.get("decode"),
            decoded=False,
            proxy_error=proxy_error,
            protocol_error=not proxy_error,
            error=message,
        )
    finally:
        if session is not None:
            try:
                session.stop()
            except Exception:
                pass
        else:
            proxy.stop_lan_endpoint()
        renderer.stop()
        proxy.stop()


def _render_report(results: list[dict]) -> str:
    lines = [
        "# 真实源投屏诊断报告",
        "",
        "测试仅在显式设置 `CAST_REAL_SOURCES=1` 时运行。记录字段限制为 source id、媒体类型、HTTP 状态、首包耗时、probe/decode 结果和分类；未记录媒体 URL、请求头或 token。",
        "",
        "| source id | 媒体类型 | HTTP 状态 | 首包耗时（ms） | probe/decode | 分类 |",
        "| --- | --- | ---: | ---: | --- | --- |",
    ]
    for result in results:
        probe = result.get("probe") or {}
        decode = result.get("decode") or {}
        probe_decode = f"probe={probe.get('returncode', 'n/a')}; decode={decode.get('returncode', 'n/a')}"
        lines.append(
            f"| {result.get('source_id', '')} | {result.get('media_type', '')} | "
            f"{result.get('http_status') or 'n/a'} | {result.get('first_byte_ms') or 'n/a'} | "
            f"{probe_decode} | {result.get('classification', 'protocol_error')} |"
        )
    lines.extend(["", "## 结果详情", ""])
    for result in results:
        lines.append(f"### {result.get('source_id', 'unknown')}")
        lines.append("")
        lines.append(f"- 分类：`{result.get('classification')}`")
        lines.append(f"- probe：`{json.dumps(result.get('probe') or {}, ensure_ascii=False, sort_keys=True)}`")
        lines.append(f"- decode：`{json.dumps(result.get('decode') or {}, ensure_ascii=False, sort_keys=True)}`")
        if result.get("error"):
            lines.append(f"- 原因：{_safe_text(result['error'])}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ffmpeg", default=None)
    parser.add_argument(
        "--output",
        default=str(ROOT / "docs" / "superpowers" / "reports" / "2026-10-04-cast-simulation-report.md"),
    )
    args = parser.parse_args(argv)
    if os.environ.get("CAST_REAL_SOURCES") != "1":
        print("CAST_REAL_SOURCES is not enabled; set it to 1 to run real source diagnostics")
        return 2
    ffmpeg = find_ffmpeg(args.ffmpeg)
    results = [run_real_source_case(source, ffmpeg) for source in discover_project_real_sources()]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_render_report(results), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, sort_keys=True))
    print(f"report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
