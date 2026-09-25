"""Level-2 decoding: inherited base fields, nested structs, fixed arrays, enums."""

from __future__ import annotations

import struct

from decoder.body import decode_body
from decoder.catalog import Message, MessageField
from decoder.structs import StructCatalog

_LIST = "System.Collections.Generic.List<Protocol.Inner>"


def _catalog() -> StructCatalog:
    return StructCatalog(
        own_fields={
            "Network.IBSerializableList": [("__state__", "System.Byte")],
            "Protocol.Inner": [
                ("a", "System.Int32"),
                ("arr", "System.Int32[]"),
                ("kind", "Protocol.Kind"),
            ],
        },
        parents={"Protocol.T": "Network.IBSerializableList"},
        enums={"Protocol.Kind": "System.Int32"},
        message_fullnames=set(),
        fixed_arrays={("Protocol.Inner", "arr"): 2},
    )


def _msg() -> Message:
    return Message(
        name="T",
        dir="S2C",
        id=1,
        id_hex="0x1",
        fields=[
            MessageField("m_inner", "Protocol.Inner"),
            MessageField("m_list", _LIST),
        ],
    )


def _inner(a: int, arr: tuple[int, int], kind: int) -> bytes:
    return struct.pack("<i", a) + struct.pack("<ii", *arr) + struct.pack("<i", kind)


def test_decodes_base_prefix_nested_struct_array_enum_and_list() -> None:
    body = (
        struct.pack("<B", 7)  # inherited __state__
        + _inner(10, (1, 2), 3)  # m_inner
        + struct.pack("<I", 2)  # m_list count
        + _inner(20, (4, 5), 0)
        + _inner(30, (6, 7), 1)
    )
    fields, error = decode_body(_msg(), body, _catalog())
    assert error is None
    by_name = {f.name: f.value for f in fields}
    assert by_name["__state__"] == 7
    assert by_name["m_inner"] == {"a": 10, "arr": [1, 2], "kind": 3}
    assert by_name["m_list"] == [
        {"a": 20, "arr": [4, 5], "kind": 0},
        {"a": 30, "arr": [6, 7], "kind": 1},
    ]


def test_unsized_array_is_unresolved() -> None:
    catalog = _catalog()
    message = Message(
        name="U",
        dir="S2C",
        id=2,
        id_hex="0x2",
        fields=[MessageField("m_vals", "System.Int32[]")],
    )
    fields, error = decode_body(message, b"\x00" * 8, catalog)
    assert error is not None and "System.Int32[]" in error
    assert fields[0].unresolved
