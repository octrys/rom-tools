"""extract_protocol: metadata v31 parsing, opcode decoding, and catalog checks."""

from __future__ import annotations

import struct

import pytest

from exporters.extract_protocol import (
    METADATA_MAGIC,
    METADATA_VERSION,
    SECTIONS,
    TYPE_DEF_FMT,
    TYPE_DEF_SIZE,
    Metadata,
    MetadataError,
    ProtocolError,
    check_nested_types,
    check_unique_opcodes,
    describe_field_drift,
    extract_messages,
    merge_types,
    referenced_types,
)

_HEADER_SIZE = 8 + len(SECTIONS) * 8


def _zigzag(value: int) -> int:
    return ((value << 1) ^ (value >> 31)) & 0xFFFFFFFF


def _compressed_uint(value: int) -> bytes:
    if value < 0x80:
        return bytes([value])
    if value < 0x4000:
        return bytes([0x80 | (value >> 8), value & 0xFF])
    if value < 0x20000000:
        return bytes(
            [
                0xC0 | (value >> 24),
                (value >> 16) & 0xFF,
                (value >> 8) & 0xFF,
                value & 0xFF,
            ]
        )
    return b"\xf0" + struct.pack("<I", value)


def _build_metadata(
    classes: list[tuple[str, str, list[tuple[str, int | None]]]],
    magic: int = METADATA_MAGIC,
    version: int = METADATA_VERSION,
) -> bytes:
    """A minimal v31 file: (namespace, name, [(field, opcode-or-None)]) per class."""
    strings = bytearray()
    string_index: dict[str, int] = {}

    def intern(text: str) -> int:
        if text not in string_index:
            string_index[text] = len(strings)
            strings.extend(text.encode() + b"\x00")
        return string_index[text]

    fields = bytearray()
    defaults = bytearray()
    blob = bytearray()
    type_defs = bytearray()
    field_index = 0
    for namespace, name, class_fields in classes:
        values = list(struct.unpack(TYPE_DEF_FMT, bytes(TYPE_DEF_SIZE)))
        values[0], values[1] = intern(name), intern(namespace)
        values[8], values[18] = field_index, len(class_fields)
        type_defs.extend(struct.pack(TYPE_DEF_FMT, *values))
        for field_name, opcode in class_fields:
            fields.extend(struct.pack("<iiI", intern(field_name), 0, 0))
            if opcode is not None:
                defaults.extend(struct.pack("<iii", field_index, 0, len(blob)))
                blob.extend(_compressed_uint(_zigzag(opcode)))
            field_index += 1

    contents = {
        "string": bytes(strings),
        "fieldDefaultValues": bytes(defaults),
        "fieldAndParameterDefaultValueData": bytes(blob),
        "fields": bytes(fields),
        "typeDefinitions": bytes(type_defs),
    }
    header = [magic, version]
    body = bytearray()
    for section in SECTIONS:
        content = contents.get(section, b"")
        header += [_HEADER_SIZE + len(body), len(content)]
        body.extend(content)
    return struct.pack("<" + "I" * len(header), *header) + bytes(body)


def test_extracts_messages_with_opcodes_and_ordered_fields() -> None:
    data = _build_metadata(
        [
            (
                "Protocol",
                "C2S_Login",
                [("m_user", None), ("__ID__", 0x12345678), ("m_pass", None)],
            ),
            ("Protocol", "S2C_Kick", [("__ID__", -2)]),
            ("Protocol", "Helper", [("m_value", None)]),
        ]
    )

    messages = extract_messages(Metadata(data))

    assert [(m["name"], m["dir"], m["id"], m["fields"]) for m in messages] == [
        ("C2S_Login", "C2S", 0x12345678, ["m_user", "m_pass"]),
        ("S2C_Kick", "S2C", 0xFFFFFFFE, []),
    ]


def test_rejects_bad_magic() -> None:
    data = _build_metadata([], magic=0xDEADBEEF)
    with pytest.raises(MetadataError, match="bad magic 0xDEADBEEF"):
        Metadata(data)


def test_rejects_unsupported_version() -> None:
    data = _build_metadata([], version=29)
    with pytest.raises(MetadataError, match="unsupported metadata version 29"):
        Metadata(data)


def test_rejects_truncated_file() -> None:
    with pytest.raises(MetadataError, match="too small"):
        Metadata(struct.pack("<II", METADATA_MAGIC, METADATA_VERSION))


@pytest.mark.parametrize(
    ("encoded", "expected"),
    [
        (b"\x7f", 0x7F),
        (b"\x81\x02", 0x102),
        (b"\xc1\x02\x03\x04", 0x01020304),
        (b"\xf0\x78\x56\x34\x12", 0x12345678),
        (b"\xfe", 0xFFFFFFFE),
        (b"\xff", 0xFFFFFFFF),
    ],
)
def test_reads_compressed_uint(encoded: bytes, expected: int) -> None:
    meta = Metadata(_build_metadata([]) + encoded)
    offset = len(meta.data) - len(encoded)
    assert meta.read_compressed_uint(offset) == expected


@pytest.mark.parametrize("lead", [0xE0, 0xEF, 0xF1, 0xFD])
def test_rejects_invalid_compressed_uint_lead(lead: int) -> None:
    meta = Metadata(_build_metadata([]) + bytes([lead, 0, 0, 0, 0]))
    with pytest.raises(MetadataError, match=f"lead byte 0x{lead:02X}"):
        meta.read_compressed_uint(len(meta.data) - 5)


def _message(
    name: str, opcode: int, ns: str = "Protocol", fields: list[str] | None = None
) -> dict:
    return {
        "name": name,
        "ns": ns,
        "dir": name[:3],
        "id": opcode,
        "id_hex": f"0x{opcode:08X}",
        "fields": fields or [],
    }


def test_unique_opcodes_pass() -> None:
    check_unique_opcodes([_message("C2S_A", 1), _message("S2C_B", 2)])


def test_duplicate_opcodes_fail_naming_both_messages() -> None:
    messages = [_message("C2S_A", 7), _message("S2C_B", 7), _message("C2S_C", 8)]
    with pytest.raises(ProtocolError, match="0x00000007: C2S_A, S2C_B"):
        check_unique_opcodes(messages)


def _dump_entry(full: str, fields: list[tuple[str, str, bool]]) -> dict:
    return {
        "full": full,
        "name": full.rsplit(".", 1)[-1],
        "fields": [
            {"name": name, "type": field_type, "isStatic": is_static}
            for name, field_type, is_static in fields
        ],
    }


def test_merge_matches_by_namespace_qualified_name() -> None:
    messages = [_message("C2S_Move", 1, fields=["m_x"])]
    dump_index = {
        "Other.C2S_Move": _dump_entry(
            "Other.C2S_Move", [("m_x", "System.String", False)]
        ),
        "Protocol.C2S_Move": _dump_entry(
            "Protocol.C2S_Move",
            [("__ID__", "System.Int32", True), ("m_x", "System.Single", False)],
        ),
    }

    typed, stats = merge_types(messages, dump_index)

    assert typed[0]["fields"] == [{"name": "m_x", "type": "System.Single"}]
    assert stats == {"matched": 1, "no_type_source": 0, "field_mismatch": 0}


def test_merge_rejects_non_int32_opcode() -> None:
    messages = [_message("C2S_Move", 1)]
    dump_index = {
        "Protocol.C2S_Move": _dump_entry(
            "Protocol.C2S_Move", [("__ID__", "System.UInt32", True)]
        ),
    }
    with pytest.raises(ProtocolError, match=r"C2S_Move.__ID__ is System.UInt32"):
        merge_types(messages, dump_index)


def test_type_field_names_lists_every_field_in_order() -> None:
    data = _build_metadata(
        [
            ("Protocol", "C2S_Login", [("__ID__", 5), ("m_user", None)]),
            ("", "Global", [("m_value", None)]),
        ]
    )

    assert Metadata(data).type_field_names() == {
        "Protocol.C2S_Login": ["__ID__", "m_user"],
        "Global": ["m_value"],
    }


@pytest.mark.parametrize(
    ("field_type", "expected"),
    [
        ("System.Int32", []),
        ("Protocol.ItemInfo", ["Protocol.ItemInfo"]),
        ("Protocol.ItemInfo[]", ["Protocol.ItemInfo"]),
        ("System.Collections.Generic.List<Protocol.ItemInfo>", ["Protocol.ItemInfo"]),
        (
            "System.Collections.Generic.Dictionary<System.Int32,Protocol.ItemInfo>",
            ["Protocol.ItemInfo"],
        ),
        ("?", []),
    ],
)
def test_referenced_types(field_type: str, expected: list[str]) -> None:
    assert referenced_types(field_type) == expected


@pytest.mark.parametrize(
    ("meta_fields", "dump_fields", "expected"),
    [
        (["m_a", "m_b"], ["m_a", "m_b"], None),
        (["m_b", "m_a"], ["m_a", "m_b"], "same fields in a different order"),
        (
            ["m_a", "m_new"],
            ["m_a", "m_old"],
            "only in metadata: ['m_new']; only in dump: ['m_old']",
        ),
        (["m_a", "m_new"], ["m_a"], "only in metadata: ['m_new']; only in dump: -"),
    ],
)
def test_describe_field_drift(
    meta_fields: list[str], dump_fields: list[str], expected: str | None
) -> None:
    dump_index = {
        "Protocol.X": _dump_entry(
            "Protocol.X", [(name, "System.Int32", False) for name in dump_fields]
        )
    }
    assert (
        describe_field_drift("Protocol.X", {"Protocol.X": meta_fields}, dump_index)
        == expected
    )


def test_describe_field_drift_reports_missing_type() -> None:
    dump_index = {"Protocol.X": _dump_entry("Protocol.X", [])}
    assert describe_field_drift("Protocol.X", {}, dump_index) == "not in metadata"
    assert describe_field_drift("Protocol.Y", {"Protocol.Y": []}, dump_index) == (
        "not in runtime dump"
    )


def _nested_fixture() -> tuple[list[dict], dict[str, dict]]:
    """C2S_Compose -> List<Protocol.Input> -> Protocol.Leaf, plus an untouched message."""
    typed = [
        {
            "name": "C2S_Compose",
            "fields": [
                {
                    "name": "m_list",
                    "type": "System.Collections.Generic.List<Protocol.Input>",
                }
            ],
        },
        {"name": "C2S_Ping", "fields": [{"name": "m_time", "type": "System.Int64"}]},
    ]
    dump_index = {
        "Protocol.Input": _dump_entry(
            "Protocol.Input",
            [("m_index", "System.Int32", False), ("m_leaf", "Protocol.Leaf", False)],
        ),
        "Protocol.Leaf": _dump_entry(
            "Protocol.Leaf", [("m_count", "System.Int32", False)]
        ),
    }
    return typed, dump_index


def test_nested_types_matching_metadata_add_no_notes() -> None:
    typed, dump_index = _nested_fixture()
    meta_fields = {
        "Protocol.Input": ["m_index", "m_leaf"],
        "Protocol.Leaf": ["m_count"],
    }

    checked = check_nested_types(typed, dump_index, meta_fields)

    assert checked == {"Protocol.Input": None, "Protocol.Leaf": None}
    assert all("_note" not in entry for entry in typed)


def test_transitive_nested_drift_annotates_the_message() -> None:
    typed, dump_index = _nested_fixture()
    meta_fields = {
        "Protocol.Input": ["m_index", "m_leaf"],
        "Protocol.Leaf": ["m_count", "m_bonus"],
    }

    checked = check_nested_types(typed, dump_index, meta_fields)

    assert checked["Protocol.Leaf"] == "only in metadata: ['m_bonus']; only in dump: -"
    assert typed[0]["_note"] == "nested type drift: Protocol.Leaf"
    assert "_note" not in typed[1]


def test_nested_drift_note_appends_to_existing_note() -> None:
    typed, dump_index = _nested_fixture()
    typed[0]["_note"] = "some fields not found in dump (schema drift)"

    check_nested_types(typed, dump_index, {"Protocol.Input": ["m_index", "m_leaf"]})

    assert typed[0]["_note"] == (
        "some fields not found in dump (schema drift); nested type drift: Protocol.Leaf"
    )
