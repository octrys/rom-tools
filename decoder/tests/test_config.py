"""``[framing]`` in decoder.toml is the single source of truth for wire framing."""

from __future__ import annotations

from pathlib import Path

import pytest

from decoder.config import load_config
from decoder.framing import Framing

_FRAMING_TOML = """
[framing]
length_size = 2
length_endian = "little"
length_includes_header = true
opcode_size = 0
opcode_endian = "little"
"""


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "decoder.toml"
    path.write_text(content, encoding="utf-8")
    return path


def test_load_config_reads_framing(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, _FRAMING_TOML))

    assert config.framing == Framing(
        length_size=2,
        length_endian="little",
        length_includes_header=True,
        opcode_size=0,
        opcode_endian="little",
    )


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("[paths]\n", "missing \\[framing\\]"),
        (_FRAMING_TOML + "bogus = 1\n", "invalid \\[framing\\]"),
        ("[framing]\nlength_size = 2\n", "invalid \\[framing\\]"),
    ],
)
def test_load_config_rejects_bad_framing(
    tmp_path: Path, content: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        load_config(_write(tmp_path, content))
