"""The table bundle (tablecrypto.unity): TextAssets, row indexes, text tables.

Each table is two TextAssets:
  C_<Name>_head  i32 count, then count x (i32 key, i32 offset, i32 length)
  C_<Name>       the rows (offsets are into this asset)
Per-language tables are C_<Name>_<Language>; only English is read. The two
text tables need no layout:
  LocalName      int id -> text; a row is (i32 id, str text)
  Localization   UI string key -> text; body is i32 count + (str key, str text)
                 pairs, and its head indexes by string key
"""

from __future__ import annotations

import struct
from functools import cached_property
from pathlib import Path

from .codec import apply_layout, read_str

PREFIX = "C_"
LANGUAGE = "English"
TEXT_TABLES = ("LocalName", "Localization")


class Bundle:
    def __init__(self, path: Path):
        import UnityPy  # only needed here; keeps the other modules importable without it

        self.assets: dict[str, bytes] = {}
        for obj in UnityPy.load(str(path)).objects:
            if obj.type.name == "TextAsset":
                d = obj.read()
                s = d.m_Script
                self.assets[d.m_Name] = s.encode("utf-8", "surrogateescape") if isinstance(s, str) else bytes(s)
        self._lower = {k.lower(): k for k in self.assets}
        if self.asset("LocalName") is None:
            raise ValueError(f"{path}: no {PREFIX}LocalName_{LANGUAGE} — not the table bundle?")

    def asset(self, table: str) -> str | None:
        """Asset holding `table` (case-insensitive; per-language tables in English)."""
        for cand in (PREFIX + table, f"{PREFIX}{table}_{LANGUAGE}"):
            if cand.lower() in self._lower:
                return self._lower[cand.lower()]
        return None

    def index(self, asset: str) -> dict[int, tuple[int, int]] | None:
        """key -> (start, end) of each row, from the asset's _head."""
        head = self.assets.get(asset + "_head")
        if head is None:
            return None
        n = struct.unpack_from("<i", head, 0)[0]
        return {k: (o, o + ln) for k, o, ln in (struct.unpack_from("<iii", head, 4 + 12 * i) for i in range(n))}

    def rows(self, asset: str, layout: list) -> dict[int, dict]:
        """Every row of a table asset decoded with its layout, by key."""
        data = self.assets[asset]
        return {k: apply_layout(layout, data, start, end, self.localname)[0]
                for k, (start, end) in self.index(asset).items()}

    @cached_property
    def localname(self) -> dict[int, str]:
        asset = self.asset("LocalName")
        data = self.assets[asset]
        return {k: read_str(data, o + 4, e)[0] for k, (o, e) in self.index(asset).items()}

    @cached_property
    def localization(self) -> dict[str, str]:
        data = self.assets[self.asset("Localization")]
        n = struct.unpack_from("<i", data, 0)[0]
        out, pos = {}, 4
        for _ in range(n):
            k, pos = read_str(data, pos, len(data))
            out[k], pos = read_str(data, pos, len(data))
        return out

    def text_table(self, table: str) -> dict | None:
        """LocalName / Localization as key -> text, else None."""
        if table == "LocalName":
            return self.localname
        if table == "Localization":
            return self.localization
        return None
