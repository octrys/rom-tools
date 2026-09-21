"""Parse the il2cpp dump (`rom_dump.cs`) into a schema model for the tables.

The runtime table dump (`tables_runtime.json`, produced by rom-frida's
`dump_tables.js`) gives every table's rows fully parsed, but keyed by the
il2cpp-obfuscated field names, with enum values left as raw integers. To make
those rows readable we need the type model behind them, which lives in the
dump:

- **enums** — `enum <Full> : System.Enum { static <Full> MEMBER = N; ... }`.
  Enum *member* names are NOT obfuscated (e.g. `MT_FIELD`), so resolving an
  enum value to its member both decodes the value and hints at the field.
- **structs / classes** — the row types and their nested value types; each
  instance field is `    <Type> <name>; // 0x<offset>`.
- **table → row type** — a table class carries `static String KFDPDJHNGHC =
  "<TableName>"` and extends `KGCGOOOMCCB<TKey, TRow>`; `TRow` is the row type.

This module is pure parsing (no naming, no I/O beyond reading the dump) so the
converter and any future tools can share one schema model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# A type header at column 0: `class|struct|enum <Name> : <bases>`.
_TYPE_HEADER = re.compile(r"^(class|struct|enum)\s+([\w.<>`]+)")
# An instance field line: `    <Type> <name>; // 0x...` (no `(`, not static/literal).
_FIELD = re.compile(r"^\s{4}([\w.<>`,\[\] ]+?)\s+([\w<>]+);\s*//\s*0x[0-9a-fA-F]+\s*$")
# An enum member: `    static <EnumType> <MEMBER> = <N>;`.
_ENUM_MEMBER = re.compile(r"^\s{4}static\s+[\w.<>`]+\s+([\w]+)\s*=\s*(-?\d+);")
# The table-name literal and the KGCGOOOMCCB<Key,Row> base.
_TABLE_NAME = re.compile(r'KFDPDJHNGHC\s*=\s*"([^"]+)"')
_TABLE_BASE = re.compile(r"KGCGOOOMCCB<\s*([^,]+?)\s*,\s*(.+?)\s*>\s*$")


@dataclass
class TypeInfo:
    """A parsed class/struct: its instance fields in declaration order."""

    full: str
    kind: str  # "class" | "struct"
    fields: list[tuple[str, str]] = field(default_factory=list)  # (name, type)


@dataclass
class Schema:
    enums: dict[str, dict[int, str]]  # full enum name -> {value: member}
    types: dict[str, TypeInfo]  # full type name -> TypeInfo
    table_rows: dict[str, str]  # table name -> row type full name

    def short(self, full: str) -> str:
        return full.rsplit(".", 1)[-1]


def _strip_system(type_name: str) -> str:
    return type_name.strip()


def parse_dump(cs_path: Path) -> Schema:
    enums: dict[str, dict[int, str]] = {}
    types: dict[str, TypeInfo] = {}
    table_rows: dict[str, str] = {}

    current_kind: str | None = None
    current_name: str | None = None
    pending_table_name: str | None = None
    pending_row_type: str | None = None

    with cs_path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            header = _TYPE_HEADER.match(line)
            if header:
                # Close any table mapping accumulated for the previous type.
                if pending_table_name and pending_row_type:
                    table_rows[pending_table_name] = pending_row_type
                pending_table_name = None
                pending_row_type = None

                current_kind = header.group(1)
                current_name = header.group(2)
                if current_kind in ("class", "struct"):
                    # A generic base on the header tells us this is a table class.
                    base = _TABLE_BASE.search(line)
                    if base:
                        pending_row_type = base.group(2).strip()
                    types.setdefault(current_name, TypeInfo(current_name, current_kind))
                elif current_kind == "enum":
                    enums.setdefault(current_name, {})
                continue

            if current_kind == "enum" and current_name:
                member = _ENUM_MEMBER.match(line)
                if member:
                    enums[current_name][int(member.group(2))] = member.group(1)
                continue

            if current_kind in ("class", "struct") and current_name:
                name_lit = _TABLE_NAME.search(line)
                if name_lit:
                    pending_table_name = name_lit.group(1)
                field_match = _FIELD.match(line)
                if field_match and "static" not in line and " = " not in line:
                    ftype = _strip_system(field_match.group(1))
                    fname = field_match.group(2)
                    types[current_name].fields.append((fname, ftype))

        if pending_table_name and pending_row_type:
            table_rows[pending_table_name] = pending_row_type

    return Schema(enums=enums, types=types, table_rows=table_rows)
