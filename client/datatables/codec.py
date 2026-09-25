"""Row binary format, layouts, and decoded-vs-runtime comparison.

Rows are little-endian, BinaryWriter style: i32, 7-bit-length-prefixed UTF-8
strings, 1-byte bools. Each table's loader is hand-written, so a row is decoded
by a LAYOUT — a list of JSON-serialisable tokens:

  ["v", path, enc]                          scalar leaf
  ["l", path, cnt, ["s", enc]]              list of scalars; cnt = "i32" | int (fixed length)
  ["l", path, cnt, ["d", layout]]           list of structs
  ["l", ..., "filter"(, keypath)]           the loader drops empty slots (or slots
                                            whose key field is empty)
  ["t", [path, ...], "i32", ["d", layout]]  tagged slots: i32 count, then per slot an
                                            i32 tag (0 = empty, k = element of the k-th list)
  ["x", enc]                                value read and discarded

Scalar encodings (loader conventions found in the tables):
  i32 u8 i16 i64 f32 f64   plain
  bool bool32              1-byte / 4-byte boolean
  str                      7-bit-length-prefixed UTF-8
  str_int                  an int written as a string
  i32_str i64_str          a string written as an int
  loc                      i32 LocalName id (unknown id -> the id as text, 0 -> "")
  milli                    fixed point: i32 thousandths
  bits                     flag set as a list of 1-based bit numbers
  orlist                   flag set as a list of OR-ed values
"""

from __future__ import annotations

import json
import math
import struct

MAX_LIST = 100_000

# Encodings tried per runtime value kind, most likely first.
ENCODINGS = {
    "int": ["i32", "str_int", "u8", "i16", "i64", "bits", "orlist", "f32"],
    "bool": ["bool", "bool32"],
    "float": ["f32", "milli", "i32", "f64"],
    "str": ["str", "loc", "i32_str", "i64_str"],
}
SKIPS = ["skip_i32", "skip_str", "skip_u8"]
FMT = {
    "bool": "<B", "bool32": "<i", "milli": "<i", "i32": "<i", "u8": "<B", "i16": "<h", "i64": "<q",
    "f32": "<f", "f64": "<d", "loc": "<i", "i32_str": "<i", "i64_str": "<q", "skip_i32": "<i", "skip_u8": "<B",
}
SIZE = {enc: struct.calcsize(fmt) for enc, fmt in FMT.items()}


class Bad(Exception):
    """A token does not decode here (out of bounds / malformed / mismatch)."""


def read_str(buf: bytes, pos: int, end: int) -> tuple[str, int]:
    n = shift = 0
    while True:
        if pos >= end:
            raise Bad
        b = buf[pos]
        pos += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            break
        shift += 7
        if shift > 28:
            raise Bad
    if pos + n > end:
        raise Bad
    try:
        return buf[pos:pos + n].decode("utf-8"), pos + n
    except UnicodeDecodeError:
        raise Bad


def read_i32(buf: bytes, pos: int, end: int) -> tuple[int, int]:
    if pos + 4 > end:
        raise Bad
    return struct.unpack_from("<i", buf, pos)[0], pos + 4


def read_token(enc: str, buf: bytes, pos: int, end: int, localname: dict[int, str]):
    """Decode one scalar token -> (value, new_pos)."""
    if enc in ("bits", "orlist"):
        n, pos = read_i32(buf, pos, end)
        if not 0 <= n <= 64 or pos + 4 * n > end:
            raise Bad
        values = struct.unpack_from(f"<{n}i", buf, pos)
        pos += 4 * n
        if enc == "orlist":
            v = 0
            for x in values:
                v |= x
            return v, pos
        if any(not 0 <= b <= 63 for b in values):
            raise Bad
        return sum({1 << (b - 1) for b in values if b}), pos
    if enc in ("str", "skip_str", "str_int"):
        s, pos = read_str(buf, pos, end)
        if enc == "str_int":
            try:
                return int(s), pos
            except ValueError:
                raise Bad
        return s, pos
    n = SIZE[enc]
    if pos + n > end:
        raise Bad
    v = struct.unpack_from(FMT[enc], buf, pos)[0]
    if enc == "loc":
        v = localname.get(v, str(v)) if v else ""
    elif enc in ("i32_str", "i64_str"):
        v = str(v)
    elif enc == "milli":
        v = v / 1000
    elif enc in ("bool", "bool32"):
        if v not in (0, 1):
            raise Bad
        v = bool(v)
    return v, pos + n


def get_path(row, path):
    v = row
    for k in path:
        if not isinstance(v, dict) or k not in v:
            return None
        v = v[k]
    return v


def set_path(row: dict, path, value) -> None:
    for k in path[:-1]:
        row = row.setdefault(k, {})
    row[path[-1]] = value


def is_empty(v) -> bool:
    """An unused slot: the loaders drop these from fixed-capacity lists."""
    if isinstance(v, dict):
        return all(is_empty(x) for x in v.values())
    if isinstance(v, list):
        return not v
    return v is None or v is False or v in (0, "", "null", "0")


def apply_layout(layout: list, buf: bytes, pos: int, end: int, localname: dict[int, str]) -> tuple[dict, int]:
    """Decode one record with a layout -> (dict, new_pos). Raises Bad."""
    out: dict = {}
    for tok in layout:
        if tok[0] == "v":
            v, pos = read_token(tok[2], buf, pos, end, localname)
            set_path(out, tok[1], v)
        elif tok[0] == "x":
            _, pos = read_token(tok[1], buf, pos, end, localname)
        elif tok[0] == "t":
            n, pos = read_i32(buf, pos, end)
            if not 0 <= n <= MAX_LIST:
                raise Bad
            lists: list[list] = [[] for _ in tok[1]]
            for _ in range(n):
                tag, pos = read_i32(buf, pos, end)
                if tag == 0:
                    continue
                if not 1 <= tag <= len(lists):
                    raise Bad
                v, pos = apply_layout(tok[3][1], buf, pos, end, localname)
                lists[tag - 1].append(v)
            for path, items in zip(tok[1], lists):
                set_path(out, path, items)
        else:
            path, cnt, elem = tok[1], tok[2], tok[3]
            if cnt == "i32":
                n, pos = read_i32(buf, pos, end)
                if not 0 <= n <= MAX_LIST:
                    raise Bad
            else:
                n = cnt
            items = []
            for _ in range(n):
                if elem[0] == "s":
                    v, pos = read_token(elem[1], buf, pos, end, localname)
                else:
                    v, pos = apply_layout(elem[1], buf, pos, end, localname)
                items.append(v)
            if tok[4:5] == ["filter"]:
                keypath = tok[5] if len(tok) > 5 else None
                items = [v for v in items if not is_empty(get_path(v, keypath) if keypath else v)]
            set_path(out, path, items)
    return out, pos


def same(enc: str, got, exp) -> bool:
    """Decoded value vs runtime value, with the dump agent's rendering quirks."""
    if exp is None:
        return False
    if exp == "null":  # how dump_tables.js renders a null managed string
        return got in ("", "null")
    if exp == "" and got == "0":  # empty sheet cells are exported as "0"
        return True
    if isinstance(exp, bool):
        return not isinstance(got, str) and got in (0, 1) and bool(got) == exp
    if isinstance(exp, float) or enc in ("f32", "f64", "milli"):
        try:
            if isinstance(got, bool) or isinstance(exp, (str, bool)):
                return False
            return math.isclose(float(got), float(exp), rel_tol=1e-6, abs_tol=1e-9) or \
                struct.pack("<f", float(got)) == struct.pack("<f", float(exp))
        except (TypeError, ValueError, OverflowError):
            return False
    return type(got) is type(exp) and got == exp


def compare(layout: list, got: dict, exp: dict, prefix: str = "") -> str | None:
    """First difference between a decoded record and its runtime value, on the
    leaves the layout reads. None if equal."""
    for tok in layout:
        if tok[0] == "x":
            continue
        if tok[0] == "t":
            for path in tok[1]:
                d = compare([["l", path, "i32", tok[3]]], got, exp, prefix)
                if d:
                    return d
            continue
        path = tok[1]
        name = prefix + ".".join(path)
        g, e = get_path(got, path), get_path(exp, path)
        if tok[0] == "v":
            if not same(tok[2], g, e):
                return (f"{name}: decoded {json.dumps(g, ensure_ascii=False)[:50]} "
                        f"!= runtime {json.dumps(e, ensure_ascii=False)[:50]}")
            continue
        if e in ("null", None):
            e = []
        if not isinstance(e, list) or len(g) != len(e):
            return f"{name}: decoded {len(g)} items, runtime {json.dumps(e)[:50]}"
        for i, (gi, ei) in enumerate(zip(g, e)):
            if tok[3][0] == "s":
                if not same(tok[3][1], gi, ei):
                    return f"{name}[{i}]: decoded {gi!r} != runtime {ei!r}"
            else:
                d = compare(tok[3][1], gi, ei, f"{name}[{i}].")
                if d:
                    return d
    return None


def describe(layout: list) -> str:
    skipped = sum(t[0] == "x" for t in layout)
    return f"{len(layout) - skipped} fields read, {skipped} skipped values"
