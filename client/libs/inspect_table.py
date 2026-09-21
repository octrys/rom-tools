"""Print a table's schema fields with sample runtime values, to drive naming.

Dev helper, run as a module from the client dir:
    python3 -m libs.inspect_table Map_Data
"""

import json
import sys
from pathlib import Path

from .table_schema import parse_dump

# client/libs/inspect_table.py -> resources/ is a sibling of client/.
RESOURCES = Path(__file__).resolve().parents[2] / "resources"

PRIMS = {"System.Int32", "System.String", "System.Boolean", "System.Single",
         "System.Int64", "System.Byte", "System.Double"}


def main() -> None:
    table = sys.argv[1]
    schema = parse_dump(RESOURCES / "rom_dump.cs")
    dump = json.load(open(RESOURCES / "tables_runtime.json", encoding="utf-8"))
    rows = [e["row"] for e in dump[table]["rows"] if isinstance(e.get("row"), dict)]
    row_type = schema.table_rows[table]
    print(f"# {table}  rows={len(rows)}  row_type={row_type}")

    def samples(field):
        vals = []
        for r in rows[:60]:
            if field in r:
                v = r[field]
                if isinstance(v, (dict, list)):
                    v = json.dumps(v, ensure_ascii=False)[:40]
                vals.append(v)
        uniq = list(dict.fromkeys(map(str, vals)))
        return uniq[:6]

    for fname, ftype in schema.types[row_type].fields:
        short = ftype.rsplit(".", 1)[-1]
        kind = "enum" if ftype in schema.enums else ("struct" if ftype in schema.types and ftype not in PRIMS else short)
        print(f"  {fname:14} {kind:12} {samples(fname)}")


if __name__ == "__main__":
    main()
