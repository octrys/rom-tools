"""CLI: reverse the wire framing of a capture against the protocol catalog.

Given a pcap, this prints each reassembled half-stream's head bytes and the
best-scoring candidate framings (see :func:`decoder.framing.rank_framings`).
The top candidate is almost certainly the real framing — plug its values into
``[framing]`` in ``decoder.toml``.

    python3 -m decoder.analyze pcap/session.pcap
    python3 -m decoder.analyze pcap/session.pcap --head 128
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .capture import read_half_streams
from .catalog import Catalog
from .config import load_config
from .framing import rank_framings

logger = logging.getLogger(__name__)


def _hexdump(data: bytes, length: int) -> str:
    head = data[:length]
    lines: list[str] = []
    for i in range(0, len(head), 16):
        chunk = head[i : i + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"  {i:04x}  {hex_part:<47}  {ascii_part}")
    return "\n".join(lines)


def analyze(path: Path, head: int, catalog_path: Path) -> None:
    catalog = Catalog.load(catalog_path)
    streams = read_half_streams(path)
    if not streams:
        print("no TCP payload streams found in capture")
        return

    for stream in streams:
        print(
            f"\n=== {stream.label}  ({len(stream.data)} bytes, "
            f"{stream.segments} segments) ==="
        )
        print(_hexdump(stream.data, head))
        candidates = rank_framings(stream, catalog)
        if not candidates:
            print(
                "  no candidate framing recognised any opcode "
                "(payload may be encrypted/compressed)"
            )
            continue
        print("  best framings (by recognised opcodes):")
        for candidate in candidates[:3]:
            f = candidate.framing
            print(
                f"    len={f.length_size}B/{f.length_endian} "
                f"incl_header={f.length_includes_header} "
                f"op={f.opcode_endian}  ->  "
                f"{candidate.frames} frames, {candidate.known} known, "
                f"{candidate.consumed}/{len(stream.data)} bytes"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pcap", type=Path, help="capture file to analyze")
    parser.add_argument(
        "--head", type=int, default=64, help="bytes to hexdump per stream (default 64)"
    )
    parser.add_argument(
        "--config", type=Path, default=None, help="path to decoder.toml"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.pcap.is_file():
        parser.error(f"no such capture: {args.pcap}")
    config = load_config(args.config)
    analyze(args.pcap, args.head, config.catalog_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
