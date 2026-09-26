"""datatables xref: type tracking over hand-assembled x64 (needs capstone)."""

import pytest

pytest.importorskip("capstone")

from datatables import xref  # noqa: E402
from datatables.code import Image, Model  # noqa: E402
from datatables.dump import parse  # noqa: E402

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
}

// Assembly-CSharp
class Holder : System.Object
{
    Row m_sRow; // 0x78
    System.Int32 get_Second(); // 0x00001000
    static System.Int32 Upper(Row r); // 0x00001010
}
"""

CODE = {
    0x1000: bytes.fromhex("8b817c000000" "c3"),  # mov eax, [rcx+0x7c] ; ret  -> m_sRow.BBBBBBBBBBB
    0x1010: bytes.fromhex("488b01" "48c1e820" "c3"),  # mov rax, [rcx] ; shr rax, 32 ; ret -> dword at +4
}


@pytest.fixture()
def result(tmp_path):
    cs = tmp_path / "rom_dump.cs"
    cs.write_text(DUMP_CS, encoding="utf-8")
    data = bytearray(b"\xcc" * 0x1100)
    for rva, code in CODE.items():
        data[rva:rva + len(code)] = code
    image = Image.__new__(Image)
    image.data, image.init, image.slots = bytes(data), 0, {}
    return xref.build(Model(cs), image, parse(cs), workers=1)


def readers(result, field):
    return {r["m"]: r for r in result["fields"]["Row"][field]["top"]}


def test_getter_names_the_embedded_row_field(result):
    r = readers(result, "BBBBBBBBBBB")["Holder::get_Second"]
    assert r["how"] == ["read"]
    assert r["getter"] == "second"


def test_shr32_reads_the_next_dword(result):
    assert "Holder::Upper" in readers(result, "BBBBBBBBBBB")
    assert "AAAAAAAAAAA" not in result["fields"]["Row"]
