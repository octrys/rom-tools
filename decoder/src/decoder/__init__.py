"""pcap analyzer/decoder for the ROM: Golden Age network protocol.

Reassembles TCP streams from captured pcaps, splits them into protocol frames,
and decodes each frame against the message catalog produced by the client
exporter (``resources/protocol/messages_typed.json``). A small FastAPI app lets
you pick a capture and inspect the decoded messages in the browser.
"""

from __future__ import annotations

from .capture import HalfStream, read_half_streams
from .catalog import Catalog, Message, MessageField
from .crypto import Rabbit, derive_key
from .session import Session, decode_sessions
from .structs import StructCatalog

__all__ = [
    "Catalog",
    "HalfStream",
    "Message",
    "MessageField",
    "Rabbit",
    "Session",
    "StructCatalog",
    "decode_sessions",
    "derive_key",
    "read_half_streams",
]
