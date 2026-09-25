"""Wire framing: split a reassembled byte stream into frames, and detect it.

The framing in use is a single :class:`Framing` value loaded from
``decoder.toml`` (``[framing]``). :func:`detect_framing` brute-forces a set of
candidate framings (length-field size/endianness, whether the length includes
the header, opcode endianness) against a capture, scoring each by how many
frames it splits the stream into whose opcode is a real catalog message, so a
protocol change shows up instead of silently producing garbage.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import NamedTuple

from .capture import HalfStream
from .catalog import Catalog


@dataclass(frozen=True)
class Framing:
    """How a message is laid out on the wire.

    ``length`` counts either the whole frame (header included) or just the body
    that follows the header, per ``length_includes_header``. The header is the
    length field followed immediately by the opcode field.

    ``opcode_size = 0`` means there is no plaintext opcode on the wire — the body
    (opcode included) is opaque, e.g. encrypted. Frames still split on length;
    the opcode is read from the body once it is decrypted upstream.
    """

    length_size: int
    length_endian: str
    length_includes_header: bool
    opcode_size: int
    opcode_endian: str

    @property
    def header_size(self) -> int:
        return self.length_size + self.opcode_size


class RawFrame(NamedTuple):
    """One length-delimited frame, before decryption or decoding.

    ``opcode`` is ``-1`` when the framing carries no plaintext opcode.
    """

    offset: int
    opcode: int
    body: bytes


def _read_length(data: bytes, offset: int, framing: Framing) -> int:
    raw = data[offset : offset + framing.length_size]
    return int.from_bytes(raw, framing.length_endian)


def _read_opcode(data: bytes, offset: int, framing: Framing) -> int:
    if framing.opcode_size == 0:
        return -1  # no plaintext opcode on the wire
    start = offset + framing.length_size
    raw = data[start : start + framing.opcode_size]
    return int.from_bytes(raw, framing.opcode_endian)


def iter_frames(data: bytes, framing: Framing) -> list[RawFrame]:
    """Split ``data`` into its frames, in stream order.

    Stops cleanly at the first truncated/implausible header rather than raising,
    so a partial capture still yields every complete frame that precedes it.
    """

    frames: list[RawFrame] = []
    offset = 0
    total = len(data)
    while offset + framing.header_size <= total:
        length = _read_length(data, offset, framing)
        opcode = _read_opcode(data, offset, framing)
        if framing.length_includes_header:
            body_len = length - framing.header_size
        else:
            body_len = length
        if body_len < 0:
            break
        body_start = offset + framing.header_size
        body_end = body_start + body_len
        if body_end > total:
            break  # truncated final frame
        frames.append(RawFrame(offset, opcode, data[body_start:body_end]))
        offset = body_end
    return frames


@dataclass
class Candidate:
    framing: Framing
    frames: int
    known: int  # frames whose opcode is in the catalog
    consumed: int  # bytes covered by parsed frames

    @property
    def score(self) -> tuple[int, int]:
        # Prefer the framing that recognises the most opcodes, then covers most.
        return (self.known, self.consumed)


def _candidate_framings() -> list[Framing]:
    framings: list[Framing] = []
    for length_size, endian, includes in itertools.product(
        (2, 4), ("little", "big"), (False, True)
    ):
        for opcode_endian in ("little", "big"):
            framings.append(
                Framing(
                    length_size=length_size,
                    length_endian=endian,
                    length_includes_header=includes,
                    opcode_size=4,
                    opcode_endian=opcode_endian,
                )
            )
    return framings


def _evaluate(stream: HalfStream, catalog: Catalog, framing: Framing) -> Candidate:
    frames = iter_frames(stream.data, framing)
    known = sum(1 for frame in frames if catalog.lookup(frame.opcode))
    consumed = sum(framing.header_size + len(frame.body) for frame in frames)
    return Candidate(
        framing=framing, frames=len(frames), known=known, consumed=consumed
    )


def rank_framings(stream: HalfStream, catalog: Catalog) -> list[Candidate]:
    """Candidate framings that recognise at least one opcode in ``stream``, best first."""

    candidates = [
        _evaluate(stream, catalog, framing) for framing in _candidate_framings()
    ]
    candidates = [c for c in candidates if c.known > 0]
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def detect_framing(
    streams: list[HalfStream], catalog: Catalog
) -> tuple[Framing | None, list[Candidate]]:
    """Brute-force the framing across all streams; return the best and the ranked list.

    Encrypted payloads only expose the plaintext handshake opcode, so ranking
    falls back to byte coverage — the true length framing is the one that splits
    a stream with no leftover bytes.
    """

    candidates = [
        candidate for stream in streams for candidate in rank_framings(stream, catalog)
    ]
    candidates.sort(key=lambda c: c.score, reverse=True)
    best = candidates[0].framing if candidates else None
    return best, candidates
