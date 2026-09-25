"""Struct layouts, inheritance and fixed-array sizes from the runtime type dump.

The message catalog (``messages_typed.json``) gives each message's own fields,
but decoding nested structs, lists of structs and fixed arrays needs more: the
field layout of every ``Protocol.*`` struct, the inheritance chain (fields
serialize base-class-first), enum underlying types, and the sizes of fixed C#
arrays (which carry no length prefix on the wire).

All of this is derived from the frida runtime dump (``rom_dump.json``: each type
with ``full``, ``parent`` and ``fields[{name, type, isStatic}]``), except the
fixed-array sizes, which are compile-time constants reversed from captures and
runtime ``.Length`` reads (:data:`FIXED_ARRAYS`).

Two base-field rules, confirmed against captures:
- ``Network.IBSerializableList`` contributes a leading ``__state__`` byte
  (the header byte on every ``*List`` message);
- ``Network.IBSerializable.__id__`` is NOT serialized — it is a runtime identity,
  so it is denylisted.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Instance fields present in the dump that are not serialized on the wire.
_FIELD_DENYLIST = {"__id__"}

# C# arrays `T[]` serialize FIXED-SIZE with no length prefix; the size is a
# compile-time/enum constant that cannot be read from the wire. Keyed by
# (owner full name, field name). Reversed from captures + runtime `.Length`.
FIXED_ARRAYS: dict[tuple[str, str], int] = {
    ("Protocol.ItemOptions", "m_optionIndex"): 10,
    ("Protocol.AbilityValue", "m_data"): 249,
    ("Protocol.S2C_ContentsSetting", "m_levelLimits"): 10,
    ("Protocol.S2C_ContentsSetting", "m_values"): 3,
    ("Protocol.S2C_EquipList", "m_usePresetSlot"): 2,
    ("Protocol.S2C_EquipList", "m_equipItemList"): 2,
    ("Protocol.S2C_EquipList", "m_equipCostume"): 2,
    ("Protocol.S2C_EquipList", "m_equipPet"): 2,
    ("Protocol.S2C_EquipList", "m_monolithEquipItemList"): 2,
}


class StructCatalog:
    """Type layouts + inheritance + enums, derived from the runtime dump."""

    def __init__(
        self,
        own_fields: dict[str, list[tuple[str, str]]],
        parents: dict[str, str],
        enums: dict[str, str],
        message_fullnames: set[str],
        fixed_arrays: dict[tuple[str, str], int] | None = None,
    ) -> None:
        self._own = own_fields
        self._parents = parents
        self._enums = enums
        self._message_fullnames = message_fullnames
        self._fixed_arrays = fixed_arrays if fixed_arrays is not None else FIXED_ARRAYS
        self._prefix_cache: dict[str, tuple[tuple[str, str], ...]] = {}

    @classmethod
    def load(cls, dump_path: Path, message_names: set[str]) -> StructCatalog:
        raw = json.loads(dump_path.read_text(encoding="utf-8"))
        own: dict[str, list[tuple[str, str]]] = {}
        parents: dict[str, str] = {}
        enums: dict[str, str] = {}
        for entry in raw:
            full = entry["full"]
            parent = entry.get("parent") or ""
            if parent:
                parents[full] = parent
            fields = entry.get("fields", [])
            if parent == "System.Enum":
                # Underlying integer type lives in the special `value__` field.
                underlying = next(
                    (f["type"] for f in fields if f["name"] == "value__"),
                    "System.Int32",
                )
                enums[full] = underlying
                continue
            own[full] = [
                (f["name"], f["type"])
                for f in fields
                if not f.get("isStatic") and f["name"] not in _FIELD_DENYLIST
            ]
        message_fullnames = {f"Protocol.{name}" for name in message_names}
        logger.info(
            "loaded %d struct layouts, %d enums from %s",
            len(own),
            len(enums),
            dump_path.name,
        )
        return cls(own, parents, enums, message_fullnames)

    def _chain(self, full: str) -> list[str]:
        """[full, parent, ..., root] following the parent map."""
        out, cur = [], full
        seen = set()
        while cur and cur not in seen:
            out.append(cur)
            seen.add(cur)
            cur = self._parents.get(cur, "")
        return out

    def layout(self, full: str) -> tuple[tuple[str, str], ...] | None:
        """Full field layout of a struct, base-class-first, or None if unknown."""
        own = self._own.get(full)
        if own is None:
            return None
        return (*self.base_prefix(full), *own)

    def base_prefix(self, full: str) -> tuple[tuple[str, str], ...]:
        """Inherited fields of the classes strictly above ``full``, base-first."""
        if full not in self._prefix_cache:
            fields: list[tuple[str, str]] = []
            for cls in reversed(self._chain(full)[1:]):
                fields.extend(self._own.get(cls, []))
            self._prefix_cache[full] = tuple(fields)
        return self._prefix_cache[full]

    def enum_underlying(self, type_name: str) -> str | None:
        return self._enums.get(type_name)

    def is_message_element(self, type_name: str) -> bool:
        return type_name in self._message_fullnames

    def array_size(self, owner: str | None, field: str | None) -> int | None:
        if owner is None or field is None:
            return None
        return self._fixed_arrays.get((owner, field))
