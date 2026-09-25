"""Decode a decrypted, opcode-stripped message body against the catalog.

Body decoding reads primitives, ``String`` / ``Char[]`` and ``List<T>`` on its
own; given a :class:`~decoder.structs.StructCatalog` it also resolves enums,
fixed arrays, nested structs and inherited base fields. A field it still cannot
size is reported as ``unresolved`` and decoding stops, leaving the rest as hex.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .catalog import Message

if TYPE_CHECKING:
    from .structs import StructCatalog

# Message bodies are always little-endian, independent of the wire framing.
_BODY_BYTE_ORDER = "<"

# IL2CPP primitive type -> (struct format char, byte size). The byte-order
# character is prepended by the reader.
_PRIMITIVES: dict[str, tuple[str, int]] = {
    "System.Boolean": ("?", 1),
    "System.Byte": ("B", 1),
    "System.SByte": ("b", 1),
    "System.Int16": ("h", 2),
    "System.UInt16": ("H", 2),
    "System.Int32": ("i", 4),
    "System.UInt32": ("I", 4),
    "System.Int64": ("q", 8),
    "System.UInt64": ("Q", 8),
    "System.Single": ("f", 4),
    "System.Double": ("d", 8),
}


@dataclass
class DecodedField:
    name: str
    type: str
    value: object | None = None
    unresolved: bool = False


_LIST_PREFIX = "System.Collections.Generic.List<"


class _ShortBody(Exception):
    """A read ran past the end of the body."""


class _Unresolved(Exception):
    """A field type cannot be decoded without extra catalog data (level 2)."""

    def __init__(self, type_name: str) -> None:
        super().__init__(type_name)
        self.type = type_name


class _Reader:
    """Little-endian cursor over a decrypted, opcode-stripped body."""

    def __init__(self, buf: bytes) -> None:
        self.buf = buf
        self.pos = 0

    def _need(self, n: int) -> None:
        if self.pos + n > len(self.buf):
            raise _ShortBody

    def primitive(self, fmt_char: str, size: int) -> object:
        self._need(size)
        (value,) = struct.unpack_from(_BODY_BYTE_ORDER + fmt_char, self.buf, self.pos)
        self.pos += size
        return value

    def u32(self) -> int:
        return self.primitive("I", 4)  # type: ignore[return-value]

    def string(self) -> str:
        """System.String: u32 char count followed by UTF-16LE code units."""
        count = self.u32()
        self._need(count * 2)
        raw = self.buf[self.pos : self.pos + count * 2]
        self.pos += count * 2
        return raw.decode("utf-16-le")

    def char_array(self) -> str:
        """System.Char[]: NUL-terminated UTF-16LE, no length prefix."""
        start = self.pos
        while True:
            self._need(2)
            unit = self.buf[self.pos : self.pos + 2]
            self.pos += 2
            if unit == b"\x00\x00":
                return self.buf[start : self.pos - 2].decode("utf-16-le")


def _read_value(
    reader: _Reader,
    ftype: str,
    structs: StructCatalog | None = None,
    owner: str | None = None,
    fname: str | None = None,
) -> object:
    """Read one field value, or raise ``_Unresolved`` for an undecodable type.

    Without ``structs`` only the self-describing types decode (primitives,
    ``String``, ``Char[]``, ``List<self-describing>``). With ``structs`` this also
    resolves enums (as their underlying int), fixed-size arrays, nested structs
    and lists of structs, recursing base-class-first.
    """

    primitive = _PRIMITIVES.get(ftype)
    if primitive is not None:
        return reader.primitive(*primitive)
    if ftype == "System.String":
        return reader.string()
    if ftype == "System.Char[]":
        return reader.char_array()
    if ftype.startswith(_LIST_PREFIX) and ftype.endswith(">"):
        inner = ftype[len(_LIST_PREFIX) : -1]
        count = reader.u32()
        return [_read_value(reader, inner, structs, owner, fname) for _ in range(count)]

    if structs is not None:
        underlying = structs.enum_underlying(ftype)
        if underlying is not None:
            return _read_value(reader, underlying, structs, owner, fname)
        if ftype.endswith("[]"):
            size = structs.array_size(owner, fname)
            if size is None:
                raise _Unresolved(ftype)
            element = ftype[:-2]
            return [
                _read_value(reader, element, structs, owner, fname) for _ in range(size)
            ]
        layout = structs.layout(ftype)
        if layout is not None:
            if structs.is_message_element(ftype):
                reader.u32()  # message-as-element carries its opcode prefix
            return {
                sub_name: _read_value(reader, sub_type, structs, ftype, sub_name)
                for sub_name, sub_type in layout
            }

    raise _Unresolved(ftype)


def decode_body(
    message: Message,
    body: bytes,
    structs: StructCatalog | None = None,
) -> tuple[list[DecodedField], str | None]:
    """Decode ``body`` against ``message`` fields, best-effort.

    Decodes fields until one cannot be sized (an undecodable type) or the body
    runs out, returning the decoded fields and an optional note explaining the
    stop. With ``structs``, inherited base-class fields (e.g. the ``__state__``
    header byte on ``*List`` messages) are prepended and nested types resolve.
    """

    owner = f"Protocol.{message.name}"
    prefix = structs.base_prefix(owner) if structs is not None else ()
    fields: list[tuple[str, str]] = [
        *prefix,
        *((spec.name, spec.type) for spec in message.fields),
    ]

    reader = _Reader(body)
    decoded: list[DecodedField] = []
    for spec_name, spec_type in fields:
        mark = reader.pos
        try:
            value = _read_value(reader, spec_type, structs, owner, spec_name)
        except _Unresolved as unresolved:
            reader.pos = mark
            decoded.append(
                DecodedField(name=spec_name, type=spec_type, unresolved=True)
            )
            return decoded, (
                f"unresolved field type {unresolved.type}; remaining fields skipped"
            )
        except _ShortBody:
            reader.pos = mark
            return decoded, f"body too short at field {spec_name}"
        decoded.append(DecodedField(name=spec_name, type=spec_type, value=value))
    return decoded, None
