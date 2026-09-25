"""Analysis cache: hits only while the capture and decoder version are unchanged."""

from __future__ import annotations

from pathlib import Path

import pytest

from decoder.cache import AnalysisCache


@pytest.fixture
def capture(tmp_path: Path) -> Path:
    path = tmp_path / "session.pcap"
    path.write_bytes(b"pcap bytes")
    return path


def test_store_then_load_round_trips(tmp_path: Path, capture: Path) -> None:
    cache = AnalysisCache(tmp_path / "cache", version=1)
    signature = cache.signature(capture)

    cache.store(capture, {"sessions": []}, signature)

    assert cache.is_fresh(capture)
    assert cache.load(capture, signature) == {"sessions": []}


def test_missing_entry_is_not_fresh(tmp_path: Path, capture: Path) -> None:
    cache = AnalysisCache(tmp_path / "cache", version=1)

    assert not cache.is_fresh(capture)
    assert cache.load(capture, cache.signature(capture)) is None


def test_version_bump_invalidates(tmp_path: Path, capture: Path) -> None:
    old = AnalysisCache(tmp_path / "cache", version=1)
    old.store(capture, {"sessions": []}, old.signature(capture))

    new = AnalysisCache(tmp_path / "cache", version=2)

    assert not new.is_fresh(capture)
    assert new.load(capture, new.signature(capture)) is None


def test_edited_capture_invalidates(tmp_path: Path, capture: Path) -> None:
    cache = AnalysisCache(tmp_path / "cache", version=1)
    cache.store(capture, {"sessions": []}, cache.signature(capture))

    capture.write_bytes(b"different, longer pcap bytes")

    assert not cache.is_fresh(capture)


def test_unreadable_analysis_is_a_miss(tmp_path: Path, capture: Path) -> None:
    cache_dir = tmp_path / "cache"
    cache = AnalysisCache(cache_dir, version=1)
    signature = cache.signature(capture)
    cache.store(capture, {"sessions": []}, signature)

    (cache_dir / "session.pcap.json").write_text("{not json", encoding="utf-8")

    assert cache.load(capture, signature) is None
