"""Load the client's protocol catalog and index it by opcode.

The catalog is the typed message list emitted by
``client/exporters/extract_protocol.py`` — a JSON array of messages, each with a
32-bit opcode (``id``), a direction (``C2S`` / ``S2C``) and its ordered typed
fields. Opcodes are per-message hashes, so they are unique across both
directions; we index the whole catalog by ``id`` for O(1) frame decoding.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MessageField:
    """One ordered field of a message: its name and IL2CPP type string."""

    name: str
    type: str


@dataclass(frozen=True)
class Message:
    """A single protocol message definition, keyed on its opcode ``id``."""

    name: str
    dir: str  # "C2S" or "S2C"
    id: int
    id_hex: str
    fields: list[MessageField] = field(default_factory=list)


@dataclass
class Catalog:
    """All messages, indexed by opcode."""

    by_opcode: dict[int, Message]

    @classmethod
    def load(cls, catalog_path: Path) -> Catalog:
        if not catalog_path.is_file():
            raise FileNotFoundError(
                f"protocol catalog not found: {catalog_path}. Run "
                "`python3 -m exporters.extract_protocol` in client/ first."
            )
        raw = json.loads(catalog_path.read_text(encoding="utf-8"))
        by_opcode: dict[int, Message] = {}
        for entry in raw:
            fields = [
                MessageField(name=f["name"], type=f["type"])
                for f in entry.get("fields", [])
            ]
            message = Message(
                name=entry["name"],
                dir=entry["dir"],
                id=int(entry["id"]),
                id_hex=entry["id_hex"],
                fields=fields,
            )
            by_opcode[message.id] = message
        logger.info("loaded %d messages from %s", len(by_opcode), catalog_path)
        return cls(by_opcode=by_opcode)

    def lookup(self, opcode: int) -> Message | None:
        return self.by_opcode.get(opcode)
