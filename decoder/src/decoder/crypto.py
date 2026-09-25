"""Rabbit stream cipher and body-key derivation for the ROM transport.

Ported from the reference server implementation
(``rom-server-poc/internal/transport``). The transport uses Rabbit (eSTREAM /
RFC 4503): a 128-bit key with an optional 64-bit IV producing a keystream XORed
with each frame body. The body key is derived from key halves the server sends
*in the clear* in the handshake, so a passive capture can be decrypted without
the client:

    rawKey  = LE64(encryptionKeyLow) || LE64(encryptionKeyHigh)   # 16 bytes
    bodyKey = AES-128-ECB-decrypt(staticKey, rawKey)

Two cipher instances run per session, both keyed with ``bodyKey`` but seeded with
different IVs: one for the client-send direction, one for the server-send.
"""

from __future__ import annotations

from Crypto.Cipher import AES

_MASK32 = 0xFFFFFFFF

# RFC 4503 counter-increment constants.
_RABBIT_A = (
    0x4D34D34D,
    0xD34D34D3,
    0x34D34D34,
    0x4D34D34D,
    0xD34D34D3,
    0x34D34D34,
    0x4D34D34D,
    0xD34D34D3,
)


def _rotl32(value: int, count: int) -> int:
    return ((value << count) | (value >> (32 - count))) & _MASK32


class Rabbit:
    """RFC 4503 Rabbit keystream generator."""

    def __init__(self, key: bytes, iv: bytes | None = None) -> None:
        if len(key) != 16:
            raise ValueError(f"Rabbit key must be 16 bytes, got {len(key)}")
        k = [key[2 * i] | (key[2 * i + 1] << 8) for i in range(8)]  # 8x 16-bit LE
        self._x = [0] * 8
        self._c = [0] * 8
        self._b = 0
        self._buf = bytearray()

        for j in range(8):
            if j % 2 == 0:
                self._x[j] = ((k[(j + 1) % 8] << 16) | k[j]) & _MASK32
                self._c[j] = ((k[(j + 4) % 8] << 16) | k[(j + 5) % 8]) & _MASK32
            else:
                self._x[j] = ((k[(j + 5) % 8] << 16) | k[(j + 4) % 8]) & _MASK32
                self._c[j] = ((k[j] << 16) | k[(j + 1) % 8]) & _MASK32

        for _ in range(4):
            self._next()
        for j in range(8):
            self._c[j] ^= self._x[(j + 4) % 8]

        if iv is not None:
            if len(iv) != 8:
                raise ValueError(f"Rabbit IV must be 8 bytes, got {len(iv)}")
            i0 = int.from_bytes(iv[0:4], "little")
            i1 = int.from_bytes(iv[4:8], "little")
            hi = (((i1 >> 16) << 16) | (i0 >> 16)) & _MASK32
            lo = (((i1 & 0xFFFF) << 16) | (i0 & 0xFFFF)) & _MASK32
            self._c[0] ^= i0
            self._c[1] ^= hi
            self._c[2] ^= i1
            self._c[3] ^= lo
            self._c[4] ^= i0
            self._c[5] ^= hi
            self._c[6] ^= i1
            self._c[7] ^= lo
            for _ in range(4):
                self._next()

    @staticmethod
    def _g(u: int, v: int) -> int:
        s = (u + v) & _MASK32
        sq = s * s
        return (sq ^ (sq >> 32)) & _MASK32

    def _next(self) -> None:
        b = self._b
        for j in range(8):
            total = self._c[j] + _RABBIT_A[j] + b
            wrapped = total & _MASK32
            b = 1 if wrapped < self._c[j] else 0
            self._c[j] = wrapped
        g = [self._g(self._x[j], self._c[j]) for j in range(8)]
        self._x[0] = (g[0] + _rotl32(g[7], 16) + _rotl32(g[6], 16)) & _MASK32
        self._x[1] = (g[1] + _rotl32(g[0], 8) + g[7]) & _MASK32
        self._x[2] = (g[2] + _rotl32(g[1], 16) + _rotl32(g[0], 16)) & _MASK32
        self._x[3] = (g[3] + _rotl32(g[2], 8) + g[1]) & _MASK32
        self._x[4] = (g[4] + _rotl32(g[3], 16) + _rotl32(g[2], 16)) & _MASK32
        self._x[5] = (g[5] + _rotl32(g[4], 8) + g[3]) & _MASK32
        self._x[6] = (g[6] + _rotl32(g[5], 16) + _rotl32(g[4], 16)) & _MASK32
        self._x[7] = (g[7] + _rotl32(g[6], 8) + g[5]) & _MASK32
        self._b = b

    def keystream(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._next()
            s0 = self._x[0] ^ (self._x[5] >> 16) ^ ((self._x[3] << 16) & _MASK32)
            s1 = self._x[2] ^ (self._x[7] >> 16) ^ ((self._x[5] << 16) & _MASK32)
            s2 = self._x[4] ^ (self._x[1] >> 16) ^ ((self._x[7] << 16) & _MASK32)
            s3 = self._x[6] ^ (self._x[3] >> 16) ^ ((self._x[1] << 16) & _MASK32)
            self._buf += (
                (s0 & _MASK32).to_bytes(4, "little")
                + (s1 & _MASK32).to_bytes(4, "little")
                + (s2 & _MASK32).to_bytes(4, "little")
                + (s3 & _MASK32).to_bytes(4, "little")
            )
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def crypt(self, body: bytes) -> bytes:
        """XOR ``body`` with keystream, advancing to the next 16-byte boundary.

        Each frame consumes ``ceil(len/16)*16`` keystream bytes so the following
        frame starts block-aligned, matching the server's ``crypt``.
        """

        aligned = ((len(body) + 15) // 16) * 16
        ks = self.keystream(aligned)
        return bytes(a ^ b for a, b in zip(body, ks))


def _le64(value: int) -> bytes:
    return value.to_bytes(8, "little")


def derive_key(raw_key: bytes, static_key: bytes) -> bytes:
    """Body key = AES-128-ECB-decrypt(static_key, raw_key)."""

    if len(raw_key) != 16:
        raise ValueError(f"raw key must be 16 bytes, got {len(raw_key)}")
    if len(static_key) != 16:
        raise ValueError(f"static key must be 16 bytes, got {len(static_key)}")
    return AES.new(static_key, AES.MODE_ECB).decrypt(raw_key)


def session_ciphers(
    static_key: bytes,
    *,
    key_low: int,
    key_high: int,
    client_send_iv: int,
    server_send_iv: int,
) -> tuple[Rabbit, Rabbit]:
    """``(client_send, server_send)`` ciphers for one session.

    Takes the handshake's key halves and IVs as unsigned 64-bit integers.
    """

    body_key = derive_key(_le64(key_low) + _le64(key_high), static_key)
    return Rabbit(body_key, _le64(client_send_iv)), Rabbit(
        body_key, _le64(server_send_iv)
    )
