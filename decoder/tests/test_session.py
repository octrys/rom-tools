"""End-to-end session decrypt round-trip, without any captured (leaked) data.

Synthesises an encrypted session with the same primitives the server uses, then
asserts ``decode_sessions`` pairs the directions, reads the plaintext handshake,
derives the key and decrypts every frame back to its field values.
"""

from __future__ import annotations

import struct

import pytest

from decoder.capture import HalfStream
from decoder.catalog import Catalog, Message, MessageField
from decoder.crypto import Rabbit, derive_key
from decoder.framing import Framing
from decoder.session import decode_sessions

_STATIC_KEY = b"Sxf89J&1*,0Axm>t"

# The confirmed wire framing: 2-byte LE length counting the header, opcode
# inside the encrypted body.
_FRAMING = Framing(
    length_size=2,
    length_endian="little",
    length_includes_header=True,
    opcode_size=0,
    opcode_endian="little",
)

_HANDSHAKE_FIELDS = [
    "m_socketUID",
    "m_connectionKey",
    "m_clientSendIV",
    "m_serverSendIV",
    "m_encryptionKeyLow",
    "m_encryptionKeyHigh",
]


def _catalog() -> Catalog:
    handshake = Message(
        name="S2C_CheckConnection",
        dir="S2C",
        id=0x32E4591F,
        id_hex="0x32e4591f",
        fields=[MessageField(n, "System.Int64") for n in _HANDSHAKE_FIELDS],
    )
    s2c_ping = Message(
        name="S2C_Ping",
        dir="S2C",
        id=0x11111111,
        id_hex="0x11111111",
        fields=[MessageField("m_value", "System.Int32")],
    )
    c2s_ping = Message(
        name="C2S_Ping",
        dir="C2S",
        id=0x22222222,
        id_hex="0x22222222",
        fields=[MessageField("m_token", "System.Int64")],
    )
    return Catalog(by_opcode={m.id: m for m in (handshake, s2c_ping, c2s_ping)})


def _frame(body: bytes) -> bytes:
    return struct.pack("<H", len(body) + 2) + body


def test_decode_sessions_round_trip() -> None:
    client_send_iv = 0x0102030405060708
    server_send_iv = 0x1112131415161718
    enc_low = 0xAABBCCDDEEFF0011
    enc_high = 0x2233445566778899

    raw_key = enc_low.to_bytes(8, "little") + enc_high.to_bytes(8, "little")
    derived = derive_key(raw_key, _STATIC_KEY)
    s2c = Rabbit(derived, server_send_iv.to_bytes(8, "little"))
    c2s = Rabbit(derived, client_send_iv.to_bytes(8, "little"))

    handshake_values = [12345, 67890, client_send_iv, server_send_iv, enc_low, enc_high]
    handshake_body = struct.pack("<I", 0x32E4591F) + b"".join(
        v.to_bytes(8, "little") for v in handshake_values
    )
    s2c_ping_body = struct.pack("<Ii", 0x11111111, -42)
    c2s_ping_body = struct.pack("<Iq", 0x22222222, 0x7FFFFFFFFFFF)

    s2c_data = _frame(handshake_body) + _frame(s2c.crypt(s2c_ping_body))
    c2s_data = _frame(c2s.crypt(c2s_ping_body))

    server = ("10.0.0.1", 17701)
    client = ("10.0.0.2", 55000)
    streams = [
        HalfStream(server[0], server[1], client[0], client[1], s2c_data),
        HalfStream(client[0], client[1], server[0], server[1], c2s_data),
    ]

    sessions = decode_sessions(streams, _catalog(), _STATIC_KEY, _FRAMING)
    assert len(sessions) == 1
    session = sessions[0]
    assert session.error is None
    assert session.handshake["m_clientSendIV"] == client_send_iv

    by_name = {f.name: f for f in session.frames}
    assert by_name["S2C_Ping"].fields[0].value == -42
    assert by_name["C2S_Ping"].fields[0].value == 0x7FFFFFFFFFFF
    assert not by_name["S2C_Ping"].error
    assert not by_name["C2S_Ping"].error


def test_decode_sessions_rejects_plaintext_opcode_framing() -> None:
    framing = Framing(
        length_size=2,
        length_endian="little",
        length_includes_header=True,
        opcode_size=4,
        opcode_endian="little",
    )
    with pytest.raises(ValueError, match="opcode_size = 4"):
        decode_sessions([], _catalog(), _STATIC_KEY, framing)


def test_decode_sessions_reports_truncated_handshake() -> None:
    # Handshake opcode followed by only one of its six u64 fields.
    handshake_body = struct.pack("<Iq", 0x32E4591F, 12345)
    streams = [
        HalfStream("10.0.0.1", 17701, "10.0.0.2", 55000, _frame(handshake_body)),
    ]

    sessions = decode_sessions(streams, _catalog(), _STATIC_KEY, _FRAMING)

    assert len(sessions) == 1
    assert sessions[0].error is not None
    assert sessions[0].error.startswith("handshake not decodable: body too short")
    assert sessions[0].frames == []
