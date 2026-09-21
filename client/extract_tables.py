#!/usr/bin/env python3
"""Extract game data tables from a local mirror of the CDN patch files.

The ROM: Golden Age patch server serves the game's Unity asset bundles under
`real/patch/Windows/`. The game-data tables live in `tablecrypto.unity` as `C_*`
TextAssets (despite the name, the content is plaintext, not encrypted). Point
this tool at a local copy of that patch tree (e.g. mirrored by the patcher) and
it exports the tables as JSON for the server to consume. Unlike the sibling tools
here it is driven entirely by `extract_tables.toml` — no CLI arguments — and it
needs UnityPy (not stdlib). Reading the original CDN bundle (rather than the
client's decompressed cache copy) gives the exact patched version, stamped by
`AssetBundlesVersion.txt`.

Patch layout:
  * `<patch>/real/patch/Windows/<bundle>.unity` — the UnityFS bundles (LZ4HC
    compressed; UnityPy decompresses them transparently).
  * `<patch>/real/patch/Windows/AssetBundlesVersion.txt` — the patch version tag.

Table format (custom little-endian binary), per table `<PREFIX>_<Name>`:
  * `<PREFIX>_<Name>_head` = `[u32 count]` then `count × (u32 key, u32 offset,
    u32 length)` — a row index (byte spans) into the data blob.
  * `<PREFIX>_<Name>`      = `[u32 count]` then packed rows. A row starts with
    u32 fields; strings are `[varint len][UTF-8]` (1-byte length in practice).

Usage:
    python3 extract_tables.py            # reads ./extract_tables.toml
"""

from __future__ import annotations

import json
import struct
import sys
import tomllib
from pathlib import Path
from typing import Callable

CONFIG_PATH = Path(__file__).with_name("extract_tables.toml")
TABLE_PREFIXES = ("C_", "G_")
# Bundle directory within the patch mirror (mirrors the CDN URL structure).
WINDOWS_SUBDIR = Path("real/patch/Windows")


def load_config(path: Path) -> dict:
    if not path.is_file():
        sys.exit(f"config not found: {path}")
    with path.open("rb") as handle:
        return tomllib.load(handle)


def resolve_path(base: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else (base / candidate).resolve()


# --- patch mirror access --------------------------------------------------


def read_version(patch_dir: Path) -> str | None:
    """The patch version tag (`AssetBundlesVersion.txt`), if present."""
    version_file = patch_dir / WINDOWS_SUBDIR / "AssetBundlesVersion.txt"
    return version_file.read_text().strip() if version_file.is_file() else None


def find_bundle(patch_dir: Path, bundle_key: str) -> Path:
    """Locate a UnityFS bundle file within the local patch mirror."""
    direct = patch_dir / WINDOWS_SUBDIR / bundle_key
    if direct.is_file():
        return direct

    # Fall back to a recursive search by file name (handles unexpected layouts).
    matches = sorted(patch_dir.rglob(bundle_key.rsplit("/", 1)[-1]))
    if not matches:
        sys.exit(f"bundle not found under patch dir: {bundle_key} (looked in {patch_dir})")
    return matches[0]


def load_text_assets(bundle_data: Path) -> dict[str, bytes]:
    """Return {TextAsset name: raw bytes} for every TextAsset in the bundle."""
    try:
        import UnityPy
    except ImportError:
        sys.exit("UnityPy is required: pip install -r requirements.txt")

    env = UnityPy.load(str(bundle_data))
    assets: dict[str, bytes] = {}
    for obj in env.objects:
        if obj.type.name != "TextAsset":
            continue
        data = obj.read()
        name = getattr(data, "m_Name", None)
        if not name:
            continue
        script = data.m_Script
        assets[name] = (
            script.encode("utf-8", "surrogateescape")
            if isinstance(script, str)
            else bytes(script)
        )
    return assets


# --- table parsing --------------------------------------------------------


def parse_head(head: bytes) -> list[tuple[int, int, int]]:
    entries: list[tuple[int, int, int]] = []
    offset = 4  # skip the u32 count
    while offset + 12 <= len(head):
        entries.append(struct.unpack_from("<III", head, offset))
        offset += 12
    return entries


def read_strings(row: bytes, start: int = 12) -> list[str]:
    """Walk `[varint len][UTF-8]` tokens, skipping intervening non-string bytes."""
    out: list[str] = []
    pos = start
    while pos < len(row):
        length = row[pos]
        end = pos + 1 + length
        if 1 <= length <= 120 and end <= len(row) and all(
            0x20 <= byte < 0x7F for byte in row[pos + 1 : end]
        ):
            out.append(row[pos + 1 : end].decode())
            pos = end
        else:
            pos += 1
    return out


def decode_map_data(data: bytes, head: bytes) -> list[dict]:
    """Typed decoder for the Map_Data table (id, mapId, codename, scene bundle)."""
    maps = []
    for ordinal, (_key, offset, length) in enumerate(parse_head(head), start=1):
        row = data[offset : offset + length]
        row_id, sub_type, map_id = struct.unpack_from("<III", row, 0)
        strings = read_strings(row)
        scene = next(
            (
                s
                for s in strings
                if "/scene_bundle_" in s and "data" not in s and "batch" not in s
            ),
            "",
        )
        maps.append(
            {
                "ordinal": ordinal,
                "id": row_id,
                "mapId": map_id,
                "subType": sub_type,
                "name": strings[0] if strings else "",
                "category": scene.split("/")[0] if "/" in scene else "",
                "scene_bundle": scene,
                "minimap": next((s for s in strings if s.startswith("Minimap")), ""),
            }
        )
    return maps


def decode_generic(data: bytes, head: bytes) -> list[dict]:
    """Lossless decoder for tables without a typed schema.

    Emits the full raw row as hex (so nothing is ever dropped and a typed
    decoder can be written later against the exact bytes) alongside two
    convenience views: every u32 word and every embedded string. The word
    view over-reads string/float regions — it is a hint, not the schema — but
    `raw` always holds the complete row.
    """
    rows = []
    for ordinal, (key, offset, length) in enumerate(parse_head(head), start=1):
        row = data[offset : offset + length]
        words = list(struct.unpack_from("<" + "I" * (len(row) // 4), row, 0))
        rows.append(
            {
                "ordinal": ordinal,
                "key": key,
                "raw": row.hex(),
                "u32": words,
                "strings": read_strings(row),
            }
        )
    return rows


DECODERS: dict[str, Callable[[bytes, bytes], list[dict]]] = {
    "Map_Data": decode_map_data,
}


def resolve_table(assets: dict[str, bytes], base_name: str) -> tuple[bytes, bytes]:
    """Find the data + `_head` TextAssets for a base name, trying each prefix."""
    for prefix in TABLE_PREFIXES:
        data_name = f"{prefix}{base_name}"
        head_name = f"{data_name}_head"
        if data_name in assets and head_name in assets:
            return assets[data_name], assets[head_name]
    raise KeyError(base_name)


def table_base_names(assets: dict[str, bytes]) -> list[str]:
    """All base table names present (a data asset paired with its `_head`)."""
    bases = []
    for name in assets:
        if name.endswith("_head"):
            continue
        for prefix in TABLE_PREFIXES:
            if name.startswith(prefix) and f"{name}_head" in assets:
                bases.append(name[len(prefix) :])
                break
    return sorted(bases)


def main() -> None:
    config = load_config(CONFIG_PATH)
    base_dir = CONFIG_PATH.parent
    patch_dir = resolve_path(base_dir, config["paths"]["patch"])
    output_dir = resolve_path(base_dir, config["paths"]["output"])
    bundle_key = config["extract"]["bundle"]
    requested = config["extract"].get("tables", [])

    if not patch_dir.is_dir():
        sys.exit(f"patch dir not found: {patch_dir}")

    print(f"[+] patch version: {read_version(patch_dir) or 'unknown'}")
    bundle_data = find_bundle(patch_dir, bundle_key)
    print(f"[+] bundle: {bundle_data}")

    assets = load_text_assets(bundle_data)
    print(f"[+] {len(assets)} TextAssets in bundle")

    targets = requested or table_base_names(assets)
    output_dir.mkdir(parents=True, exist_ok=True)

    for base_name in targets:
        try:
            data, head = resolve_table(assets, base_name)
        except KeyError:
            print(f"[!] table not found (or no _head): {base_name}")
            continue
        decoder = DECODERS.get(base_name, decode_generic)
        rows = decoder(data, head)
        out_path = output_dir / f"{base_name}.json"
        out_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
        typed = "typed" if base_name in DECODERS else "generic"
        print(f"[+] {base_name}: {len(rows)} rows ({typed}) -> {out_path}")


if __name__ == "__main__":
    main()
