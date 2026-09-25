"""UI traces -> evidence of which table column feeds which UI label.

A trace (rom-frida's trace_ui_text.js, one JSON object per line) records every
text the UI displays (string, GameObject path, time, frame) and every table
row read by key; Localization reads give the caption keys. Traces hold values
and UI paths, not obfuscated names, so they stay valid across client builds:
they are matched against the tables decoded for the current build.

Texts shown together form a record: same frame (same millisecond in traces
without frames) and same instance — the nearest ancestor that is a prefab
instance (`(Clone)`) or carries a sibling index (`Slot(Clone)#3`), at most
INSTANCE_DEPTH levels up, so a cell's name and its value in a nested layout
group stay together; otherwise the direct parent. Two ways a column is tied to
a label, counted over ALL traces:

anchor    a record's candidate rows are (1) rows with a string cell equal to
          one of its texts ("Dragon Harbor" -> Map_Data[108]) and (2) rows
          read by key right before its texts — since the previous text, at
          most READ_WINDOW_MS earlier: the game reads row i, then fills cell
          i. Every number in the record that equals a field of a candidate row
          votes for (column <- label); so does one equal to the value / 1000
          (the tables store rates in thousandths: 20000 -> "+20%"), noted as
          the finding's `scale`. Precision = votes / records where the
          table had a candidate and the label showed a number: the real
          source is near 100%; coincidences, and rows read by game logic
          rather than the UI, are low.
valueset  a label that showed several distinct numbers (>= 10) or strings is
          matched against every column; the column holding all of them with
          the fewest distinct values of its own wins. No row context, so weaker.
state     a live game component holding a table row (CMapManager.m_sMapData)
          and plaintext-named fields next to it (m_vMapSize, m_txtTitle...):
          the row is known exactly (by its key), so each value is compared
          with that row's columns only (numbers as is or / 1000, strings and
          text components by equality). Precision over distinct snapshots;
          a finding needs MIN_STATE_VALUES distinct matched values, so a
          constant can't pass for a column. The label is the game's own field
          name — the strongest naming evidence there is.
"""

from __future__ import annotations

import collections
import json
import math
import re
from pathlib import Path

MIN_RECORDS = 3  # records (or distinct values) a finding needs to be reported
MAX_CANDIDATE_ROWS = 8  # an anchor text (or a burst of reads) in more rows of a table is too generic
READ_WINDOW_MS = 1000  # rows read longer before a text are not its candidates
TEXT_TABLES = ("LocalName", "Localization")  # their reads are captions, not rows
INSTANCE_DEPTH = 3  # ancestors searched for the instance a text belongs to
SCALES = (1, 1000)  # shown value = stored value / scale
VALUESET_MIN_NUMBER = 10  # smaller numbers (counters, pins) are in every column
EXAMPLES = 3
MIN_STATE_RECORDS = 2  # distinct snapshots matching a column
MIN_STATE_VALUES = 2  # ... with this many distinct values

TAG_RE = re.compile(r"<[^>]{1,40}>")
NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)*")
CLONE_RE = re.compile(r"\(Clone\)|#\d+|\(\d+\)")
INSTANCE_RE = re.compile(r"\(Clone\)|#\d+$")
TIME_RE = re.compile(r"\d{1,2}:\d{2}")
# "53. [W] The Witch's Hideout" -> "[W] The Witch's Hideout" -> "The Witch's Hideout"; "(Lv 5)" -> "Lv 5"
CORE_RES = [re.compile(r"^\d+\.\s*"), re.compile(r"\[[^\]]*\]\s*"), re.compile(r"^\(|\)$")]


def load(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def clean(text: str) -> str:
    return TAG_RE.sub("", text).replace("​", "").strip()


def detect_thousands(texts) -> str:
    """The client formats numbers with its locale ('23.000' or '23,000'): a
    separator followed by exactly three digits decides it."""
    votes = collections.Counter()
    for text in texts:
        for sep, _ in re.findall(r"\d([.,])(\d{3})(?!\d)", text):
            votes[sep] += 1
    return votes.most_common(1)[0][0] if votes else ","


def numbers(text: str, thousands: str) -> list[float]:
    """Numbers in display order."""
    dec = "," if thousands == "." else "."
    out = []
    for tok in NUM_RE.findall(text):
        try:
            out.append(round(float(tok.replace(thousands, "").replace(dec, ".")), 6))
        except ValueError:
            pass
    return out


def cores(text: str) -> set[str]:
    out, t = {text}, text
    for r in CORE_RES:
        t = r.sub("", t).strip()
        out.add(t)
    return {c for c in out if len(c) >= 3}


def split_path(path: str) -> tuple[str, str]:
    """GameObject path -> (parent path, label). The label is '<parent>/<leaf>'
    without (Clone) and #index markers, so instances of one prefab share it; the
    parent keeps them, so each instance is its own record."""
    parent, _, leaf = path.rpartition("/")
    label = f"{parent.rsplit('/', 1)[-1]}/{leaf}" if parent else leaf
    return parent, CLONE_RE.sub("", label)


def instance(path: str) -> str:
    """GameObject path -> the record it belongs to: its nearest ancestor that
    is a prefab instance or carries a sibling index, else its direct parent.
    Keyed by that node and its two ancestors only: the agent keeps a fixed
    number of names leaf upwards, so deeper texts of one instance lose more of
    the path's top."""
    parts = path.split("/")[:-1]
    for i in range(len(parts) - 1, max(len(parts) - 1 - INSTANCE_DEPTH, -1), -1):
        if INSTANCE_RE.search(parts[i]):
            return "/".join(parts[max(0, i - 2):i + 1])
    return "/".join(parts)


def leaves(value, path=""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from leaves(v, f"{path}.{k}" if path else k)
    elif isinstance(value, list):
        for v in value:
            yield from leaves(v, path + "[]")
    else:
        yield path, value


class TableIndex:
    """Decoded tables ({table: {key: row}}) indexed for matching."""

    def __init__(self, tables: dict[str, dict]):
        self.tables = tables
        self.cells = collections.defaultdict(list)  # string cell -> [(table, key, path)]
        self.num_cols = collections.defaultdict(set)  # (table, path) -> numbers
        self.str_cols = collections.defaultdict(set)  # (table, path) -> strings
        for tb, rows in tables.items():
            for k, row in rows.items():
                for p, v in leaves(row):
                    if isinstance(v, bool) or v is None:
                        continue
                    if isinstance(v, str):
                        if len(v) >= 3:
                            self.cells[v].append((tb, k, p))
                            self.str_cols[(tb, p)].add(v)
                    elif isinstance(v, (int, float)):
                        self.num_cols[(tb, p)].add(round(float(v), 6))
        self.num_index = collections.defaultdict(set)
        for col, vs in self.num_cols.items():
            for v in vs:
                self.num_index[v].add(col)
        self._numeric: dict = {}
        self._key_field: dict = {}

    def key_field(self, tb: str) -> str | None:
        """The top-level field that equals the row key in every row."""
        if tb not in self._key_field:
            rows = self.tables.get(tb) or {}
            first = next(iter(rows.values()), {})
            self._key_field[tb] = next((f for f in first if all(isinstance(r, dict) and r.get(f) == k
                                                                for k, r in rows.items())), None)
        return self._key_field[tb]

    def numeric(self, tb: str, k) -> list[tuple[str, float]]:
        if (tb, k) not in self._numeric:
            self._numeric[(tb, k)] = [(p, round(float(v), 6)) for p, v in leaves(self.tables[tb][k])
                                      if isinstance(v, (int, float)) and not isinstance(v, bool)]
        return self._numeric[(tb, k)]


class Evidence:
    """Accumulates traces; result() -> the evidence document."""

    def __init__(self, index: TableIndex):
        self.index = index
        self.traces: list[str] = []
        self.n_texts = self.n_records = 0
        self.captions = collections.defaultdict(collections.Counter)  # label -> Localization keys
        self.anchors = collections.defaultdict(lambda: {"labels": collections.Counter(), "records": set()})
        self.hits = collections.Counter()  # (table, path, label) -> records where it matched
        self.chances = collections.Counter()  # (table, path, label) -> records where it could
        self.positions = collections.defaultdict(collections.Counter)  # (table, path, label) -> (index, count, range)
        self.scales = collections.defaultdict(collections.Counter)  # (table, path, label) -> scale
        self.examples = collections.defaultdict(list)
        self.shown_nums = collections.defaultdict(set)  # label -> numbers shown (valueset)
        self.shown_strs = collections.defaultdict(set)
        self.state_hits = collections.Counter()  # (table, path, label) -> snapshots where it matched
        self.state_chances = collections.Counter()  # (table, path, label) -> snapshots where it could
        self.state_values = collections.defaultdict(set)  # (table, path, label) -> distinct matched values
        self.state_scales = collections.defaultdict(collections.Counter)
        self.state_examples = collections.defaultdict(list)
        self.state_seen: set = set()  # (label, table, key, value): a snapshot counts once
        self.n_states = 0

    def add(self, name: str, events: list[dict]) -> None:
        trace_no = len(self.traces)
        self.traces.append(name)
        texts, last_loc = [], None  # (instant, parent, label, text, rows read before it)
        pending = []  # rows read since the previous text: (t, table, key)
        states = []
        for e in events:
            if e.get("e") == "state":
                states.append(e)
            elif e.get("e") == "get" and e.get("tb") == "Localization":
                last_loc = (e["t"], e["k"])
            elif e.get("e") == "get" and e.get("tb") not in TEXT_TABLES and e.get("tb") in self.index.tables:
                pending.append((e["t"], e["tb"], e["k"]))
            elif e.get("e") == "txt":
                text = clean(e["v"])
                if not text or TIME_RE.search(text):
                    continue  # clocks change every second and match anything
                _, label = split_path(e["p"])
                parent = instance(e["p"])
                if last_loc and last_loc[0] == e["t"]:
                    self.captions[label][last_loc[1]] += 1
                instant = ("f", e["f"]) if e.get("f", -1) >= 0 else ("t", e["t"])
                reads = [(tb, k) for t, tb, k in pending if e["t"] - t <= READ_WINDOW_MS]
                pending = []
                texts.append((instant, parent, label, text, reads))
        self.n_texts += len(texts)
        thousands = detect_thousands(t[3] for t in texts)
        texts = [(*t, numbers(t[3], thousands)) for t in texts]
        for e in states:
            self._state(e, thousands)

        for _, _, label, text, _, nums in texts:
            if nums and len(re.sub(r"[\d.,\s/%:+\-~xX()]|Lv", "", text)) <= 3:  # a numeric display
                self.shown_nums[label].update(n for n in nums if abs(n) >= VALUESET_MIN_NUMBER)
            elif not nums:
                self.shown_strs[label].add(text)

        # records: same parent + same frame; a repeated label starts the next one
        records, current, reads, key = [], {}, [], None
        for instant, parent, label, text, before, nums in texts:
            if (instant, parent) != key or label in current:
                if current:
                    records.append((current, reads))
                current, reads, key = {}, [], (instant, parent)
            current[label] = (text, nums)
            reads += before
        if current:
            records.append((current, reads))
        self.n_records += len(records)
        for i, (record, reads) in enumerate(records):
            self._record((trace_no, i), record, reads)

    def _record(self, rid, record: dict[str, tuple[str, list[float]]], reads: list[tuple]) -> None:
        """record: label -> (text, numbers in display order); reads: rows read before it."""
        rows: dict[tuple, str | None] = {}  # (table, key) -> anchor label (None: read by key)
        for label, (text, _) in record.items():
            for core in cores(text):
                cells = self.index.cells.get(core, ())
                if not 0 < len({(tb, k) for tb, k, _ in cells}) <= MAX_CANDIDATE_ROWS:
                    continue
                for tb, k, p in cells:
                    rows[(tb, k)] = label
                    self.anchors[(tb, p)]["labels"][label] += 1
                    self.anchors[(tb, p)]["records"].add(rid)
        by_table = collections.defaultdict(set)
        for tb, k in reads:
            if k in self.index.tables[tb]:
                by_table[tb].add(k)
        for tb, keys in by_table.items():
            if len(keys) <= MAX_CANDIDATE_ROWS:
                for k in keys:
                    rows.setdefault((tb, k), None)
        if not rows:
            return
        for label, (text, nums) in record.items():
            wanted = {n for n in nums if abs(n) >= 2}  # 0/1 are in every row
            if not wanted:
                continue
            tried, matched = set(), set()
            for (tb, k), anchor_label in rows.items():
                for p, v in self.index.numeric(tb, k):
                    key = (tb, p, label)
                    tried.add(key)
                    if key in matched:
                        continue
                    scale = next((s for s in SCALES if round(v / s, 6) in wanted), None)
                    if scale is not None:
                        matched.add(key)
                        self.scales[key][scale] += 1
                        self.positions[key][(nums.index(round(v / scale, 6)), len(nums), "~" in text)] += 1
                        if len(self.examples[key]) < EXAMPLES:
                            via = "read" if anchor_label is None else f'"{record[anchor_label][0][:40]}"'
                            self.examples[key].append(f'{tb}[{k}] {via} -> "{text[:40]}"')
            for key in tried:
                self.chances[key] += 1
            for key in matched:
                self.hits[key] += 1

    def _valueset(self, label: str, shown: set, lookup) -> dict | None:
        """The one column holding every value `label` showed, if it stands out."""
        hits = collections.Counter()
        for v in shown:
            for col in lookup(v):
                hits[col] += 1
        full = [(len(vs), col) for col, n in hits.items() if n == len(shown)
                for vs in [self.index.num_cols.get(col) or self.index.str_cols.get(col)]]
        if not full:
            return None
        full.sort()
        card, (tb, p) = full[0]
        score = len(shown) - math.log2(card)
        runner_up = len(shown) - math.log2(full[1][0]) if len(full) > 1 else None
        if runner_up is not None and score - runner_up < 1:
            return None  # ambiguous
        return {"kind": "valueset", "table": tb, "path": p, "label": label, "values": len(shown),
                "distinct": card, "score": round(score, 1)}

    def context(self, label: str) -> list[str]:
        """Caption keys of the label and its siblings (same parent)."""
        prefix = label.rsplit("/", 1)[0] + "/"
        keys = [c.most_common(1)[0][0] for lab, c in self.captions.items() if lab == label or lab.startswith(prefix)]
        return sorted(set(keys))[:4]

    def _state(self, e: dict, thousands: str) -> None:
        """A component's plaintext values vs the columns of the exact row it holds."""
        tb = e.get("tb")
        rows = self.index.tables.get(tb)
        key_field = self.index.key_field(tb) if rows else None
        k = e.get("row", {}).get(key_field) if key_field else None
        if k is None or k not in rows:
            return
        self.n_states += 1
        row = rows[k]
        strings = [(p, v) for p, v in leaves(row) if isinstance(v, str) and len(v) >= 3]
        for path, value in leaves(e.get("v", {})):
            label = f"{e['cls']}.{path}"
            if (label, tb, k, value) in self.state_seen:
                continue
            self.state_seen.add((label, tb, k, value))
            if isinstance(value, str):
                text = clean(value)
                nums = numbers(text, thousands) if len(re.sub(r"[\d.,\s/%:+\-~xX()]|Lv", "", text)) <= 3 else []
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                text, nums = None, [round(float(value), 6)]
            else:
                continue
            # 0/1 are in every row; a fraction (a scale of 0.7) is distinctive enough with the row known
            wanted = {n for n in nums if abs(n) >= 2 or n != int(n)}
            candidates = []
            if wanted:
                candidates += [(p, v, next((s for s in SCALES if round(v / s, 6) in wanted), None))
                               for p, v in self.index.numeric(tb, k)]
            if text and len(text) >= 3 and not nums:
                candidates += [(p, v, 1 if v == text else None) for p, v in strings]
            for p, v, scale in candidates:
                key = (tb, p, label)
                self.state_chances[key] += 1
                if scale is None:
                    continue
                self.state_hits[key] += 1
                self.state_values[key].add(v)
                self.state_scales[key][scale] += 1
                if len(self.state_examples[key]) < EXAMPLES:
                    self.state_examples[key].append(f"{tb}[{k}] {v!r} == {label} {value!r}")

    def result(self) -> dict:
        columns = []
        for (tb, p), a in self.anchors.items():
            if len(a["records"]) >= MIN_RECORDS:
                columns.append({"kind": "anchor", "table": tb, "path": p, "label": a["labels"].most_common(1)[0][0],
                                "labels": a["labels"].most_common(4), "records": len(a["records"])})
        for key, n in self.hits.items():
            if n < MIN_RECORDS:
                continue
            tb, p, label = key
            (pos, count, ranged), _ = self.positions[key].most_common(1)[0]
            columns.append({"kind": "number", "table": tb, "path": p, "label": label, "records": n,
                            "chances": self.chances[key], "precision": round(n / self.chances[key], 3),
                            "position": pos, "numbers": count, "range": ranged,
                            "scale": self.scales[key].most_common(1)[0][0],
                            "captions": self.context(label), "examples": self.examples[key]})
        for label, shown in self.shown_nums.items():
            if len(shown) >= MIN_RECORDS and (found := self._valueset(label, shown, lambda v: self.index.num_index.get(v, ()))):
                found["captions"] = self.context(label)
                columns.append(found)
        str_cols = collections.defaultdict(set)
        for v, cells in self.index.cells.items():
            for tb, _, p in cells:
                str_cols[v].add((tb, p))
        for label, shown in self.shown_strs.items():
            if len(shown) >= MIN_RECORDS and (found := self._valueset(label, shown, lambda v: str_cols.get(v, ()))):
                found["captions"] = self.context(label)
                columns.append(found)
        for key, n in self.state_hits.items():
            if n < MIN_STATE_RECORDS or len(self.state_values[key]) < MIN_STATE_VALUES:
                continue
            tb, p, label = key
            columns.append({"kind": "state", "table": tb, "path": p, "label": label, "records": n,
                            "chances": self.state_chances[key], "precision": round(n / self.state_chances[key], 3),
                            "values": len(self.state_values[key]), "scale": self.state_scales[key].most_common(1)[0][0],
                            "examples": self.state_examples[key]})
        columns.sort(key=lambda c: (c["table"], c["path"], c["kind"]))
        return {"traces": self.traces, "texts": self.n_texts, "records": self.n_records, "states": self.n_states,
                "columns": columns}
