"""Annotated table schema: every column (nested structs and lists included) with
its declared type, offset and value-derived hints. Field names stay obfuscated
(random per build); the hints say what a column IS:

  id         top-level field equal to the row key in every row
  enum       member names are plaintext even though the type name is obfuscated
  fk         ints that are (almost all) keys of another table
  asset      strings sharing a resource prefix (UI_Icon_*, PC_Knight_*, ...)
  text       free text / datetime strings
  sharedWith other tables using the same obfuscated name — the obfuscator maps
             one original identifier to one name per build

and, from names.toml, whether each column is named yet:

  name       the readable name `decode` can apply
  naming     "confirmed" / "tentative" / "suggested" (the names.toml status),
             "obfuscated" (no entry yet) or "plaintext" (never obfuscated)

Each table gets `naming` counts too: how much of it is still to be named.
"""

from __future__ import annotations

import collections
import json
import re

from .code import obfuscated
from .dump import Dump, elem_type
from .names import Entry

FK_MIN_DISTINCT = 20  # below this, small key sets (1..5) match by accident
FK_MIN_COVERAGE = 0.95
SAMPLE = 5
MAX_DEPTH = 8
# Hard stop: real rows have at most a few hundred columns. Following the
# declared type graph (instead of the values actually present) explodes.
MAX_COLUMNS_PER_TABLE = 2000
ASSET_RE = re.compile(r"^([A-Za-z]+_[A-Za-z]*)[A-Za-z0-9_./-]*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def collect(values: list, field: str) -> list:
    """Values of `field` across row dicts, flattening lists."""
    out = []
    for v in values:
        if isinstance(v, dict) and field in v:
            x = v[field]
            out.extend(x) if isinstance(x, list) else out.append(x)
    return out


class Builder:
    def __init__(self, dump: Dump, runtime: dict):
        self.dump = dump
        keys = {n: {r["key"] for r in t["rows"] if isinstance(r["key"], int)}
                for n, t in runtime.items() if isinstance(t, dict) and t.get("rows")}
        self.keys = {n: ks for n, ks in keys.items() if ks}
        self.usage = collections.defaultdict(set)  # obfuscated field name -> tables
        self.stats = collections.Counter()
        self.table_cols = 0

    def fk(self, table: str, ints: list[int]):
        counts = collections.Counter(ints)
        if len(counts) < FK_MIN_DISTINCT:
            return None
        best = None
        for other, ks in self.keys.items():
            if other == table:
                continue
            cov = sum(n for v, n in counts.items() if v in ks) / len(ints)
            if cov >= FK_MIN_COVERAGE and (best is None or (cov, -len(ks)) > (best[1], -len(self.keys[best[0]]))):
                best = (other, cov)
        return best

    def scalar_hints(self, table: str, t: str, vals: list, col: dict) -> None:
        present = [v for v in vals if v is not None]
        distinct = {json.dumps(v, ensure_ascii=False) for v in present}
        col["distinct"] = len(distinct)
        if len(distinct) == 1 and present:
            col["constant"] = present[0]
        if t in self.dump.enums:
            members = self.dump.enums[t]
            seen = collections.Counter(v for v in present if isinstance(v, int))
            col["enum"] = {str(k): v for k, v in sorted(members.items())}
            col["enumSeen"] = {str(k): members.get(k, "?") for k in sorted(seen)}
            bad = [k for k in seen if k not in members]
            if bad:
                col["enumInvalid"] = sorted(bad)[:SAMPLE]
            prefixes = collections.Counter(m.split("_")[0] for m in members.values() if "_" in m)
            col["hint"] = f"enum {prefixes.most_common(1)[0][0]}_*" if prefixes else "enum"
            self.stats["enum"] += 1
            return
        ints = [v for v in present if isinstance(v, int) and not isinstance(v, bool) and v not in (0, -1)]
        strs = [v for v in present if isinstance(v, str) and v]
        if ints:
            col["range"] = [min(ints), max(ints)]
            ref = self.fk(table, ints)
            if ref:
                col["fk"] = ref[0]
                col["fkCoverage"] = round(ref[1], 3)
                col["hint"] = f"fk {ref[0]}"
                self.stats["fk"] += 1
        if strs:
            col["sample"] = list(dict.fromkeys(strs))[:SAMPLE]
            if sum(bool(DATE_RE.match(s)) for s in strs) >= 0.8 * len(strs):
                col["hint"] = "datetime"
            elif sum(" " in s for s in strs) >= 0.3 * len(strs):
                col["hint"] = "text"
            else:
                pref = collections.Counter(m.group(1) for s in strs if (m := ASSET_RE.match(s)))
                if pref and sum(pref.values()) >= 0.8 * len(strs):
                    col["hint"] = f"asset {pref.most_common(1)[0][0]}*"
            self.stats["string:" + col.get("hint", "other").split()[0]] += 1
        elif not ints or "fk" not in col:
            col.setdefault("sample", list(dict.fromkeys(present))[:SAMPLE])

    def fields(self, table: str, type_name: str, values: list, path: str, seen: tuple) -> list[dict]:
        """Columns of `type_name`. Only descends where the runtime values
        actually hold nested dicts."""
        cols = []
        for t, name, off in self.dump.types.get(type_name, []):
            self.usage[name].add(table)
            col = {"field": name, "type": t, "offset": off}
            if path:
                col["path"] = f"{path}.{name}"
            vals = collect(values, name)
            et = elem_type(t)
            inner = et or t
            if et:
                col["list"] = True
            nested = [v for v in vals if isinstance(v, dict)]
            if nested and inner in self.dump.types and inner not in seen and len(seen) < MAX_DEPTH:
                col["fields"] = self.fields(table, inner, nested, col.get("path", name), seen + (inner,))
            else:
                self.scalar_hints(table, inner, vals, col)
            self.table_cols += 1
            if self.table_cols > MAX_COLUMNS_PER_TABLE:
                raise RuntimeError(f"{table}: more than {MAX_COLUMNS_PER_TABLE} columns — runaway recursion")
            cols.append(col)
        return cols

    def table(self, name: str, meta: dict, rt: dict) -> dict:
        rows = rt.get("rows") or []
        out = {"name": name, **meta, "count": rt.get("count", len(rows))}
        if rows and not isinstance(rows[0]["row"], dict):
            out["kind"] = "string-map"  # LocalName / Localization: key -> text
            out["sample"] = {str(r["key"]): r["row"] for r in rows[:SAMPLE]}
            return out
        values = [r["row"] for r in rows]
        self.table_cols = 0
        out["fields"] = self.fields(name, meta["rowType"], values, "", (meta["rowType"],))
        self.stats["columns"] += self.table_cols
        for c in out["fields"]:
            if rows and all(isinstance(r["row"], dict) and r["row"].get(c["field"]) == r["key"] for r in rows):
                c["hint"] = "id"
                break
        return out


def annotate_shared(cols: list[dict], usage: dict, table: str) -> None:
    for c in cols:
        others = sorted(usage[c["field"]] - {table})
        if others:
            c["sharedWith"] = others
        if "fields" in c:
            annotate_shared(c["fields"], usage, table)


def annotate_names(cols: list[dict], entries: dict[str, Entry], counts: collections.Counter) -> None:
    for c in cols:
        entry = entries.get(c["field"])
        if entry:
            c["name"], c["naming"] = entry.name, entry.status
        else:
            c["naming"] = "obfuscated" if obfuscated(c["field"]) else "plaintext"
        counts[c["naming"]] += 1
        if "fields" in c:
            annotate_names(c["fields"], entries, counts)


def build(dump: Dump, runtime: dict, entries: dict[str, Entry] | None = None) -> tuple[dict, collections.Counter, list[str]]:
    """-> (schema by table, stats, tables missing from the dump). `entries`
    is names.toml: each column's readable name and naming status."""
    b = Builder(dump, runtime)
    schema, missing = {}, []
    for name, rt in runtime.items():
        meta = dump.tables.get(name)
        if not meta or not isinstance(rt, dict) or meta["rowType"] not in dump.types and meta["rowType"] != "System.String":
            missing.append(name)
            continue
        schema[name] = b.table(name, meta, rt)
    for name, t in schema.items():
        annotate_shared(t.get("fields", []), b.usage, name)
        if "fields" in t:
            counts = collections.Counter()
            annotate_names(t["fields"], entries or {}, counts)
            t["naming"] = dict(counts.most_common())
            b.stats.update({f"naming:{k}": n for k, n in counts.items()})
    return schema, b.stats, missing
