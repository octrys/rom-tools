"""Run a capture's full analysis: framing detection + decrypt + decode.

This is what the web app runs on first open of a pcap (and caches). Decoding
uses the confirmed framing from config; the brute-forced framing is recorded
alongside so a protocol change shows up as a mismatch instead of silently
producing garbage.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .capture import read_half_streams
from .catalog import Catalog
from .config import Config
from .framing import Framing, detect_framing
from .session import Session, decode_sessions
from .structs import StructCatalog

logger = logging.getLogger(__name__)

# Bump when the decoder changes what it produces, so cached analyses from an
# older decoder are treated as stale and re-run instead of served.
ANALYSIS_VERSION = 4


def _session_dict(session: Session) -> dict[str, object]:
    times = [f.time for f in session.frames if f.time is not None]
    t0 = min(times) if times else 0.0
    return {
        "label": session.label,
        "error": session.error,
        "handshake": {k: str(v) for k, v in session.handshake.items()},
        "frames": [
            {
                "offset": frame.offset,
                "direction": frame.direction,
                "plaintext": frame.plaintext,
                "t": round(frame.time - t0, 6) if frame.time is not None else None,
                "opcode_hex": f"{frame.opcode:#010x}",
                "name": frame.name,
                "error": frame.error,
                "body_len": len(frame.body),
                "body_hex": frame.body[:256].hex(),
                "fields": [
                    {
                        "name": fld.name,
                        "type": fld.type,
                        "value": fld.value,
                        "unresolved": fld.unresolved,
                    }
                    for fld in frame.fields
                ],
            }
            for frame in session.frames
        ],
    }


def _load_structs(
    type_dump_path: Path | None, catalog: Catalog
) -> StructCatalog | None:
    if type_dump_path is None:
        return None
    if not type_dump_path.is_file():
        logger.warning(
            "type dump not found: %s; decoding self-describing types only",
            type_dump_path,
        )
        return None
    message_names = {message.name for message in catalog.by_opcode.values()}
    return StructCatalog.load(type_dump_path, message_names)


@dataclass(frozen=True)
class Analyzer:
    """Everything a capture's analysis needs: catalogs, framing and crypto key."""

    catalog: Catalog
    framing: Framing
    static_key: bytes
    structs: StructCatalog | None = None

    @classmethod
    def from_config(cls, config: Config) -> Analyzer:
        catalog = Catalog.load(config.catalog_path)
        return cls(
            catalog=catalog,
            framing=config.framing,
            static_key=config.static_key,
            structs=_load_structs(config.type_dump_path, catalog),
        )

    def analyze(self, path: Path) -> dict[str, object]:
        """Analyze one capture into a JSON-serializable result."""

        streams = read_half_streams(path)
        detected, _candidates = detect_framing(streams, self.catalog)
        sessions = decode_sessions(
            streams, self.catalog, self.static_key, self.framing, self.structs
        )

        framing_dict = dataclasses.asdict(self.framing)
        detected_dict = dataclasses.asdict(detected) if detected else None
        framing_matches = detected_dict is None or all(
            detected_dict[key] == framing_dict[key]
            for key in ("length_size", "length_endian", "length_includes_header")
        )

        return {
            "name": path.name,
            "analyzed_at": int(time.time()),
            "framing": framing_dict,
            "detected_framing": detected_dict,
            "framing_matches": framing_matches,
            "sessions": [_session_dict(session) for session in sessions],
        }
