"""Frame splitting and framing detection."""

from __future__ import annotations

import struct

from decoder.capture import HalfStream
from decoder.catalog import Catalog, Message
from decoder.framing import (
    Framing,
    RawFrame,
    detect_framing,
    iter_frames,
    rank_framings,
)

_OPCODE = 0x32E4591F

# 2-byte LE length counting the whole frame, then a plaintext 4-byte LE opcode.
_PLAINTEXT_OPCODE_FRAMING = Framing(
    length_size=2,
    length_endian="little",
    length_includes_header=True,
    opcode_size=4,
    opcode_endian="little",
)


def _catalog() -> Catalog:
    message = Message(name="S2C_Test", dir="S2C", id=_OPCODE, id_hex=f"{_OPCODE:#x}")
    return Catalog(by_opcode={_OPCODE: message})


def _frame(body: bytes) -> bytes:
    return struct.pack("<HI", len(body) + 6, _OPCODE) + body


def _stream(data: bytes) -> HalfStream:
    return HalfStream("10.0.0.1", 17701, "10.0.0.2", 55000, data)


def test_iter_frames_splits_and_stops_at_truncated_frame() -> None:
    data = _frame(b"ab") + _frame(b"") + _frame(b"xyz")[:-1]

    frames = iter_frames(data, _PLAINTEXT_OPCODE_FRAMING)

    assert frames == [RawFrame(0, _OPCODE, b"ab"), RawFrame(8, _OPCODE, b"")]


def test_iter_frames_without_opcode_keeps_it_in_body() -> None:
    framing = Framing(
        length_size=2,
        length_endian="little",
        length_includes_header=True,
        opcode_size=0,
        opcode_endian="little",
    )

    frames = iter_frames(_frame(b"ab"), framing)

    assert frames == [RawFrame(0, -1, struct.pack("<I", _OPCODE) + b"ab")]


def test_rank_framings_puts_real_framing_first() -> None:
    stream = _stream(_frame(b"ab") + _frame(b"cdef") + _frame(b""))

    candidates = rank_framings(stream, _catalog())

    assert candidates[0].framing == _PLAINTEXT_OPCODE_FRAMING
    assert candidates[0].known == 3
    assert candidates[0].consumed == len(stream.data)
    assert all(candidate.known > 0 for candidate in candidates)


def test_detect_framing_without_known_opcodes() -> None:
    best, candidates = detect_framing([_stream(b"\xff" * 32)], _catalog())

    assert best is None
    assert candidates == []
