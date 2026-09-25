"""Decrypt captured sessions and decode their frames against the catalog.

A session is one TCP connection: two half-streams (one per direction). The
server opens with a *plaintext* ``S2C_CheckConnection`` frame carrying the
per-session IVs and the encryption-key halves; from those we derive the Rabbit
body key and decrypt the rest of the capture passively — no client needed.

Keystream rules mirror the reference server:
- the S2C handshake frame is plaintext and consumes no keystream, so the
  server-send cipher starts at the second S2C frame;
- every client-send frame is encrypted from the first;
- each frame advances its direction's keystream to the next 16-byte boundary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .body import DecodedField, decode_body
from .capture import HalfStream
from .catalog import Catalog
from .crypto import Rabbit, session_ciphers
from .framing import Framing, RawFrame, iter_frames
from .structs import StructCatalog

logger = logging.getLogger(__name__)

HANDSHAKE_MESSAGE = "S2C_CheckConnection"

# Every decrypted body starts with its u32 LE opcode.
_OPCODE_SIZE = 4

# The handshake fields are declared Int64, but they carry raw key material and
# IVs, so they are read as unsigned 64-bit values.
_U64_MASK = 0xFFFF_FFFF_FFFF_FFFF

# Handshake fields the session ciphers are built from.
_HANDSHAKE_KEY_FIELDS = (
    "m_encryptionKeyLow",
    "m_encryptionKeyHigh",
    "m_clientSendIV",
    "m_serverSendIV",
)


@dataclass
class DecodedFrame:
    offset: int
    direction: str  # "C2S" or "S2C"
    plaintext: bool
    opcode: int
    name: str
    body: bytes  # decrypted, opcode included
    time: float | None = None  # capture timestamp (epoch seconds)
    fields: list[DecodedField] = field(default_factory=list)
    error: str | None = None


@dataclass
class Session:
    label: str
    handshake: dict[str, int] = field(default_factory=dict)
    frames: list[DecodedFrame] = field(default_factory=list)
    error: str | None = None


def _endpoint_key(stream: HalfStream) -> frozenset[tuple[str, int]]:
    return frozenset(
        {(stream.src_ip, stream.src_port), (stream.dst_ip, stream.dst_port)}
    )


def _read_opcode(plain: bytes) -> int:
    return int.from_bytes(plain[:_OPCODE_SIZE], "little")


@dataclass(frozen=True)
class _FrameDecoder:
    """Turns a decrypted frame body into a ``DecodedFrame`` via the catalogs."""

    catalog: Catalog
    structs: StructCatalog | None

    def decode(
        self,
        stream: HalfStream,
        raw: RawFrame,
        plain: bytes,
        direction: str,
        plaintext: bool,
    ) -> DecodedFrame:
        opcode = _read_opcode(plain)
        message = self.catalog.lookup(opcode)
        frame = DecodedFrame(
            offset=raw.offset,
            direction=direction,
            plaintext=plaintext,
            opcode=opcode,
            name=message.name if message else f"UNKNOWN_{opcode:#010x}",
            body=plain,
            time=stream.time_at(raw.offset),
        )
        if message is None:
            frame.error = "opcode not in catalog"
        else:
            frame.fields, frame.error = decode_body(
                message, plain[_OPCODE_SIZE:], self.structs
            )
        return frame


def decode_sessions(
    streams: list[HalfStream],
    catalog: Catalog,
    static_key: bytes,
    framing: Framing,
    structs: StructCatalog | None = None,
) -> list[Session]:
    """Pair half-streams into sessions, decrypt them, and decode every frame.

    ``framing`` splits each half-stream into frames. Its opcode must live inside
    the encrypted body (``opcode_size = 0``), since it is only readable after
    decryption.
    """

    if framing.opcode_size != 0:
        raise ValueError(
            f"session decoding needs an in-body opcode (opcode_size = 0), "
            f"got opcode_size = {framing.opcode_size}"
        )

    groups: dict[frozenset[tuple[str, int]], list[HalfStream]] = {}
    for stream in streams:
        groups.setdefault(_endpoint_key(stream), []).append(stream)

    decoder = _FrameDecoder(catalog, structs)
    return [
        _decode_one(members, decoder, static_key, framing)
        for members in groups.values()
    ]


def _find_handshake(
    framed: list[tuple[HalfStream, list[RawFrame]]], catalog: Catalog
) -> tuple[HalfStream, list[RawFrame]] | None:
    """The S2C half-stream: the one whose first frame is the plaintext handshake."""

    for stream, frames in framed:
        if not frames:
            continue
        message = catalog.lookup(_read_opcode(frames[0].body))
        if message is not None and message.name == HANDSHAKE_MESSAGE:
            return stream, frames
    return None


def _handshake_values(frame: DecodedFrame) -> dict[str, int]:
    return {
        fld.name: fld.value & _U64_MASK
        for fld in frame.fields
        if isinstance(fld.value, int)
    }


def _decode_direction(
    decoder: _FrameDecoder,
    stream: HalfStream,
    frames: list[RawFrame],
    cipher: Rabbit,
    direction: str,
) -> list[DecodedFrame]:
    """Decrypt and decode consecutive encrypted frames of one direction."""

    return [
        decoder.decode(stream, raw, cipher.crypt(raw.body), direction, plaintext=False)
        for raw in frames
    ]


def _chronological(frame: DecodedFrame) -> tuple[float, str, int]:
    # Frames without a timestamp (shouldn't happen) sort last, by direction and
    # offset.
    time = frame.time if frame.time is not None else float("inf")
    return (time, frame.direction, frame.offset)


def _decode_one(
    members: list[HalfStream],
    decoder: _FrameDecoder,
    static_key: bytes,
    framing: Framing,
) -> Session:
    framed = [(stream, iter_frames(stream.data, framing)) for stream in members]
    label = " <-> ".join(sorted(stream.label for stream, _ in framed))

    found = _find_handshake(framed, decoder.catalog)
    if found is None:
        return Session(label=label, error="no plaintext handshake frame found")
    s2c_stream, s2c_frames = found

    handshake_raw = s2c_frames[0]
    handshake_frame = decoder.decode(
        s2c_stream, handshake_raw, handshake_raw.body, "S2C", plaintext=True
    )
    handshake = _handshake_values(handshake_frame)
    missing = [name for name in _HANDSHAKE_KEY_FIELDS if name not in handshake]
    if handshake_frame.error or missing:
        reason = handshake_frame.error or f"missing fields {', '.join(missing)}"
        return Session(
            label=label,
            handshake=handshake,
            error=f"handshake not decodable: {reason}",
        )

    c2s_cipher, s2c_cipher = session_ciphers(
        static_key,
        key_low=handshake["m_encryptionKeyLow"],
        key_high=handshake["m_encryptionKeyHigh"],
        client_send_iv=handshake["m_clientSendIV"],
        server_send_iv=handshake["m_serverSendIV"],
    )

    # The handshake consumes no keystream: S2C decryption starts at frame 1,
    # while every C2S frame is encrypted from the first.
    frames = [
        handshake_frame,
        *_decode_direction(decoder, s2c_stream, s2c_frames[1:], s2c_cipher, "S2C"),
    ]
    for stream, stream_frames in framed:
        if stream is not s2c_stream:
            frames.extend(
                _decode_direction(decoder, stream, stream_frames, c2s_cipher, "C2S")
            )

    frames.sort(key=_chronological)
    return Session(label=label, handshake=handshake, frames=frames)
