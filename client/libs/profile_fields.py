"""Profile every obfuscated table field across all tables, to drive naming.

Because the il2cpp obfuscation is *consistent* (one original name maps to one
obfuscated string everywhere), the real unit of naming work is the set of
distinct `(owner struct, obfuscated field)` pairs — not the 122 tables. This
helper makes a single pass over the schema (`rom_dump.cs`) and the runtime dump
(`tables_runtime.json`) and, for each distinct field, reports:

- its declared type (and, for enums, the resolved member names it takes),
- how many tables use it (frequency) and how many rows were sampled,
- a handful of distinct sample values (enums shown as member names),
- whether `table_names.toml` already names it, and to what.

Unnamed fields are ranked by impact (tables desc, then rows) so naming can be
driven top-down: the highest-frequency fields cover the most schema occurrences.

Dev helper, run as a module from the client dir:
    python3 -m libs.profile_fields                # ranked table to stdout
    python3 -m libs.profile_fields --all          # include already-named fields
    python3 -m libs.profile_fields --limit 40     # top N unnamed (default 60)

Also writes the full profile as JSON next to the typed tables
(`<output>/_fields.json`) for programmatic use.
"""

from __future__ import annotations

import json
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .table_schema import Schema, parse_dump

CONFIG_PATH = Path(__file__).resolve().parents[1] / "type_tables.toml"

MAX_SAMPLES = 8
MAX_ROWS_PER_TABLE = 400  # enough for value variety without scanning millions
MAX_DEPTH = 12

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


@dataclass
class FieldProfile:
    owner_short: str
    obf: str
    type_name: str = ""
    is_enum: bool = False
    tables: set[str] = field(default_factory=set)
    rows: int = 0
    samples: list[str] = field(default_factory=list)
    named_as: str | None = None

    def add_sample(self, rendered: str) -> None:
        self.rows += 1
        if len(self.samples) < MAX_SAMPLES and rendered not in self.samples:
            self.samples.append(rendered)


def list_element_type(type_name: str) -> str | None:
    """Element type of a `List<...>`/array, else None."""
    prefix = "System.Collections.Generic.List<"
    if type_name.startswith(prefix) and type_name.endswith(">"):
        return type_name[len(prefix) : -1].strip()
    if type_name.endswith("[]"):
        return type_name[:-2].strip()
    return None


class Profiler:
    def __init__(self, schema: Schema, names: dict):
        self.schema = schema
        self.global_names: dict[str, str] = names.get("global", {})
        self.type_names: dict[str, dict[str, str]] = names.get("types", {})
        self.table_names: dict[str, dict[str, str]] = names.get("tables", {})
        self.enum_names: dict[str, str] = names.get("enums", {})
        self.fields: dict[tuple[str, str], FieldProfile] = {}

    def _named_as(self, table: str, owner_short: str, obf: str, type_name: str) -> str | None:
        explicit = (
            self.table_names.get(table, {}).get(obf)
            or self.type_names.get(owner_short, {}).get(obf)
            or self.global_names.get(obf)
        )
        if explicit is not None:
            return explicit
        if type_name in self.schema.enums:
            return self.enum_names.get(self.schema.short(type_name))
        return None

    def _entry(self, owner_short: str, obf: str, type_name: str, table: str) -> FieldProfile:
        key = (owner_short, obf)
        entry = self.fields.get(key)
        if entry is None:
            entry = FieldProfile(owner_short=owner_short, obf=obf)
            self.fields[key] = entry
        entry.type_name = type_name
        entry.is_enum = type_name in self.schema.enums
        entry.tables.add(table)
        entry.named_as = self._named_as(table, owner_short, obf, type_name)
        return entry

    def _render(self, value: object, type_name: str) -> str:
        if type_name in self.schema.enums and isinstance(value, int):
            return str(self.schema.enums[type_name].get(value, value))
        if isinstance(value, dict):
            return "{…}"
        if isinstance(value, list):
            return f"[{len(value)} items]"
        return str(value)[:48]

    def walk(self, value: object, type_name: str, table: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            return
        element = list_element_type(type_name)
        if element is not None:
            if isinstance(value, list):
                for item in value:
                    self.walk(item, element, table, depth + 1)
            return
        info = self.schema.types.get(type_name)
        if info is None or type_name in PRIMITIVES or type_name in self.schema.enums:
            return
        if not isinstance(value, dict):
            return
        owner_short = self.schema.short(type_name)
        for fname, ftype in info.fields:
            if fname not in value:
                continue
            entry = self._entry(owner_short, fname, ftype, table)
            entry.add_sample(self._render(value[fname], ftype))
            self.walk(value[fname], ftype, table, depth + 1)

    def run(self, dump: dict) -> None:
        for table_name, table in dump.items():
            row_type = self.schema.table_rows.get(table_name)
            if not row_type or row_type not in self.schema.types:
                continue
            for entry in table.get("rows", [])[:MAX_ROWS_PER_TABLE]:
                row = entry.get("row")
                if isinstance(row, dict):
                    self.walk(row, row_type, table_name, 0)


def load_paths() -> tuple[Path, Path, Path, Path]:
    if not CONFIG_PATH.is_file():
        sys.exit(f"config not found: {CONFIG_PATH}")
    with CONFIG_PATH.open("rb") as handle:
        config = tomllib.load(handle)
    base = CONFIG_PATH.parent
    paths = config["paths"]

    def resolve(value: str) -> Path:
        candidate = Path(value).expanduser()
        return candidate if candidate.is_absolute() else (base / candidate).resolve()

    return (
        resolve(paths["dump_cs"]),
        resolve(paths["runtime"]),
        resolve(paths.get("names", "table_names.toml")),
        resolve(paths["output"]),
    )


def parse_args(argv: list[str]) -> tuple[bool, int]:
    show_all = "--all" in argv
    limit = 60
    if "--limit" in argv:
        idx = argv.index("--limit")
        if idx + 1 >= len(argv):
            sys.exit("--limit needs a number")
        limit = int(argv[idx + 1])
    return show_all, limit


def main() -> None:
    show_all, limit = parse_args(sys.argv[1:])
    dump_cs, runtime, names_path, output_dir = load_paths()
    for required in (dump_cs, runtime):
        if not required.is_file():
            sys.exit(f"input not found: {required}")

    schema = parse_dump(dump_cs)
    names: dict = {}
    if names_path.is_file():
        with names_path.open("rb") as handle:
            names = tomllib.load(handle)

    with runtime.open(encoding="utf-8") as handle:
        dump = json.load(handle)

    profiler = Profiler(schema, names)
    profiler.run(dump)

    entries = list(profiler.fields.values())
    named = [e for e in entries if e.named_as]
    unnamed = [e for e in entries if not e.named_as]
    unnamed.sort(key=lambda e: (len(e.tables), e.rows), reverse=True)

    print(f"# {len(entries)} distinct fields  "
          f"named={len(named)}  unnamed={len(unnamed)}  "
          f"({len(named) / len(entries) * 100:.1f}% named)" if entries else "# no fields")

    shown = (named + unnamed) if show_all else unnamed
    if not show_all:
        shown = unnamed[:limit]
        print(f"# top {len(shown)} unnamed by impact (tables, then rows) — --all for everything\n")
    header = f"{'owner.field':30} {'kind':10} {'tbls':>4} {'rows':>6}  samples"
    print(header)
    print("-" * len(header))
    for entry in shown:
        kind = f"enum" if entry.is_enum else schema.short(entry.type_name) or "?"
        element = list_element_type(entry.type_name)
        if element is not None:
            kind = f"[{schema.short(element)}]"
        tag = f"  = {entry.named_as}" if entry.named_as else ""
        label = f"{entry.owner_short}.{entry.obf}"
        samples = ", ".join(entry.samples[:5])
        print(f"{label:30} {kind:10} {len(entry.tables):>4} {entry.rows:>6}  {samples}{tag}")

    output_dir.mkdir(parents=True, exist_ok=True)
    report = output_dir / "_fields.json"
    report.write_text(
        json.dumps(
            [
                {
                    "owner": entry.owner_short,
                    "obf": entry.obf,
                    "type": entry.type_name,
                    "is_enum": entry.is_enum,
                    "tables": sorted(entry.tables),
                    "table_count": len(entry.tables),
                    "rows_sampled": entry.rows,
                    "samples": entry.samples,
                    "named_as": entry.named_as,
                }
                for entry in sorted(
                    entries, key=lambda e: (e.named_as is not None, -len(e.tables), -e.rows)
                )
            ],
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    print(f"\n[+] full profile -> {report}")


if __name__ == "__main__":
    main()
