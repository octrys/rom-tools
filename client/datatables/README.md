# datatables

Decodes the game-data tables **offline**, straight from the patch bundle
(`tablecrypto.unity`), into JSON — every row of every table, with English
text. The frida dumps are needed once per build, to learn each
table's binary layout; decoding afterwards needs only the bundle.

```bash
cd client
uv run --with 'UnityPy>=1.25' python3 -m datatables infer    # learn layouts (~75 s, once per build)
uv run --with 'UnityPy>=1.25' python3 -m datatables verify   # layouts + text tables vs the runtime dump
uv run --with 'UnityPy>=1.25' python3 -m datatables decode   # bundle -> tables/<Table>.json
uv run --with 'UnityPy>=1.25' python3 -m datatables schema   # annotated column schema
uv run --with 'UnityPy>=1.25' python3 -m datatables uitrace [TRACE ...]  # UI traces -> evidence.json
uv run --with 'UnityPy>=1.25' python3 -m datatables suggest  # evidence -> names.toml suggestions
```

`infer` / `verify` / `decode` take table names to limit them (`verify Buff
ItemInfo`); every command takes `--config <toml>`. Needs
Python 3.11+ and [UnityPy](https://github.com/K0lb3/UnityPy) (`requirements.txt`;
`uv run --with` avoids installing it); peaks around 600 MB of RAM.

## Configuration — [`datatables.toml`](datatables.toml)

| Key | |
|---|---|
| `[paths].bundle` | `tablecrypto.unity` from the patch |
| `[paths].dump_cs` | `rom_dump.cs` — rom-frida's `dump_client.js` (row types, enums) |
| `[paths].runtime` | `tables_runtime.json` — rom-frida's `dump_tables.js` (ground truth for `infer` / `verify` / `schema`) |
| `[paths].output` | where everything is written (default `resources/datatables/`) |
| `[paths].names` | [`names.toml`](names.toml) — curated field names, committed |
| `[decode].enum_names` | enum values as member names (`BT_BUFF`, flag sets `A\|B`) instead of ints |
| `[decode].names` | rename fields from `names.toml`: `confirmed` entries, `all` (suggestions too), or `none` |
| `[infer]` | search limits; raise `node_budget` / `time_budget` for a stubborn table |

**`dump_cs` and `runtime` must come from the same build as each other**:
obfuscated names change every build.

## Output — `[paths].output`

- `layouts.json` — per table: the asset and its row layout (`infer`). Saved
  after every table, so an interrupted run keeps its progress.
- `tables/<Table>.json` — `{key: row}` for the 120 row tables, plus
  `LocalName` (id → text) and `Localization` (UI key → text) (`decode`).
- `schema.json` — every column with type, offset and hints: `id`, `enum`,
  `fk <Table>`, `asset <Prefix>*`, `text`, `datetime`, `sharedWith` (`schema`).
- `traces/` — imported UI traces, kept across runs; `evidence.json` — what
  `uitrace` matched in them.

## Naming fields — `uitrace`, `suggest`, [`names.toml`](names.toml)

Field names are obfuscated, random per build and not reversible. But one
original identifier maps to one obfuscated name everywhere in a build
(`DNIEOGFIAHC` is the row id in 97 tables), so **the unit of naming is the
identifier**: naming `NOHLEFCNDPA` once names it in all 49 tables that use it.

**Evidence from the UI.** rom-frida's `trace_ui_text.js` records every text the
UI displays, with its GameObject path (plaintext, e.g. `Btn_Field/Text_WorldLevel`)
and frame, every table row the game reads by key, and the Localization caption
keys. Play, open the windows whose tables you want named, and feed the traces
to `uitrace`. It keeps every trace in `traces/` and analyses all of them
together, so evidence accumulates across sessions:

- **anchor**: texts of one instance (a list cell, a map icon — else one
  GameObject) shown in one frame form a record. Its candidate rows are the
  rows with a string cell equal to one of its texts (`"Dragon Harbor"` →
  `Map_Data[108]`) and the rows read right before its texts (the game reads
  row *i*, then fills cell *i*). The numbers shown are matched against those
  rows only (`"(Lv 95~97)"` → `BAEHJKGLEPM.AIBAPEMMMEO` = 95), as is or ÷1000
  — rates are stored in thousandths (`AbilityOption.PPABCBFBBIN` 20000 →
  `"+20%"`; the finding's `scale`). Precision (matches / chances) separates
  the source (~100%) from coincidences and from rows read by game logic.
- **state**: live game components that keep a table row in a field
  (`CMapManager.m_sMapData`, the world map window, monster/NPC actors) are
  snapshotted with their plaintext fields (`m_vMapSize`, `m_nMapID`, the text
  of `m_txtTitle`…). The row is known by its key, so each value is compared
  with that row only; a column needs 2+ snapshots with 2+ distinct values.
  The draft is the game's own field name (`m_vMapSize.m_X` →
  `MFGNLFAOAGB.BPFBMIDLJKI` gives `x`, and `mapSize` for the struct), weighted
  above UI labels.
- **valueset**: a label that showed several distinct values is matched against
  every column; the one holding all of them, with the fewest values of its
  own, wins (e.g. teleport costs → `Warp_Data.JAAKNNEDJJM.NOHLEFCNDPA`).

Traces store values and UI paths, not obfuscated names, so they stay valid
after a client update: `uitrace` re-matches them against the new tables.

**Curating.** `suggest` turns the evidence (plus row keys) into one entry per
identifier in `names.toml`, with a draft name, a score and the evidence lines.
Review them, fix the name, and set `status = "confirmed"`; confirmed entries are
never changed by the tool and are what `decode` applies by default.

Weak evidence is left out: number matches below 60% precision; string (anchor)
matches with fewer than 10 records, or whose top label holds less than half of
them — a string that shows up on unrelated screens is a coincidence. Drafts
come from the UI label; one that names nothing (`Text_2`, `Text_Off`,
`Mask/Text_Info`) becomes `value` for a number column and is dropped
otherwise. Drafts are a starting point: the label is the UI's name for the
widget, not the column's (`Text_Area` showed map names → confirmed as
`mapName`).

**Across builds.** Each entry has an `anchor`: its declaration position from a
table's row type (`Warp_Data/4/2` = row field #4, its field #2). Table names
are plaintext and declaration order survives rebuilds, so after an update
`suggest` re-keys every entry to its new obfuscated name — and reports any
whose anchor no longer resolves.

```bash
# rom-frida (Windows): python spawn.py trace_ui_text.js  -> storage/ui_trace_<stamp>.jsonl
uv run --with 'UnityPy>=1.25' python3 -m datatables uitrace /mnt/c/.../rom-frida/storage/ui_trace_*.jsonl
uv run --with 'UnityPy>=1.25' python3 -m datatables suggest
# edit names.toml: confirm entries, then
uv run --with 'UnityPy>=1.25' python3 -m datatables decode
```

## How it works

Each table is two TextAssets: `C_<Name>_head` (i32 count, then `(i32 key, i32
offset, i32 length)` per row) and `C_<Name>` (the rows: little-endian,
BinaryWriter style — i32, 7-bit-length-prefixed UTF-8, 1-byte bools).
Per-language tables are `C_<Name>_<Language>`; only `English` is read (the
runtime dump is English too). The text tables need no layout:
`LocalName` rows are `(i32 id, str)`, `Localization` is `i32 count` + `(str key,
str text)` pairs.

Every table has a hand-written loader, and they don't agree on a format.
Conventions found so far ([`codec.py`](codec.py) lists them as encodings):

- fields out of declaration order; some read and discarded, some computed
  after load (absent from the binary)
- strings as LocalName ids (unknown id → the id as text), ints as strings and
  strings as ints
- floats as thousandths in an `i32` (`1166` → `1.166`), ints as `f32`
- empty cells written as `0` / `"0"` / `[0]`
- fixed arrays without a count; fixed-capacity lists whose empty slots (or
  slots whose key field is 0) the loader drops
- flag sets as lists: bit numbers (`[2,5]` → `0b10010`) or OR-ed values
- tagged slots: per slot an `i32` tag picks which of two lists gets the element

So layouts are **inferred** ([`infer.py`](infer.py)): a depth-first search
tries, token by token, which runtime field in which encoding decodes to the
runtime value in a sample of rows at once. A candidate layout is then checked
on every row, and any row it gets wrong joins the sample for the next round.

## Caveats

- Re-run `infer` after a client update (new obfuscated names) or a data patch
  that changes a table's columns; `verify` tells you which tables broke. Then
  `suggest`, to re-key `names.toml` to the new names.
- A table that fails `infer` usually means a new loader convention: compare the
  raw row bytes with the runtime value before raising the search limits.
- Fields holding the same value in every row can't be told apart by their
  values, so two of them can end up with each other's names. The decoded
  values themselves are still right.
- `["x", …]` layout tokens are values in the binary that the runtime row
  doesn't show; they're read and dropped.
- The runtime `LocalName` has one key that isn't in the bundle (text sent by
  the server); `verify` reports it and still passes.
