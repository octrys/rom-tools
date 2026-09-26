"""datatables schema: naming status per column, from names.toml."""

from datatables.dump import parse
from datatables.names import Entry
from datatables.schema import build

DUMP_CS = """\
// Assembly-CSharp
class Tbl : Base<System.Int32,Row>
{
    static System.String NAME = "Tbl";
}

// Assembly-CSharp
struct Row : System.ValueType
{
    System.Int32 AAAAAAAAAAA; // 0x10
    System.Int32 BBBBBBBBBBB; // 0x14
    System.Int32 CCCCCCCCCCC; // 0x18
    System.Int32 plainField; // 0x1c
}
"""

RUNTIME = {"Tbl": {"count": 2, "rows": [
    {"key": 1, "row": {"AAAAAAAAAAA": 1, "BBBBBBBBBBB": 7, "CCCCCCCCCCC": 0, "plainField": 3}},
    {"key": 2, "row": {"AAAAAAAAAAA": 2, "BBBBBBBBBBB": 9, "CCCCCCCCCCC": 0, "plainField": 4}},
]}}


def test_columns_carry_name_and_naming_status(tmp_path):
    cs = tmp_path / "rom_dump.cs"
    cs.write_text(DUMP_CS, encoding="utf-8")
    entries = {"AAAAAAAAAAA": Entry("index", "confirmed"), "BBBBBBBBBBB": Entry("capacity", "tentative")}
    schema, stats, _ = build(parse(cs), RUNTIME, entries)
    cols = {c["field"]: c for c in schema["Tbl"]["fields"]}
    assert (cols["AAAAAAAAAAA"]["name"], cols["AAAAAAAAAAA"]["naming"]) == ("index", "confirmed")
    assert (cols["BBBBBBBBBBB"]["name"], cols["BBBBBBBBBBB"]["naming"]) == ("capacity", "tentative")
    assert "name" not in cols["CCCCCCCCCCC"] and cols["CCCCCCCCCCC"]["naming"] == "obfuscated"
    assert cols["plainField"]["naming"] == "plaintext"
    assert schema["Tbl"]["naming"] == {"confirmed": 1, "tentative": 1, "obfuscated": 1, "plaintext": 1}
    assert stats["naming:obfuscated"] == 1
