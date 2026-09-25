"""The il2cpp dump (rom-frida's `rom_dump.cs`) as a type model for the tables.

- types:  class/struct -> instance fields in declaration order (type, name, offset)
- enums:  enum -> {value: member}; member names are NOT obfuscated even when the
          enum's type name is, so they name both the value and the field
- tables: table name -> table class, key type, row type. A table class extends
          `<Base><TKey, TRow>` and its first member is the static name literal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

LIST_RE = re.compile(r"^System\.Collections\.Generic\.(?:List|IList|HashSet|Queue|Stack)<(.+)>$")
FIELD_RE = re.compile(r"^    (\S+) (\w+); // 0x([0-9a-f]+)$", re.M)  # \w+ skips `Name();` methods
ENUM_MEMBER_RE = re.compile(r"^    static \S+ (\w+) = (-?\d+);$", re.M)
BLOCK_RE = re.compile(r"^(class|struct|enum) (\S+) : ([^\n]*)\n\{\n(.*?)\n\}", re.M | re.S)
TABLE_RE = re.compile(
    r'^class (\S+) : \S+<([^,>]+),([^>]+)>\n\{\n    static System\.String \w+ = "([^"]+)";', re.M
)


def elem_type(t: str | None) -> str | None:
    """Element type of a List<T>-like or T[] declaration, else None."""
    if not t:
        return None
    m = LIST_RE.match(t)
    if m:
        return m.group(1)
    return t[:-2] if t.endswith("[]") else None


@dataclass
class Dump:
    types: dict[str, list[tuple[str, str, int]]]
    enums: dict[str, dict[int, str]]
    tables: dict[str, dict[str, str]]  # name -> {tableClass, keyType, rowType}

    def row_type(self, table: str) -> str | None:
        return self.tables.get(table, {}).get("rowType")

    def field_type(self, owner: str | None, path) -> str | None:
        """Declared type at `path` (field names, through lists/arrays) from `owner`."""
        t = owner
        for name in path:
            t = elem_type(t) or t
            t = next((ft for ft, n, _ in self.types.get(t, []) if n == name), None)
            if t is None:
                return None
        return t

    def enum_name(self, value: int, enum: str) -> int | str:
        """Member name; an undeclared value made of declared single-bit members is
        a flag set -> 'A|B'; anything else stays a number."""
        members = self.enums[enum]
        if value in members:
            return members[value]
        if value > 0:
            bits = [1 << i for i in range(value.bit_length()) if value >> i & 1]
            if all(b in members for b in bits):
                return "|".join(members[b] for b in bits)
        return value

    def name_enums(self, value, t: str | None):
        """A decoded row/value with enum-typed ints replaced by member names."""
        if isinstance(value, dict):
            decl = {n: ft for ft, n, _ in self.types.get(t, [])}
            return {k: self.name_enums(v, decl.get(k)) for k, v in value.items()}
        if isinstance(value, list):
            et = elem_type(t)
            return [self.name_enums(v, et) for v in value]
        if t in self.enums and isinstance(value, int) and not isinstance(value, bool):
            return self.enum_name(value, t)
        return value


def parse(path: Path) -> Dump:
    cs = path.read_text(encoding="utf-8")
    types, enums = {}, {}
    for kind, name, _base, body in BLOCK_RE.findall(cs):
        if kind == "enum":
            enums[name] = {int(v): k for k, v in ENUM_MEMBER_RE.findall(body)}
        else:
            types[name] = [(t, n, int(o, 16)) for t, n, o in FIELD_RE.findall(body)]
    tables = {
        m.group(4): {"tableClass": m.group(1), "keyType": m.group(2), "rowType": m.group(3)}
        for m in TABLE_RE.finditer(cs)
    }
    return Dump(types, enums, tables)
