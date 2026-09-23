#!/usr/bin/env python3
"""Turn the runtime table dump into typed, named, human-readable JSON.

The shipped bundle can only be decoded losslessly offline (raw bytes): its
on-wire field order is bespoke and some values are resolved only at runtime
(localized names, `.unity`-suffixed scene paths). This tool instead consumes the
**runtime** dump produced by rom-frida's `dump_tables.js` (`tables_runtime.json`)
— every table's rows, fully parsed and resolved, keyed by the il2cpp-obfuscated
field names with enum values as raw integers — and makes it readable:

1. **Enum resolution** — every enum value becomes its (un-obfuscated) member
   name, e.g. `2 -> "MT_FIELD"`, using the type model parsed from `rom_dump.cs`.
2. **Field naming** — obfuscated field names are renamed via `table_names.toml`
   (curated, per table + shared nested structs). Unmapped fields keep their
   obfuscated name so nothing is ever lost; a coverage report shows progress.

Config-driven like its siblings — edit `extract_tables.toml` and run it. No CLI.

Usage (run from the client dir as a module so `libs` resolves):
    python3 -m exporters.extract_tables    # reads exporters/extract_tables.toml
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

from libs import Schema, parse_dump

CONFIG_PATH = Path(__file__).with_name("extract_tables.toml")

PRIMITIVES = {
    "System.Int32",
    "System.UInt32",
    "System.Int64",
    "System.UInt64",
    "System.Int16",
    "System.UInt16",
    "System.Byte",
    "System.SByte",
    "System.Single",
    "System.Double",
    "System.Boolean",
    "System.String",
    "System.Char",
}


def load_config(path: Path) -> dict:
    if not path.is_file():
        sys.exit(f"config not found: {path}")
    with path.open("rb") as handle:
        return tomllib.load(handle)


def resolve_path(base: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else (base / candidate).resolve()


def list_element_type(type_name: str) -> str | None:
    """Element type of a `List<...>`/array, else None."""
    if type_name.startswith("System.Collections.Generic.List<") and type_name.endswith(">"):
        return type_name[len("System.Collections.Generic.List<") : -1].strip()
    if type_name.endswith("[]"):
        return type_name[:-2].strip()
    return None


class Namer:
    """Maps obfuscated field names to friendly ones, per table and per shared
    nested struct type. Falls back to the obfuscated name so output is always
    complete."""

    def __init__(self, mapping: dict):
        # mapping["global"] = {obf: friendly}                  (consistent across the codebase)
        # mapping["types"][<TypeShortName>] = {obf: friendly}  (shared structs)
        # mapping["tables"][<table>] = {obf: friendly}
        # mapping["enums"] = {<EnumShortName>: friendly}       (name any field of this enum type)
        self.global_names: dict[str, str] = mapping.get("global", {})
        self.tables: dict[str, dict[str, str]] = mapping.get("tables", {})
        self.types: dict[str, dict[str, str]] = mapping.get("types", {})
        self.enums: dict[str, str] = mapping.get("enums", {})

    def lookup(self, table: str, owner_type_short: str, obf: str) -> str | None:
        # Most specific wins: per-table, then shared struct, then the global
        # (obfuscation is consistent, so one name can map an identifier everywhere).
        friendly = self.tables.get(table, {}).get(obf)
        if friendly is None:
            friendly = self.types.get(owner_type_short, {}).get(obf)
        if friendly is None:
            friendly = self.global_names.get(obf)
        return friendly

    def enum_name(self, enum_type_short: str) -> str | None:
        # Enum *members* are not obfuscated, so an enum type is self-describing;
        # naming it once names every field of that type across all tables.
        return self.enums.get(enum_type_short)


class Converter:
    def __init__(self, schema: Schema, namer: Namer):
        self.schema = schema
        self.namer = namer

    def resolve_value(self, value: object, type_name: str, table: str) -> object:
        if value is None:
            return None
        if type_name in self.schema.enums:
            if isinstance(value, int):
                return self.schema.enums[type_name].get(value, value)
            return value
        element = list_element_type(type_name)
        if element is not None and isinstance(value, list):
            return [self.resolve_value(item, element, table) for item in value]
        if type_name in self.schema.types and type_name not in PRIMITIVES and isinstance(value, dict):
            return self.resolve_struct(value, type_name, table)
        return value

    def resolve_struct(self, value: object, type_name: str, table: str) -> object:
        if not isinstance(value, dict):
            return value
        info = self.schema.types[type_name]
        short = self.schema.short(type_name)
        out: dict[str, object] = {}
        for fname, ftype in info.fields:
            if fname not in value:
                continue
            friendly = self.namer.lookup(table, short, fname)
            if friendly is None and ftype in self.schema.enums:
                friendly = self.namer.enum_name(self.schema.short(ftype))
            # Fall back to the obfuscated name, and never clobber a sibling that
            # already claimed the same friendly name (e.g. two fields of one enum).
            if friendly is None or friendly in out:
                friendly = fname
            out[friendly] = self.resolve_value(value[fname], ftype, table)
        # Preserve any keys the schema did not mention (defensive).
        for extra in value:
            if extra not in {f[0] for f in info.fields}:
                out.setdefault(extra, value[extra])
        return out

    def convert_row(self, row: object, row_type: str, table: str) -> dict:
        resolved = self.resolve_struct(row, row_type, table)
        return resolved if isinstance(resolved, dict) else {"value": resolved}

    def collect_fields(self, row_type: str, table: str) -> list[tuple[str, str, bool]]:
        """Distinct (owner_short, obf, is_named) for a table's schema, recursing
        nested structs/lists once each. Drives the coverage report."""
        out: list[tuple[str, str, bool]] = []
        seen: set[tuple[str, str]] = set()

        def walk(type_name: str, stack: frozenset[str]) -> None:
            element = list_element_type(type_name)
            if element is not None:
                walk(element, stack)
                return
            if type_name in PRIMITIVES or type_name in self.schema.enums:
                return
            info = self.schema.types.get(type_name)
            if not info or type_name in stack:
                return
            short = self.schema.short(type_name)
            for fname, ftype in info.fields:
                key = (short, fname)
                if key not in seen:
                    seen.add(key)
                    is_named = self.namer.lookup(table, short, fname) is not None
                    if not is_named and ftype in self.schema.enums:
                        is_named = self.namer.enum_name(self.schema.short(ftype)) is not None
                    out.append((short, fname, is_named))
                walk(ftype, stack | {type_name})

        walk(row_type, frozenset())
        return out


def main() -> None:
    config = load_config(CONFIG_PATH)
    base_dir = CONFIG_PATH.parent
    dump_cs = resolve_path(base_dir, config["paths"]["dump_cs"])
    runtime = resolve_path(base_dir, config["paths"]["runtime"])
    output_dir = resolve_path(base_dir, config["paths"]["output"])
    names_path = resolve_path(base_dir, config["paths"].get("names", "table_names.toml"))

    for required in (dump_cs, runtime):
        if not required.is_file():
            sys.exit(f"input not found: {required}")

    print(f"[+] parsing schema from {dump_cs.name} ...")
    schema = parse_dump(dump_cs)
    print(f"[+] enums={len(schema.enums)} types={len(schema.types)} tables={len(schema.table_rows)}")

    mapping = {}
    if names_path.is_file():
        with names_path.open("rb") as handle:
            mapping = tomllib.load(handle)
    namer = Namer(mapping)
    converter = Converter(schema, namer)

    with runtime.open(encoding="utf-8") as handle:
        dump = json.load(handle)

    output_dir.mkdir(parents=True, exist_ok=True)
    missing_schema: list[str] = []
    per_table: list[dict] = []
    named_total = 0
    field_total = 0

    for table_name, table in sorted(dump.items()):
        row_type = schema.table_rows.get(table_name)
        if not row_type or row_type not in schema.types:
            missing_schema.append(table_name)
            continue
        rows_out = [
            {
                "key": entry.get("key"),
                **converter.convert_row(entry.get("row") or {}, row_type, table_name),
            }
            for entry in table.get("rows", [])
        ]
        (output_dir / f"{table_name}.json").write_text(
            json.dumps(rows_out, indent=2, ensure_ascii=False) + "\n"
        )

        fields = converter.collect_fields(row_type, table_name)
        named = sum(1 for _, _, is_named in fields if is_named)
        total = len(fields)
        named_total += named
        field_total += total
        per_table.append(
            {
                "table": table_name,
                "rows": len(rows_out),
                "named": named,
                "total": total,
                "obfuscated": [f"{s}.{o}" for s, o, is_named in fields if not is_named],
            }
        )

    pct = (named_total / field_total * 100) if field_total else 0
    print(f"[+] wrote {len(per_table)} tables to {output_dir}")
    print(f"[+] field-name coverage (distinct schema fields): {named_total}/{field_total} ({pct:.1f}%)")
    if missing_schema:
        print(f"[!] no schema for {len(missing_schema)} tables: {', '.join(missing_schema[:10])}"
              + (" ..." if len(missing_schema) > 10 else ""))

    report = output_dir / "_coverage.json"
    report.write_text(
        json.dumps(
            {
                "named_fields": named_total,
                "total_fields": field_total,
                "percent": round(pct, 1),
                "per_table": sorted(per_table, key=lambda r: r["total"] - r["named"], reverse=True),
                "missing_schema": missing_schema,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    print(f"[+] coverage report -> {report}")


if __name__ == "__main__":
    main()
