"""UI traces -> evidence of which table column feeds which UI label.

A trace (rom-frida's trace_ui_text.js, one JSON object per line) records every
text the UI displays (string, GameObject path, time) and the lazy-table rows
it reads; Localization reads give the caption keys. Traces hold values and UI
paths, not obfuscated names, so they stay valid across client builds: they are
matched against the tables decoded for the current build.

Two ways a column is tied to a label, counted over ALL traces:

anchor    texts shown together (same parent GameObject, same instant) form a
          record; a text equal to a string cell identifies the row ("Dragon
          Harbor" -> Map_Data[108]). Every number in the record that equals a
          field of THAT row votes for (column <- label). Precision = votes /
          records where the row was identified and the label showed a number:
          the real source is near 100%, coincidences are low.
valueset  a label that showed several distinct numbers (>= 10) or strings is
          matched against every column; the column holding all of them with
          the fewest distinct values of its own wins. No row context, so weaker.
"""

from __future__ import annotations

import collections
import json
import math
import re
from pathlib import Path

MIN_RECORDS = 3  # records (or distinct values) a finding needs to be reported
MAX_CANDIDATE_ROWS = 8  # an anchor text found in more rows than this is too generic
VALUESET_MIN_NUMBER = 10  # smaller numbers (counters, pins) are in every column
EXAMPLES = 3

TAG_RE = re.compile(r"<[^>]{1,40}>")
NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)*")
CLONE_RE = re.compile(r"\(Clone\)|\(\d+\)")
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
    without (Clone) markers, so instances of one prefab share it."""
    parent, _, leaf = path.rpartition("/")
    label = f"{parent.rsplit('/', 1)[-1]}/{leaf}" if parent else leaf
    return parent, CLONE_RE.sub("", label)


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
        self.examples = collections.defaultdict(list)
        self.shown_nums = collections.defaultdict(set)  # label -> numbers shown (valueset)
        self.shown_strs = collections.defaultdict(set)

    def add(self, name: str, events: list[dict]) -> None:
        trace_no = len(self.traces)
        self.traces.append(name)
        texts, last_loc = [], None
        for e in events:
            if e.get("e") == "get" and e.get("tb") == "Localization":
                last_loc = (e["t"], e["k"])
            elif e.get("e") == "txt":
                text = clean(e["v"])
                if not text or TIME_RE.search(text):
                    continue  # clocks change every second and match anything
                parent, label = split_path(e["p"])
                if last_loc and last_loc[0] == e["t"]:
                    self.captions[label][last_loc[1]] += 1
                texts.append((e["t"], parent, label, text))
        self.n_texts += len(texts)
        thousands = detect_thousands(t[3] for t in texts)

        for _, _, label, text in texts:
            nums = numbers(text, thousands)
            if nums and len(re.sub(r"[\d.,\s/%:+\-~xX()]|Lv", "", text)) <= 3:  # a numeric display
                self.shown_nums[label].update(n for n in nums if abs(n) >= VALUESET_MIN_NUMBER)
            elif not nums:
                self.shown_strs[label].add(text)

        # records: same parent + same instant; a repeated label starts the next one
        records, current, key = [], {}, None
        for t, parent, label, text in texts:
            if (t, parent) != key or label in current:
                if current:
                    records.append(current)
                current, key = {}, (t, parent)
            current[label] = text
        if current:
            records.append(current)
        self.n_records += len(records)
        for i, record in enumerate(records):
            self._record((trace_no, i), record, thousands)

    def _record(self, rid, record: dict[str, str], thousands: str) -> None:
        rows: dict[tuple, str] = {}  # (table, key) -> anchor label
        for label, text in record.items():
            for core in cores(text):
                cells = self.index.cells.get(core, ())
                if not 0 < len({(tb, k) for tb, k, _ in cells}) <= MAX_CANDIDATE_ROWS:
                    continue
                for tb, k, p in cells:
                    rows[(tb, k)] = label
                    self.anchors[(tb, p)]["labels"][label] += 1
                    self.anchors[(tb, p)]["records"].add(rid)
        if not rows:
            return
        for label, text in record.items():
            nums = numbers(text, thousands)
            wanted = {n for n in nums if abs(n) >= 2}  # 0/1 are in every row
            if not wanted:
                continue
            tried, matched = set(), set()
            for (tb, k), anchor_label in rows.items():
                for p, v in self.index.numeric(tb, k):
                    key = (tb, p, label)
                    tried.add(key)
                    if v in wanted and key not in matched:
                        matched.add(key)
                        self.positions[key][(nums.index(v), len(nums), "~" in text)] += 1
                        if len(self.examples[key]) < EXAMPLES:
                            self.examples[key].append(f'{tb}[{k}] "{record[anchor_label][:40]}" -> "{text[:40]}"')
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
        columns.sort(key=lambda c: (c["table"], c["path"], c["kind"]))
        return {"traces": self.traces, "texts": self.n_texts, "records": self.n_records, "columns": columns}
