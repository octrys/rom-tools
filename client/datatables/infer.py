"""Infer a table's row layout against the runtime dump.

A depth-first search over "what is the next token in the row": a runtime leaf
in one of its possible encodings, or a skipped value. A token is accepted only
if it decodes to the runtime value in every sampled row at once, and a layout
only if it consumes every sampled row exactly. Candidate layouts are then
checked against ALL rows; a failing row joins the sample and the search
restarts (counterexample-driven).

Pruning: a memo of dead (positions, used leaves, skips) states; leaves with
identical values in every sample are tried in declaration order only
(symmetry breaking); list-of-struct element layouts are cached per position.
"""

from __future__ import annotations

import json
import struct
import time

from .codec import (ENCODINGS, MAX_LIST, SKIPS, Bad, apply_layout, compare, get_path, is_empty,
                    read_token, same)
from .config import Limits
from .dump import Dump, elem_type

DECLARED_ENC = {
    "System.Int32": "i32", "System.UInt32": "i32", "System.Int64": "i64", "System.Single": "f32",
    "System.Boolean": "bool", "System.String": "str", "System.Byte": "u8", "System.Int16": "i16",
}


# ---------------------------------------------------------------- leaves

def leaves_of(row: dict, prefix=()):
    """Runtime row -> (path, value); structs are flattened, lists stay leaves."""
    for k, v in row.items():
        p = prefix + (k,)
        if isinstance(v, dict):
            yield from leaves_of(v, p)
        else:
            yield p, v


def scalar_kind(values: list, decl: str | None) -> str | None:
    if decl in ("System.Single", "System.Double"):
        return "float"
    vs = [v for v in values if v is not None]
    if not vs:
        return None
    if all(isinstance(v, bool) for v in vs):
        return "bool"
    if all(isinstance(v, str) for v in vs):
        return "str"
    if any(isinstance(v, float) for v in vs):
        return "float"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in vs):
        return "int"
    return None


class Leaf:
    """One searchable column. kind: int/bool/float/str, 'slist' (list of
    scalars, .elem = scalar kind) or 'dlist' (list of structs, .sub = Leaves)."""

    def __init__(self, path, kind, elem=None, sub=None, fixed=None, informative=True, etype=None):
        self.path, self.kind, self.elem, self.sub = path, kind, elem, sub
        self.fixed = fixed  # constant list length across all rows, if any
        self.informative = informative
        self.etype = etype  # declared element type, for lists never seen non-empty


def declared_layout(dump: Dump, t: str, depth: int = 0) -> list:
    """Straight declaration-order layout of a type: the best guess for list
    elements the runtime never shows (empty slots the loader filters out)."""
    out = []
    for ft, name, _ in dump.types.get(t, []):
        et = elem_type(ft)
        if et:
            inner = ["d", declared_layout(dump, et, depth + 1)] if et in dump.types else ["s", DECLARED_ENC.get(et, "i32")]
            out.append(["l", [name], "i32", inner])
        elif ft in dump.types and depth < 6:
            out.extend([tok[0], [name] + tok[1]] + tok[2:] for tok in declared_layout(dump, ft, depth + 1))
        else:
            out.append(["v", [name], DECLARED_ENC.get(ft, "i32")])  # enums are int32
    return out


def build_leaves(dump: Dump, row_type: str | None, rows: list) -> list[Leaf]:
    """Leaves for a set of runtime values (rows or list elements)."""
    order, values = [], {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for p, v in leaves_of(row):
            if p not in values:
                values[p] = []
                order.append(p)
            values[p].append(v)
    out = []
    for p in order:
        vs = values[p]
        decl = dump.field_type(row_type, p) if row_type else None
        if any(isinstance(v, list) for v in vs):
            lists = [v for v in vs if isinstance(v, list)]
            lens = {len(v) for v in lists}
            fixed = lens.pop() if len(lens) == 1 and len(lists) == len(vs) else None
            elems = [e for v in lists for e in v]
            informative = any(lists)
            etype = elem_type(decl)
            if elems and all(isinstance(e, dict) for e in elems) or not elems and etype in dump.types:
                sub = build_leaves(dump, etype, elems)
                out.append(Leaf(p, "dlist", sub=sub, fixed=fixed, informative=informative, etype=etype))
            else:
                ek = scalar_kind(elems, etype) or "int"
                out.append(Leaf(p, "slist", elem=ek, fixed=fixed, informative=informative))
            continue
        kind = scalar_kind(vs, decl)
        if kind is None:
            continue  # always null: nothing to find in the binary
        distinct = {json.dumps(v) for v in vs}
        out.append(Leaf(p, kind, informative=len(distinct) > 1 or vs[0] not in (0, "", False, 0.0)))
    return out


# ---------------------------------------------------------------- search

class Search:
    def __init__(self, dump: Dump, localname: dict[int, str], deadline: float, limits: Limits):
        self.dump = dump
        self.localname = localname
        self.deadline = deadline
        self.limits = limits
        self.nodes = 0
        self.dead: set = set()  # (positions, used, skips) that led to no layout
        self.found = 0
        self.lists: dict = {}  # list-of-struct results per (leaf, mode, positions)

    def child(self) -> "Search":
        sub = Search(self.dump, self.localname, self.deadline, self.limits)
        sub.nodes = self.nodes
        return sub

    def tick(self) -> None:
        self.nodes += 1
        if self.nodes > self.limits.node_budget or time.monotonic() > self.deadline:
            raise TimeoutError

    def record(self, leaves: list[Leaf], samples: list, bounded: bool):
        """Yield (layout, new_positions) for records described by `leaves`.
        samples: list of (buf, pos, end, value). bounded: `end` is the record
        end (rows) — else end is the buffer end (list elements), and a layout
        is complete once every informative leaf has been read."""
        # Symmetry breaking: leaves with identical values in every sample are
        # interchangeable here, so only their declaration order is tried. A
        # row telling them apart later becomes a counterexample and splits them.
        first_of, self.twin = {}, {}
        for i, leaf in enumerate(leaves):
            if leaf.kind in ENCODINGS:
                sig = (leaf.kind, tuple(json.dumps(get_path(v, leaf.path)) for _, _, _, v in samples))
                if sig in first_of:
                    self.twin[i] = first_of[sig]
                first_of.setdefault(sig, i)
        self.prev_twin = {i: max(j for j in range(i) if self.twin.get(j, j) == self.twin[i]) for i in self.twin}
        yield from self._dfs(leaves, samples, bounded, [s[1] for s in samples], set(), -1, [], 0)

    def _dfs(self, leaves, samples, bounded, poses, used, last, layout, skips):
        """Pre-order yield when a record is complete. For unbounded records
        (list elements) also yield post-order — longest first — so an element
        may leave computed fields out; the caller verifies every element."""
        self.tick()
        complete = False
        if bounded:
            at_end = [p == s[2] for p, s in zip(poses, samples)]
            if all(at_end):
                self.found += 1
                yield list(layout), poses
                return
            if any(at_end):
                return
            key = (tuple(poses), frozenset(used), skips)
            if key in self.dead:
                return
            found_before = self.found
        elif all(leaf.informative is False or i in used for i, leaf in enumerate(leaves)) and layout:
            yield list(layout), poses
            complete = True

        n = len(leaves)
        for i in ((last + 1 + k) % n for k in range(n)):
            if i in used or i in self.prev_twin and self.prev_twin[i] not in used:
                continue
            leaf = leaves[i]
            for tok, new in self._leaf_tokens(leaf, samples, poses):
                layout.append(tok)
                used.add(i)
                yield from self._dfs(leaves, samples, bounded, new, used, i, layout, skips)
                used.discard(i)
                layout.pop()
            if leaf.kind == "dlist":
                for j, tok, new in self._tagged(leaves, i, used, samples, poses):
                    layout.append(tok)
                    used.update((i, j))
                    yield from self._dfs(leaves, samples, bounded, new, used, i, layout, skips)
                    used.difference_update((i, j))
                    layout.pop()
        if skips < self.limits.max_skips:
            for enc in SKIPS:
                new = []
                try:
                    for (buf, _, end, _), p in zip(samples, poses):
                        new.append(read_token(enc, buf, p, end, self.localname)[1])
                except Bad:
                    continue
                layout.append(["x", enc])
                yield from self._dfs(leaves, samples, bounded, new, used, last, layout, skips + 1)
                layout.pop()
        if not bounded and not complete and layout and layout[-1][0] != "x":
            yield list(layout), poses
        if bounded and self.found == found_before:
            self.dead.add(key)

    def _verify(self, tok, samples, poses) -> list[int]:
        """Positions after `tok` in every sample, if it decodes to the runtime value."""
        new = []
        for (buf, _, end, val), p in zip(samples, poses):
            got, p = apply_layout([tok], buf, p, end, self.localname)
            if compare([tok], got, val):
                raise Bad
            new.append(p)
        return new

    def _scalar(self, enc, samples, poses, path) -> list[int]:
        new = []
        for (buf, _, end, val), p in zip(samples, poses):
            v, p2 = read_token(enc, buf, p, end, self.localname)
            if not same(enc, v, get_path(val, path)):
                raise Bad
            new.append(p2)
        return new

    def _leaf_tokens(self, leaf, samples, poses):
        path = leaf.path
        if leaf.kind in ENCODINGS:
            for enc in ENCODINGS[leaf.kind]:
                try:
                    yield ["v", list(path), enc], self._scalar(enc, samples, poses, path)
                except Bad:
                    pass
            return
        # list modes: exact i32 count, fixed-capacity with empty slots dropped, fixed length
        modes = [("i32", False), ("i32", True)] + ([(leaf.fixed, False)] if leaf.fixed else [])
        for cnt, filt in modes:
            try:
                after, ns, exps = [], [], []
                for (buf, _, end, val), p in zip(samples, poses):
                    exp = get_path(val, path)
                    exp = [] if exp in ("null", None) else exp
                    if not isinstance(exp, list):
                        raise Bad
                    if cnt == "i32":
                        if p + 4 > end:
                            raise Bad
                        n = struct.unpack_from("<i", buf, p)[0]
                        p += 4
                    else:
                        n = cnt
                    if not (len(exp) <= n <= MAX_LIST if filt else n == len(exp)):
                        raise Bad
                    after.append(p)
                    ns.append(n)
                    exps.append(exp)
            except Bad:
                continue
            tail = ["filter"] if filt else []
            if leaf.kind == "slist":
                for enc in ENCODINGS[leaf.elem]:
                    try:
                        new = []
                        for (buf, _, end, _), p, n, exp in zip(samples, after, ns, exps):
                            items = []
                            for _ in range(n):
                                v, p = read_token(enc, buf, p, end, self.localname)
                                items.append(v)
                            if filt:
                                items = [v for v in items if not is_empty(v)]
                            if len(items) != len(exp) or not all(same(enc, v, e) for v, e in zip(items, exp)):
                                raise Bad
                            new.append(p)
                        yield ["l", list(path), cnt, ["s", enc]] + tail, new
                    except Bad:
                        pass
            else:
                yield from self._dlist(leaf, cnt, tail, samples, poses, after, ns, exps)

    def _tagged(self, leaves, i, used, samples, poses):
        key = ("t", i, frozenset(used), tuple(poses))
        if key not in self.lists:
            self.lists[key] = list(self._tagged_search(leaves, i, used, samples, poses))
        return iter(self.lists[key])

    def _tagged_search(self, leaves, i, used, samples, poses):
        """Two lists of the same element type sharing one slot array, each slot
        tagged with the list it belongs to."""
        a = leaves[i]
        shape = {tuple(leaf.path) for leaf in a.sub}
        for j, b in enumerate(leaves):
            if j == i or j in used or b.kind != "dlist":
                continue
            if a.sub and b.sub and {tuple(leaf.path) for leaf in b.sub} != shape:
                continue
            elem_leaves = a.sub or b.sub
            if not elem_leaves:
                continue
            for order in ((a, b), (b, a)):
                paths = [list(order[0].path), list(order[1].path)]
                firsts = []
                for (buf, _, end, val), p in zip(samples, poses):
                    if p + 8 > end:
                        break
                    n, tag = struct.unpack_from("<ii", buf, p)
                    if n <= 0 or tag not in (1, 2):
                        continue
                    exp = get_path(val, order[tag - 1].path)
                    if isinstance(exp, list) and exp:
                        firsts.append((buf, p + 8, end, exp[0]))
                if not firsts:
                    continue
                sub = self.child()
                for tried, (elayout, _) in enumerate(sub.record(elem_leaves, firsts, False)):
                    self.nodes = sub.nodes
                    if tried >= self.limits.max_element_layouts:
                        break
                    tok = ["t", paths, "i32", ["d", elayout]]
                    try:
                        yield j, tok, self._verify(tok, samples, poses)
                    except Bad:
                        continue

    def _dlist(self, leaf, cnt, tail, samples, poses, after, ns, exps):
        key = (tuple(leaf.path), cnt, tuple(tail), tuple(poses))
        if key not in self.lists:
            self.lists[key] = list(self._dlist_search(leaf, cnt, tail, samples, poses, after, ns, exps))
        return iter(self.lists[key])

    def _dlist_search(self, leaf, cnt, tail, samples, poses, after, ns, exps):
        path = leaf.path
        firsts = [(buf, p, end, exp[0]) for (buf, _, end, _), p, exp in zip(samples, after, exps) if exp]
        if not firsts:
            if not any(ns):
                yield ["l", list(path), cnt, ["d", []]] + tail, after
            elif tail and leaf.etype in self.dump.types:
                # only empty slots in the sample: read them with the declared layout
                tok = ["l", list(path), cnt, ["d", declared_layout(self.dump, leaf.etype)]] + tail
                try:
                    yield tok, self._verify(tok, samples, poses)
                except Bad:
                    pass
            return
        # Element layout learnt from the first binary slot of each sample, then
        # verified on every element. In filtered lists that slot may be an
        # empty one, so also learn from single samples.
        groups = [firsts] + ([[f] for f in firsts[:4]] if tail and len(firsts) > 1 else [])
        seen = set()
        for group in groups:
            sub = self.child()
            elements = [(b, p, len(b), v) for b, p, _, v in group]
            for tried, (elayout, _) in enumerate(sub.record(leaf.sub, elements, False)):
                self.nodes = sub.nodes
                if tried >= self.limits.max_element_layouts:
                    break
                keys = [[]] + ([[t[1]] for t in elayout if t[0] == "v"] if tail else [])
                for key in keys:
                    tok = ["l", list(path), cnt, ["d", elayout]] + tail + key
                    sig = json.dumps(tok)
                    if sig in seen:
                        continue
                    seen.add(sig)
                    try:
                        yield tok, self._verify(tok, samples, poses)
                    except Bad:
                        continue


# ---------------------------------------------------------------- tables

def pick_samples(rows: list, k: int, forced=()) -> list:
    """Greedy: rows that add the most unseen (leaf, value) pairs, plus rows
    with non-empty lists so list layouts are learnable."""
    def pairs(r):
        return {(p, json.dumps(v)) for p, v in leaves_of(r["row"])}

    def lists(r):
        return sum(2 for _, v in leaves_of(r["row"]) if isinstance(v, list) and v)

    chosen = list(forced)
    taken = {id(r) for r in chosen}
    seen = set().union(*(pairs(r) for r in chosen)) if chosen else set()
    pool = rows if len(rows) <= 4000 else rows[::max(1, len(rows) // 4000)]
    pool = [(r, pairs(r), lists(r)) for r in pool if id(r) not in taken]
    while len(chosen) < k and pool:
        i, gain = max(((i, len(ps - seen) + ls) for i, (_, ps, ls) in enumerate(pool)), key=lambda x: x[1])
        r, ps, _ = pool.pop(i)
        chosen.append(r)
        seen |= ps
        if gain == 0 and len(chosen) >= 4:
            break
    return chosen


def check_all(layout: list, data: bytes, idx: dict, rows: list, localname: dict[int, str]):
    """First runtime row the layout gets wrong -> (row, reason), else (None, None)."""
    for r in rows:
        span = idx.get(r["key"])
        if span is None:
            continue
        try:
            got, pos = apply_layout(layout, data, span[0], span[1], localname)
        except Bad:
            return r, "layout does not decode"
        if pos != span[1]:
            return r, f"{span[1] - pos} bytes left over"
        d = compare(layout, got, r["row"])
        if d:
            return r, d
    return None, None


def infer_table(dump: Dump, row_type: str | None, data: bytes, idx: dict, rows: list,
                localname: dict[int, str], limits: Limits, log=print):
    """-> (layout, None) or (None, reason)."""
    rows = [r for r in rows if r["key"] in idx and isinstance(r["row"], dict)]
    if not rows:
        return None, "no runtime rows with a binary span"
    deadline = time.monotonic() + limits.time_budget
    forced: list = []
    for rnd in range(limits.max_rounds):
        sample = pick_samples(rows, limits.sample_rows, forced)
        leaves = build_leaves(dump, row_type, [r["row"] for r in rows[:2000]] + [r["row"] for r in sample])
        samples = [(data, idx[r["key"]][0], idx[r["key"]][1], r["row"]) for r in sample]
        search = Search(dump, localname, deadline, limits)
        tried = 0
        try:
            for layout, _ in search.record(leaves, samples, True):
                tried += 1
                bad, why = check_all(layout, data, idx, rows, localname)
                if bad is None:
                    return layout, None
                if bad not in forced:
                    forced.append(bad)
                    log(f"    round {rnd}: layout {tried} fails on key {bad['key']} ({why}); resampling")
                    break
            else:
                return None, f"no layout fits the sample (round {rnd}, {search.nodes} nodes)"
        except TimeoutError:
            return None, f"search budget exhausted (round {rnd}, {search.nodes} nodes)"
    return None, "counterexample rounds exhausted"
