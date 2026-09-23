#!/usr/bin/env python3
"""Project the typed tables into the minimal, server-facing gamedata set.

`extract_tables.py` produces full, human-readable tables keyed by (mostly) friendly
field names. This tool is the next stage: it selects only the fields the game
server needs, renames them to server conventions, and writes one JSON file per
entity plus a `version.json` stamp into the game-server repo.

Why a separate stage:
  * The server never reads the raw/obfuscated client tables, only this output.
  * The server schema is decoupled from client field names — when an obfuscated
    name is resolved in `table_names.toml`, only `gamedata.toml` changes.
  * Output is deterministic (stable order, no timestamps) so committed diffs are
    meaningful — the main signal that an index guess was wrong.

Config-driven like its siblings — edit `gamedata.toml` and run it. No CLI.

Usage (run from the client dir as a module, like its siblings):
    python3 -m exporters.export_gamedata    # reads exporters/gamedata.toml
"""

from __future__ import annotations

import collections
import json
import sys
import tomllib
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("gamedata.toml")


def load_config(path: Path) -> dict:
    if not path.is_file():
        sys.exit(f"config not found: {path}")
    with path.open("rb") as handle:
        return tomllib.load(handle)


def resolve_path(base: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else (base / candidate).resolve()


def extract(row: object, path: str) -> object:
    """Follow a dot-path into a nested row, returning None if any step is absent."""
    current = row
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def project_entity(entity: dict, tables_dir: Path) -> tuple[list[dict], dict]:
    """Project one source table into server-facing rows plus a report entry.

    Fails fast on a missing source, a missing key value, or duplicate keys — all
    of which mean the projection or an upstream mapping is wrong.
    """
    name = entity["name"]
    source = entity["source"]
    key = entity.get("key", "id")
    fields: dict[str, str] = entity["fields"]
    todo: dict[str, str] = entity.get("todo", {})
    filt = entity.get("filter")

    source_path = tables_dir / f"{source}.json"
    if not source_path.is_file():
        sys.exit(f"[{name}] source table not found: {source_path}")
    rows = json.loads(source_path.read_text(encoding="utf-8"))

    keep = set(filt["keep"]) if filt else None
    filter_field = filt["field"] if filt else None

    out: list[dict] = []
    seen_keys: set[object] = set()
    missing_field_counts: collections.Counter[str] = collections.Counter()

    for row in rows:
        if keep is not None and row.get(filter_field) not in keep:
            continue
        projected: dict[str, object] = {}
        for dest, source_field in fields.items():
            value = extract(row, source_field)
            if value is None:
                missing_field_counts[dest] += 1
            projected[dest] = value

        key_value = projected.get(key)
        if key_value is None:
            sys.exit(f"[{name}] row missing key field '{key}': {row!r:.200}")
        if key_value in seen_keys:
            sys.exit(f"[{name}] duplicate key {key}={key_value!r}")
        seen_keys.add(key_value)
        out.append(projected)

    report = {
        "source": source,
        "rows": len(out),
        "fields": list(fields.keys()),
        "todo": list(todo.keys()),
        "missing_field_values": dict(missing_field_counts),
    }
    return out, report


def write_json(path: Path, rows: list[dict]) -> None:
    path.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    config = load_config(CONFIG_PATH)
    base_dir = CONFIG_PATH.parent
    tables_dir = resolve_path(base_dir, config["paths"]["tables"])
    output_dir = resolve_path(base_dir, config["paths"]["output"])
    meta = config.get("meta", {})
    entities = config.get("entity", [])
    if not entities:
        sys.exit("no [[entity]] sections in config")

    output_dir.mkdir(parents=True, exist_ok=True)
    reports: dict[str, dict] = {}

    for entity in entities:
        rows, report = project_entity(entity, tables_dir)
        write_json(output_dir / f"{entity['name']}.json", rows)
        reports[entity["name"]] = report
        missing = report["missing_field_values"]
        note = f"  (missing values: {missing})" if missing else ""
        todo = f"  todo: {report['todo']}" if report["todo"] else ""
        print(f"[+] {entity['name']:<8} {report['rows']:>5} rows -> {entity['name']}.json{note}{todo}")

    # No timestamp: keep output deterministic so committed diffs stay meaningful.
    version = {
        "client_release": meta.get("client_release", "unknown"),
        "schema_version": meta.get("schema_version", 0),
        "entities": reports,
    }
    (output_dir / "version.json").write_text(
        json.dumps(version, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"[+] wrote {len(reports)} entities + version.json to {output_dir}")


if __name__ == "__main__":
    main()
