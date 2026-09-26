"""The game's code, for `xref`: the il2cpp dump as a class model, the unpacked
GameAssembly image, and its resolved metadata slots.

- rom_dump.cs (rom-frida's dump_client.js): every class/struct with its kind,
  parent, instance and static fields (type, offset) and methods (RVA,
  signature). dump.py reads only what the tables need; this reads it all.
- gameassembly.bin (rom-frida's dump_code.js): the Themida-unpacked module
  image laid out by RVA — file offset == RVA, so the dump's method RVAs index
  straight into it.
- gameassembly_slots.json (same agent): il2cpp code reaches classes, methods,
  fields and string literals through `[rip+slot]` globals; the agent resolved
  each slot in-game to what it names ({u: 1 class | 2 type | 3/6 method |
  4 field | 5 string, ...}).

All three must come from the same build.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

PRIM_SIZE = {
    "System.Boolean": 1, "System.Byte": 1, "System.SByte": 1, "System.Char": 2, "System.Int16": 2,
    "System.UInt16": 2, "System.Int32": 4, "System.UInt32": 4, "System.Single": 4, "System.Int64": 8,
    "System.UInt64": 8, "System.Double": 8, "System.IntPtr": 8, "System.UIntPtr": 8,
}
OBFUSCATED_RE = re.compile(r"^[A-P]{11}$")
CLASS_RE = re.compile(r"^(class|struct|enum|interface) (\S+)(?: : (.*))?$")
METHOD_RE = re.compile(r"^    (static )?(.+?) ([^ (]+)\((.*)\); // 0x([0-9a-f]+)$")
FIELD_RE = re.compile(r"^    (static )?(.+?) (\S+); // 0x([0-9a-f]+)$")


def split_top(s: str, sep: str = ",") -> list[str]:
    """Split on `sep` outside <...>."""
    out, depth, buf = [], 0, ""
    for ch in s:
        depth += (ch == "<") - (ch == ">")
        if ch == sep and depth == 0:
            out.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        out.append(buf.strip())
    return out


def param_types(params: str) -> list[str]:
    return [p.rsplit(" ", 1)[0] for p in split_top(params)]


def parse_sig(sig: str) -> tuple[bool, str, str, list[str]] | None:
    """'static Ret Name(T a, U b);' -> (static, ret, name, [T, U])."""
    m = re.match(r"^(static )?(.+?) ([^ (]+)\((.*)\);?\s*$", sig.strip())
    return (bool(m.group(1)), m.group(2), m.group(3), param_types(m.group(4))) if m else None


def obfuscated(name: str) -> bool:
    return bool(OBFUSCATED_RE.match(name))


@dataclass
class Method:
    cls: str | None
    static: bool
    ret: str
    name: str
    params: list[str]

    @property
    def full(self) -> str:
        return f"{self.cls}::{self.name}"


@dataclass
class Cls:
    kind: str
    parent: str | None
    fields: list[tuple[int, str, str]] = field(default_factory=list)  # (offset, type, name), instance
    statics: dict[int, tuple[str, str]] = field(default_factory=dict)  # offset -> (type, name)


class Model:
    """Classes and methods of the dump, with layout helpers. Offsets are the
    dump's: a struct's fields start at 0x10 (the boxed header), so inside the
    struct's raw data a field sits at offset - 0x10."""

    def __init__(self, dump_cs: Path):
        self.classes: dict[str, Cls] = {}
        self.methods: dict[int, list[Method]] = {}  # rva -> every method compiled to that code
        cur = None
        for line in dump_cs.read_text(encoding="utf-8", errors="replace").splitlines():
            m = CLASS_RE.match(line)
            if m:
                bases = split_top(m.group(3) or "")
                cur = m.group(2)
                self.classes.setdefault(cur, Cls(m.group(1), bases[0] if bases else None))
                continue
            if cur is None or not line.startswith("    "):
                continue
            m = METHOD_RE.match(line)
            if m:
                rva = int(m.group(5), 16)
                if rva:
                    self.methods.setdefault(rva, []).append(
                        Method(cur, bool(m.group(1)), m.group(2), m.group(3), param_types(m.group(4))))
                continue
            m = FIELD_RE.match(line)
            if m:
                off = int(m.group(4), 16)
                if m.group(1):
                    self.classes[cur].statics[off] = (m.group(2), m.group(3))
                else:
                    self.classes[cur].fields.append((off, m.group(2), m.group(3)))
        self._sizes: dict[str, int] = {}
        self._all: dict[str, list] = {}

    def add_method(self, rva: int, method: Method) -> None:
        """A method known from elsewhere (an inflated generic from a slot)."""
        self.methods.setdefault(rva, []).append(method)

    def method(self, rva: int) -> Method | None:
        ms = self.methods.get(rva)
        return ms[0] if ms else None

    def display(self, rva: int) -> str:
        """The most readable name among the methods sharing this code."""
        ms = self.methods.get(rva) or []
        best = min(ms, key=lambda m: (obfuscated(m.name), obfuscated((m.cls or "").rsplit(".", 1)[-1])),
                   default=None)
        return best.full if best else hex(rva)

    # ------------------------------------------------------------ types

    def kind(self, t: str) -> str | None:
        c = self.classes.get(t)
        return c.kind if c else None

    def is_struct(self, t: str) -> bool:
        return self.kind(t) == "struct" and t not in PRIM_SIZE

    def is_ref(self, t: str) -> bool:
        return self.kind(t) in ("class", "interface")

    def is_big_struct(self, t: str) -> bool:
        """Passed and returned by pointer in the x64 ABI (size not 1/2/4/8)."""
        return self.is_struct(t) and self.size(t) not in (1, 2, 4, 8)

    def size(self, t: str) -> int:
        """Storage size of a value (struct data without header), padding ignored."""
        if t in PRIM_SIZE:
            return PRIM_SIZE[t]
        if self.kind(t) == "enum":
            return 4
        if not self.is_struct(t):
            return 8  # reference
        if t not in self._sizes:
            self._sizes[t] = 1  # recursion guard
            fl = self.classes[t].fields
            if fl:
                off, ft, _ = max(fl)
                self._sizes[t] = off - 0x10 + self.size(ft)
        return self._sizes[t]

    def fields_of(self, t: str) -> list[tuple[int, str, str]]:
        """Instance fields, inherited ones included, by offset."""
        if t not in self._all:
            out, seen, k = [], set(), t
            while k in self.classes and k not in seen:
                seen.add(k)
                out += self.classes[k].fields
                k = self.classes[k].parent
            self._all[t] = sorted(out)
        return self._all[t]

    def field_at(self, t: str, off: int) -> tuple[int, str, str] | None:
        """The field of t covering dump offset `off`."""
        for f in self.fields_of(t):
            if f[0] <= off < f[0] + self.size(f[1]):
                return f
        return None

    def chain(self, t: str, off: int, obj: bool) -> list[tuple[str, str]]:
        """[(owner type, field name), ...] from t down to the innermost field at
        `off` (from an object pointer if obj, else from struct data)."""
        out = []
        if not obj:
            off += 0x10
        while True:
            f = self.field_at(t, off)
            if not f:
                return out
            fo, ft, fn = f
            out.append((t, fn))
            if not self.is_struct(ft):
                return out
            t, off = ft, off - fo + 0x10

    def leaf_size(self, t: str, off: int, obj: bool) -> int:
        c = self.chain(t, off, obj)
        if not c:
            return 8
        owner, name = c[-1]
        ft = next(ft for _, ft, fn in self.fields_of(owner) if fn == name)
        return self.size(ft)


class Image:
    """gameassembly.bin + gameassembly_slots.json."""

    def __init__(self, image: Path, slots: Path):
        self.data = image.read_bytes()
        meta = json.loads(slots.read_text(encoding="utf-8"))
        self.init = int(meta["init"], 16)
        self.slots = {int(k, 16): v for k, v in meta["slots"].items()}

    def slot_text(self, slot: int) -> str:
        """What a slot names, for listings."""
        v = self.slots.get(slot)
        if not v:
            return ""
        if v["u"] in (3, 6):
            return f"{v['c']}::{v['m']}"
        if v["u"] == 5:
            return json.dumps(v["s"], ensure_ascii=False)[:80]
        return v.get("c") or v.get("t") or ""
