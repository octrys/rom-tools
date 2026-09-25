"""Field decoding: primitives plus the self-describing types (level 1)."""

from __future__ import annotations

import struct

from decoder.body import decode_body
from decoder.catalog import Message, MessageField

_LIST_I32 = "System.Collections.Generic.List<System.Int32>"
_LIST_STR = "System.Collections.Generic.List<System.String>"


def _msg(*fields: tuple[str, str]) -> Message:
    return Message(
        name="T",
        dir="C2S",
        id=1,
        id_hex="0x1",
        fields=[MessageField(n, t) for n, t in fields],
    )


def _string(value: str) -> bytes:
    units = value.encode("utf-16-le")
    return struct.pack("<I", len(units) // 2) + units


def test_decodes_string_and_trailing_primitive() -> None:
    message = _msg(
        ("m_id", "System.Int64"),
        ("m_name", "System.String"),
        ("m_ok", "System.Boolean"),
    )
    body = struct.pack("<q", 42) + _string("Åland") + struct.pack("<?", True)
    fields, error = decode_body(message, body)
    assert error is None
    assert [(f.name, f.value) for f in fields] == [
        ("m_id", 42),
        ("m_name", "Åland"),
        ("m_ok", True),
    ]


def test_decodes_list_of_primitive() -> None:
    message = _msg(("m_values", _LIST_I32))
    body = struct.pack("<I", 3) + struct.pack("<iii", 1, -2, 3)
    fields, error = decode_body(message, body)
    assert error is None
    assert fields[0].value == [1, -2, 3]


def test_decodes_list_of_strings() -> None:
    message = _msg(("m_names", _LIST_STR))
    body = struct.pack("<I", 2) + _string("ab") + _string("c")
    fields, error = decode_body(message, body)
    assert error is None
    assert fields[0].value == ["ab", "c"]


def test_stops_on_unresolved_type() -> None:
    message = _msg(("m_id", "System.Int32"), ("m_struct", "Protocol.SomeStruct"))
    body = struct.pack("<i", 7) + b"\x00\x00\x00\x00"
    fields, error = decode_body(message, body)
    assert error is not None and "Protocol.SomeStruct" in error
    assert fields[0].value == 7
    assert fields[1].unresolved


def test_reports_short_body() -> None:
    message = _msg(("m_a", "System.Int32"), ("m_b", "System.Int32"))
    fields, error = decode_body(message, struct.pack("<i", 1))
    assert error is not None and "too short" in error
    assert [f.name for f in fields] == ["m_a"]
