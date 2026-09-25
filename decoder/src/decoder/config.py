"""Load `decoder.toml`: catalog path, capture directory and wire framing.

Relative paths resolve against the config file's directory, matching the other
tools in this repo (``extract_protocol.toml`` et al.). The framing lives here so
that locking it in after reversing a capture is a config edit, not a code change.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .framing import Framing

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "decoder.toml"


@dataclass(frozen=True)
class Config:
    catalog_path: Path
    type_dump_path: Path | None
    pcap_dir: Path
    cache_dir: Path
    framing: Framing
    static_key: bytes


def _resolve(base: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else (base / candidate).resolve()


def _load_framing(config_path: Path, raw: dict[str, object]) -> Framing:
    if "framing" not in raw:
        raise ValueError(f"{config_path}: missing [framing] section")
    try:
        return Framing(**raw["framing"])
    except TypeError as err:
        raise ValueError(f"{config_path}: invalid [framing]: {err}") from err


def load_config(path: Path | None = None) -> Config:
    config_path = path or DEFAULT_CONFIG_PATH
    if not config_path.is_file():
        raise FileNotFoundError(f"config not found: {config_path}")
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    base = config_path.parent
    paths = raw.get("paths", {})
    crypto_cfg = raw.get("crypto", {})
    framing = _load_framing(config_path, raw)
    static_key = crypto_cfg.get("static_key", "").encode("utf-8")
    type_dump = paths.get("type_dump", "../resources/rom_dump.json")
    return Config(
        catalog_path=_resolve(
            base, paths.get("catalog", "../resources/protocol/messages_typed.json")
        ),
        type_dump_path=_resolve(base, type_dump) if type_dump else None,
        pcap_dir=_resolve(base, paths.get("pcap_dir", "./pcap")),
        cache_dir=_resolve(base, paths.get("cache_dir", "./.cache")),
        framing=framing,
        static_key=static_key,
    )
