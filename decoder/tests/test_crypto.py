"""Rabbit keystream golden vectors, ported from the reference server tests.

These are verified against the live production server, so they guard the port
against any regression in the counter/carry/keystream arithmetic.
"""

from __future__ import annotations

import pytest

from decoder.crypto import Rabbit, derive_key

_SEQ_KEY = bytes(range(16))


@pytest.mark.parametrize(
    ("key", "iv", "n", "want"),
    [
        (
            _SEQ_KEY,
            None,
            64,
            (
                "08404f232bf002175aaf97e92e6e5fe52e6f26497e5e027f931f48b08c51c49d"
                "7004d864cc8f2451e03c4cc8c7c94f5496a49c4b694144dd4c33bfe211d8256f"
            ),
        ),
        (
            _SEQ_KEY,
            bytes([0xAA, 0xBB, 0xCC, 0xDD, 0x01, 0x02, 0x03, 0x04]),
            64,
            (
                "49082cd10ead88cc22772034bc972ac0dd5f39140d05f6d21769fd78381dbc40"
                "fca011057e192ba6686efcec2d20b961a41dd1a703bb7504f32e871de2ffa23e"
            ),
        ),
        (
            bytes.fromhex("4d8a1953efda4b63ed1edc0e0d4d2c42"),
            None,
            32,
            "970914f2655575c6c674dbb14cf24f95c297b3c44275e42440f04e3ffc823b13",
        ),
    ],
)
def test_rabbit_golden_vectors(key: bytes, iv: bytes | None, n: int, want: str) -> None:
    assert Rabbit(key, iv).keystream(n).hex() == want


def test_crypt_is_involutive() -> None:
    body = b"the quick brown fox jumps over" * 3
    encrypted = Rabbit(_SEQ_KEY, None).crypt(body)
    decrypted = Rabbit(_SEQ_KEY, None).crypt(encrypted)
    assert decrypted == body
    assert encrypted != body


def test_crypt_advances_to_block_boundary() -> None:
    """A 30-byte frame must consume 32 keystream bytes, aligning the next frame."""
    cipher = Rabbit(_SEQ_KEY, None)
    cipher.crypt(b"\x00" * 30)  # consumes 32 keystream bytes
    reference = Rabbit(_SEQ_KEY, None)
    reference.keystream(32)  # skip the same 32 bytes
    assert cipher.keystream(16) == reference.keystream(16)


def test_derive_key_round_trips_against_aes_ecb() -> None:
    from Crypto.Cipher import AES

    static_key = b"Sxf89J&1*,0Axm>t"
    raw_key = bytes(range(16))
    derived = derive_key(raw_key, static_key)
    # deriveKey decrypts; encrypting the result must return the raw key.
    assert AES.new(static_key, AES.MODE_ECB).encrypt(derived) == raw_key
