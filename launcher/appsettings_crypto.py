#!/usr/bin/env python3
"""Decrypt / encrypt the ROM: Golden Age launcher ``appsettings.json``.

The launcher (``Launcher.Core.dll`` → ``Launcher.Settings``) stores its config
as an ``ENC: <base64>`` blob. The scheme, reversed from the decompiled
``Settings.cs``:

    key = SHA256(p1 + p4 + p2 + p3)          # 32 bytes  → AES-256
    iv  = key[:16]                           # first 16 bytes of the same hash
    cipher = AES-256-CBC, PKCS7 padding
    file   = "ENC: " + base64(cipher(utf-8 plaintext))

The four ``pN`` constants are hard-coded in ``Settings.cs``; a plaintext that
does not start with ``ENC: `` is returned verbatim by the launcher, so decrypt
is a no-op on already-plaintext input.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import sys
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

PREFIX = "ENC: "

# Hard-coded key material from Launcher.Settings (order used to build the key
# string is p1 + p4 + p2 + p3 — note p4 before p2/p3).
_P1 = "G@me*ZXCQQECVWy%%^&%@##$EWFXCVEWRT$@%^FDAFXZCTRYyujessdaf!@#$QSADFE1e47+#$CXVCK$Pflkjb$hty@#$RG($^dvwfd56CVrt34F"
_P2 = "A*@VuncheFrCeq69CXZCVDSAF$@%^#!DFVTREY$%$#RWEFDSAGFHGRJOILOUHDSFDSAFSD$W$%^@CVEWR^dfaq456DAF23143265GFHDSAF2435v"
_P3 = "^zxcvY$ZCVFY%^&HNBdsfgsfdg4567uyjgfdsgthu@#$$&^&*YU(%43dvCVCXBt31rfgbfhY%^%R$fwdgCFGytra!#*VvZ13875XCVERq6SD(*24"
_P4 = "Lo$^ckCqKCVREWTNflbknwrlektjo54jAVCXre5$%&%^&#$#rDFerwY^57DVDSFT$@%^3erDF@!Trewt6XCVREW^$#^Yed1QDf6@*XCVFDSTY465"


def _get_crypto_keys() -> tuple[bytes, bytes]:
    """Return ``(key, iv)`` exactly as ``Settings.GetCryptoKeys()`` does."""
    digest = hashlib.sha256((_P1 + _P4 + _P2 + _P3).encode("utf-8")).digest()
    return digest, digest[:16]


def decrypt(cipher_text: str) -> str:
    """Decrypt an ``ENC: ...`` blob; pass through anything without the prefix."""
    cipher_text = cipher_text.strip()
    if not cipher_text.startswith(PREFIX):
        return cipher_text
    key, iv = _get_crypto_keys()
    payload = base64.b64decode(cipher_text[len(PREFIX):].strip())
    aes = AES.new(key, AES.MODE_CBC, iv)
    return unpad(aes.decrypt(payload), AES.block_size).decode("utf-8")


def encrypt(plain_text: str) -> str:
    """Encrypt UTF-8 plaintext into the ``ENC: <base64>`` form the launcher reads."""
    key, iv = _get_crypto_keys()
    aes = AES.new(key, AES.MODE_CBC, iv)
    payload = aes.encrypt(pad(plain_text.encode("utf-8"), AES.block_size))
    return PREFIX + base64.b64encode(payload).decode("ascii")


def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    # utf-8-sig strips the BOM the launcher writes, matching .NET ReadAllText.
    return Path(path).read_text(encoding="utf-8-sig")


def _write(path: str, data: str) -> None:
    if path == "-":
        sys.stdout.write(data)
        if not data.endswith("\n"):
            sys.stdout.write("\n")
        return
    Path(path).write_text(data, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (("decrypt", "ENC: blob → plaintext JSON"),
                            ("encrypt", "plaintext JSON → ENC: blob")):
        sub_parser = sub.add_parser(name, help=help_text)
        sub_parser.add_argument("input", help="input file, or - for stdin")
        sub_parser.add_argument("-o", "--output", default="-",
                                help="output file (default: stdout)")

    args = parser.parse_args(argv)
    transform = decrypt if args.command == "decrypt" else encrypt
    _write(args.output, transform(_read(args.input)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
