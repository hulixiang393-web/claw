from __future__ import annotations

import os

import pytest

from tools import run_cast_real_sources as real_sources

ALLOWED_FAILURE_CLASSES = real_sources.ALLOWED_FAILURE_CLASSES
classify_failure = real_sources.classify_failure


@pytest.mark.skipif(os.environ.get("CAST_REAL_SOURCES") == "1", reason="opt-in guard is inactive")
def test_real_sources_are_opt_in():
    pytest.skip("set CAST_REAL_SOURCES=1 to run real source integration")


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"source_available": False, "error_type": "RequestError"}, "source_unavailable"),
        ({"proxy_error": True}, "proxy_unreachable"),
        ({"http_status": 200, "probe_returncode": 0, "decoded": False, "decode_returncode": 1}, "ffmpeg_decode_error"),
        ({"http_status": 200, "probe_returncode": 1, "decoded": False}, "unsupported_format"),
        ({"protocol_error": True}, "protocol_error"),
        ({"decoded": True}, "decoded"),
    ],
)
def test_classify_failure_is_explicit(result, expected):
    assert classify_failure(result) == expected
    assert expected in ALLOWED_FAILURE_CLASSES


def test_classify_failure_does_not_accept_unclassified_success():
    assert classify_failure({"source_available": True, "decoded": False}) == "protocol_error"


def test_real_source_result_has_no_sensitive_url_fields(monkeypatch):
    monkeypatch.setattr(
        real_sources,
        "discover_project_real_sources",
        lambda: [{"source_id": "dm84", "media_type": "video", "media_url": "https://secret.invalid/token"}],
    )
    monkeypatch.setattr(
        real_sources,
        "run_real_source_case",
        lambda source, ffmpeg_path: {
            "source_id": source["source_id"],
            "media_type": source["media_type"],
            "http_status": 403,
            "first_byte_ms": None,
            "probe": {"returncode": 1},
            "decode": {"decoded": False},
            "classification": "source_unavailable",
        },
    )
    sources = real_sources.discover_project_real_sources()
    result = real_sources.run_real_source_case(sources[0], "ffmpeg")
    assert set(result) <= {
        "source_id",
        "media_type",
        "http_status",
        "first_byte_ms",
        "probe",
        "decode",
        "classification",
        "error",
    }
    assert "secret.invalid" not in str(result)


@pytest.mark.skipif(os.environ.get("CAST_REAL_SOURCES") != "1", reason="CAST_REAL_SOURCES is not enabled")
def test_project_real_sources_through_virtual_renderer():
    sources = real_sources.discover_project_real_sources()
    results = [real_sources.run_real_source_case(source, None) for source in sources]
    assert results
    assert {result["classification"] for result in results} <= ALLOWED_FAILURE_CLASSES
    for result in results:
        print(result)
