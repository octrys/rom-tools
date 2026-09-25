"""Persistent per-capture analysis cache.

Each capture's analysis is stored as a JSON file under the cache directory,
next to a small signature file: the source capture's size + mtime plus the
analysis version. A cache hit is only honoured when the signature still
matches, so re-dropping or editing a pcap, or changing the decoder, re-analyzes
it. Keeping the signature separate lets freshness checks skip the analysis body.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class AnalysisCache:
    """Analyses keyed by capture file name, invalidated by signature."""

    def __init__(self, cache_dir: Path, version: int) -> None:
        self._cache_dir = cache_dir
        self._version = version

    def signature(self, capture: Path) -> dict[str, int]:
        """A cheap change-detector for the capture and the decoder producing it."""

        stat = capture.stat()
        return {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "version": self._version,
        }

    def is_fresh(self, capture: Path) -> bool:
        """Whether a current cached analysis exists, without loading its body."""

        return self._stored_signature(capture) == self.signature(capture)

    def load(self, capture: Path, signature: dict[str, int]) -> dict | None:
        """Return the cached analysis when its stored signature matches."""

        if self._stored_signature(capture) != signature:
            return None
        data = _read_json(self._analysis_file(capture))
        return data if isinstance(data, dict) else None

    def store(self, capture: Path, analysis: dict, signature: dict[str, int]) -> None:
        """Save ``analysis``, computed from the capture as it was at ``signature``."""

        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._analysis_file(capture).write_text(json.dumps(analysis), encoding="utf-8")
        # Written last: a signature only ever vouches for a complete analysis.
        self._signature_file(capture).write_text(
            json.dumps(signature), encoding="utf-8"
        )

    def _stored_signature(self, capture: Path) -> object:
        return _read_json(self._signature_file(capture))

    def _stem(self, capture: Path) -> str:
        # Flatten the name so a capture in a subdir cannot escape the cache dir.
        return capture.name.replace("/", "_").replace("\\", "_")

    def _analysis_file(self, capture: Path) -> Path:
        return self._cache_dir / f"{self._stem(capture)}.json"

    def _signature_file(self, capture: Path) -> Path:
        return self._cache_dir / f"{self._stem(capture)}.sig.json"


def _read_json(path: Path) -> object:
    """Parsed JSON at ``path``, or None when it is missing or unreadable."""

    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        logger.warning("ignoring unreadable cache file %s: %s", path, err)
        return None
