#!/usr/bin/env python3
"""Rebuild the ROM: Golden Age network-protocol catalog from client artifacts.

The protocol schema comes from two inputs, merged here:

  1. `global-metadata.dat` — the client's IL2CPP metadata, clean (unencrypted) on
     PC. Parsed directly (no il2cpp dumper, which the Themida-packed binary
     defeats), it yields every `Protocol.C2S_*` / `S2C_*` message class with its
     32-bit `__ID__` opcode and its ordered `m_*` field names. This is ~80% of the
     protocol — everything but the field *types*.

  2. `rom_dump.json` — a runtime dump from frida-il2cpp-bridge (`dump_bridge.js`).
     Field types are indexed through a table that lives inside the packed
     `GameAssembly.dll` and is decrypted only in memory, so they cannot be read
     statically from the metadata. The dump supplies `name:type` per field, which
     this tool merges onto the opcode catalog by class name.

Outputs (into the configured output dir):
  * `messages.json` / `messages.md`        — opcodes + ordered field names
  * `messages_typed.json` / `messages_typed.md` — the above + field types

Like the sibling `extract_tables.py`, this is config-driven (`extract_protocol.toml`),
not CLI. It is standard library only but needs `tomllib` (Python 3.11+).

Wire note: IL2CPP stores Int32 field defaults zigzag-encoded, so `__ID__` is
decoded `id = (u >> 1) ^ -(u & 1)`. A client update rotates every opcode (the body
serialization stays identical); re-point `[paths].metadata` at the new file and
re-run to remigrate.
"""
from __future__ import annotations

import json
import struct
import sys
import tomllib
from collections import Counter
from pathlib import Path

# IL2CPP global-metadata.dat section order after the 8-byte {magic, version} head.
# Each section is a {offset, size} u32 pair; we only read the ones we need.
SECTIONS = [
    "stringLiteral", "stringLiteralData", "string", "events", "properties",
    "methods", "parameterDefaultValues", "fieldDefaultValues",
    "fieldAndParameterDefaultValueData", "fieldMarshaledSizes", "parameters",
    "fields", "genericParameters", "genericParameterConstraints",
    "genericContainers", "nestedTypes", "interfaces", "vtableMethods",
    "interfaceOffsets", "typeDefinitions",
]
TYPE_DEF_SIZE = 88          # sizeof(Il2CppTypeDefinition) for metadata v31
FIELD_DEF_SIZE = 12         # sizeof(Il2CppFieldDefinition)
# Il2CppTypeDefinition layout: 7×i32, 1×u32 (fieldStart), 8×i32, 8×u16, 2×u32.
TYPE_DEF_FMT = "<" + "i" * 7 + "I" + "i" * 8 + "H" * 8 + "II"


def load_config(path: Path) -> dict:
    if not path.is_file():
        sys.exit(f"config not found: {path}")
    with path.open("rb") as handle:
        return tomllib.load(handle)


def resolve_path(base: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else (base / candidate).resolve()


# --- metadata parsing -----------------------------------------------------


class Metadata:
    """Minimal reader over a clean `global-metadata.dat` (v31)."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        header = struct.unpack_from("<" + "I" * (2 + len(SECTIONS) * 2), data, 0)
        self.sections: dict[str, tuple[int, int]] = {}
        cursor = 2  # skip magic + version
        for name in SECTIONS:
            self.sections[name] = (header[cursor], header[cursor + 1])
            cursor += 2
        self._string_offset = self.sections["string"][0]
        self._field_offset = self.sections["fields"][0]
        self._default_blob = self.sections["fieldAndParameterDefaultValueData"][0]
        self._field_defaults = self._index_field_defaults()

    def string(self, index: int) -> str:
        start = self._string_offset + index
        end = self.data.index(b"\x00", start)
        return self.data[start:end].decode("utf-8", "replace")

    def field(self, field_index: int) -> tuple[int, int, int]:
        """(nameIndex, typeIndex, token) for a field definition."""
        return struct.unpack_from("<iiI", self.data, self._field_offset + field_index * FIELD_DEF_SIZE)

    def _index_field_defaults(self) -> dict[int, int]:
        offset, size = self.sections["fieldDefaultValues"]
        defaults: dict[int, int] = {}
        for pos in range(offset, offset + size, 12):
            field_index, _type_index, data_index = struct.unpack_from("<iii", self.data, pos)
            defaults[field_index] = data_index
        return defaults

    def read_compressed_uint(self, base: int) -> int:
        """Decode IL2CPP's compressed-uint at an absolute offset."""
        data = self.data
        lead = data[base]
        if (lead & 0x80) == 0:
            return lead & 0x7F
        if (lead & 0xC0) == 0x80:
            return ((lead & 0x3F) << 8) | data[base + 1]
        if (lead & 0xE0) == 0xC0:
            return ((lead & 0x1F) << 24) | (data[base + 1] << 16) | (data[base + 2] << 8) | data[base + 3]
        if lead == 0xF0:
            return data[base + 1] | (data[base + 2] << 8) | (data[base + 3] << 16) | (data[base + 4] << 24)
        if lead == 0xFE:
            return 0xFFFFFFFE
        if lead == 0xFF:
            return 0xFFFFFFFF
        return lead

    def opcode_default(self, field_index: int) -> int | None:
        """The zigzag-decoded `__ID__` opcode for a field, if it has a default."""
        data_index = self._field_defaults.get(field_index)
        if data_index is None:
            return None
        raw = self.read_compressed_uint(self._default_blob + data_index) & 0xFFFFFFFF
        return ((raw >> 1) ^ -(raw & 1)) & 0xFFFFFFFF

    def type_definitions(self):
        offset, size = self.sections["typeDefinitions"]
        for index in range(size // TYPE_DEF_SIZE):
            yield struct.unpack_from(TYPE_DEF_FMT, self.data, offset + index * TYPE_DEF_SIZE)


def extract_messages(meta: Metadata) -> list[dict]:
    """Every message class carrying an `__ID__`, with opcode and ordered fields."""
    messages: list[dict] = []
    for type_def in meta.type_definitions():
        name = meta.string(type_def[0])
        namespace = meta.string(type_def[1])
        field_start, field_count = type_def[8], type_def[18]

        opcode: int | None = None
        fields: list[str] = []
        for offset in range(field_count):
            field_index = field_start + offset
            name_index, _type_index, _token = meta.field(field_index)
            field_name = meta.string(name_index)
            if field_name == "__ID__":
                opcode = meta.opcode_default(field_index)
                continue
            fields.append(field_name)

        if opcode is None:
            continue

        direction = (
            "C2S" if name.startswith("C2S_")
            else "S2C" if name.startswith("S2C_")
            else "?"
        )
        messages.append({
            "name": name,
            "ns": namespace,
            "dir": direction,
            "logical": name[4:] if direction in ("C2S", "S2C") else name,
            "id": opcode,
            "id_hex": f"0x{opcode:08X}",
            "fields": fields,
        })

    messages.sort(key=lambda message: (message["dir"], message["logical"]))
    return messages


# --- type merge -----------------------------------------------------------


def load_dump_index(dump_path: Path) -> dict[str, dict]:
    """Index the runtime dump by class name (bare and `Protocol.`-qualified)."""
    dump = json.loads(dump_path.read_text())
    index: dict[str, dict] = {}
    for entry in dump:
        index[entry["name"]] = entry
        index[entry.get("full", entry["name"])] = entry
    return index


def merge_types(messages: list[dict], dump_index: dict[str, dict]) -> tuple[list[dict], dict]:
    """Attach field types from the dump onto the opcode catalog."""
    typed: list[dict] = []
    stats = {"matched": 0, "no_type_source": 0, "field_mismatch": 0}
    for message in messages:
        source = dump_index.get(message["name"]) or dump_index.get("Protocol." + message["name"])
        entry = {key: message[key] for key in ("name", "dir", "id", "id_hex")}

        if not source:
            entry["fields"] = [{"name": field, "type": "?"} for field in message["fields"]]
            entry["_note"] = "no runtime type source (evolved/removed)"
            stats["no_type_source"] += 1
            typed.append(entry)
            continue

        types_by_name = {
            field["name"]: field["type"]
            for field in source["fields"]
            if not field.get("isStatic")
        }
        fields = []
        mismatch = False
        for field_name in message["fields"]:
            field_type = types_by_name.get(field_name)
            if field_type is None:
                mismatch = True
            fields.append({"name": field_name, "type": field_type or "?"})
        entry["fields"] = fields
        if mismatch:
            entry["_note"] = "some fields not found in dump (schema drift)"
            stats["field_mismatch"] += 1
        stats["matched"] += 1
        typed.append(entry)
    return typed, stats


# --- output ---------------------------------------------------------------


def write_messages(messages: list[dict], out_dir: Path) -> dict:
    (out_dir / "messages.json").write_text(
        json.dumps(messages, ensure_ascii=False, indent=1), encoding="utf-8")

    id_counts = Counter(message["id"] for message in messages)
    duplicates = {opcode: count for opcode, count in id_counts.items() if count > 1}

    lines = [f"# ROM Golden Age — Network Protocol ({len(messages)} messages)\n"]
    lines.append(f"Unique ids: {len(id_counts)} | duplicate ids: {len(duplicates)}\n")
    for direction in ("C2S", "S2C", "?"):
        group = [m for m in messages if m["dir"] == direction]
        if not group:
            continue
        lines.append(f"\n## {direction} ({len(group)})\n")
        for message in group:
            fields = ", ".join(message["fields"]) if message["fields"] else "(no fields)"
            lines.append(f"- **{message['id_hex']}** `{message['name']}` — {fields}")
    (out_dir / "messages.md").write_text("\n".join(lines), encoding="utf-8")
    return {"unique_ids": len(id_counts), "duplicate_ids": len(duplicates)}


def write_typed(typed: list[dict], out_dir: Path) -> None:
    (out_dir / "messages_typed.json").write_text(
        json.dumps(typed, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = ["# ROM Golden Age — Typed Protocol (opcodes + field types)\n"]
    for direction in ("C2S", "S2C"):
        group = [m for m in typed if m["dir"] == direction]
        lines.append(f"\n## {direction} ({len(group)})\n")
        for message in group:
            fields = ", ".join(
                f"{field['type']} {field['name']}" for field in message["fields"]
            ) or "(empty)"
            note = f"  <!-- {message['_note']} -->" if "_note" in message else ""
            lines.append(f"- **{message['id_hex']}** `{message['name']}`: {fields}{note}")
    (out_dir / "messages_typed.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    config_path = Path(__file__).resolve().parent / "extract_protocol.toml"
    config = load_config(config_path)
    base = config_path.parent
    paths = config.get("paths", {})

    metadata_path = resolve_path(base, paths.get("metadata", "./global-metadata.dat"))
    dump_value = paths.get("dump", "")
    out_dir = resolve_path(base, paths.get("output", "../resources/protocol"))
    if not metadata_path.is_file():
        sys.exit(f"metadata not found: {metadata_path}")
    out_dir.mkdir(parents=True, exist_ok=True)

    meta = Metadata(metadata_path.read_bytes())
    messages = extract_messages(meta)
    id_stats = write_messages(messages, out_dir)

    c2s = sum(1 for m in messages if m["dir"] == "C2S")
    s2c = sum(1 for m in messages if m["dir"] == "S2C")
    print(f"messages with __ID__: {len(messages)} (C2S={c2s} S2C={s2c})")
    print(f"unique ids={id_stats['unique_ids']} duplicate ids={id_stats['duplicate_ids']}")
    print(f"-> {out_dir}/messages.json, messages.md")

    if not dump_value:
        print("dump not configured; skipping type merge (untyped catalog only)")
        return

    dump_path = resolve_path(base, dump_value)
    if not dump_path.is_file():
        sys.exit(f"dump not found: {dump_path} (set [paths].dump = \"\" to skip typing)")
    typed, merge_stats = merge_types(messages, load_dump_index(dump_path))
    write_typed(typed, out_dir)
    print(f"type merge: {merge_stats}")
    print(f"-> {out_dir}/messages_typed.json, messages_typed.md")


if __name__ == "__main__":
    main()
