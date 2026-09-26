"""datatables.toml -> Config. Relative paths resolve against the TOML's directory."""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

DEFAULT_PATH = Path(__file__).with_name("datatables.toml")


@dataclass
class Limits:
    """Search limits for layout inference ([infer])."""

    sample_rows: int = 24
    max_rounds: int = 12
    node_budget: int = 400_000
    time_budget: int = 60
    max_skips: int = 6
    max_element_layouts: int = 64


@dataclass
class Config:
    bundle: Path
    dump_cs: Path
    runtime: Path
    image: Path
    slots: Path
    output: Path
    names: Path
    enum_names: bool
    apply_names: str
    limits: Limits

    @property
    def layouts(self) -> Path:
        return self.output / "layouts.json"

    @property
    def traces(self) -> Path:
        return self.output / "traces"

    @property
    def evidence(self) -> Path:
        return self.output / "evidence.json"

    @property
    def xref(self) -> Path:
        return self.output / "xref.json"

    @property
    def schema(self) -> Path:
        return self.output / "schema.json"

    @property
    def tables(self) -> Path:
        return self.output / "tables"


def load(path: Path = DEFAULT_PATH) -> Config:
    if not path.is_file():
        sys.exit(f"config not found: {path}")
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    def resolve(key: str) -> Path:
        value = Path(raw["paths"][key]).expanduser()
        return value if value.is_absolute() else (path.parent / value).resolve()

    known = {f.name for f in fields(Limits)}
    unknown = set(raw.get("infer", {})) - known
    if unknown:
        sys.exit(f"{path}: unknown [infer] keys: {', '.join(sorted(unknown))}")
    decode = raw.get("decode", {})
    apply_names = decode.get("names", "confirmed")
    if apply_names not in ("confirmed", "all", "none"):
        sys.exit(f"{path}: [decode].names must be confirmed, all or none (got {apply_names!r})")
    return Config(
        bundle=resolve("bundle"),
        dump_cs=resolve("dump_cs"),
        runtime=resolve("runtime"),
        image=resolve("image"),
        slots=resolve("slots"),
        output=resolve("output"),
        names=resolve("names"),
        enum_names=decode.get("enum_names", False),
        apply_names=apply_names,
        limits=Limits(**raw.get("infer", {})),
    )
