#!/usr/bin/env python3
"""Decrypt / re-encrypt the nProtect GameGuard config container.

GameGuard ships its per-game config as an encrypted container, not as text:
`client/ROMGoldenAge.ini` (also mirrored in `client/GameGuard/`) and the
`gameguard/real/update.cfg` the patch CDN serves. The plaintext is a plain INI.

`ROMGoldenAge.ini` is where GameMon reads its **own** update host from — NOT from
the metadata/resources literals that `metadata_host.py` and `resources_host.py`
rewrite:

    [GAMEMON]
    GAME_NAME=ROMGoldenAge
    UPDATE_SERVER=patch.romgoldenage.com      <- GG's update host
    UPDATE_PATH=/gameguard/real/              <- ...+ path => /gameguard/real/update.cfg
    79085e5a=mgr.gameguard.co.kr              <- nProtect's own live-check hosts (x5)
    HTTP_PORT=443
    ...

So redirecting bases A and B leaves GameGuard talking to production; this file is
the third, independent redirect lever.

## Container layout

Little-endian. The trailer is plaintext; records are `[u8 tag][26 81 32][payload]`
with the tag BEFORE the magic, counting down 0x24 -> 0x21:

    [encrypted blob]
    0x24 | magic | u32 tailExtra | u32 256
    0x23 | magic | filename NUL  | 64-byte digest
    0x22 | magic | u32 nameLen   | u32 64          (the lengths of record 0x23)
    0x21 | magic                                   EOF

The blob decrypts to `[INI text][tailExtra + 256 bytes]`, so

    textLen = len(blob) - tailExtra - 256

which lands exactly on the end of the INI text in every known sample (588 B for
ROMGoldenAge.ini, 578 B for ROMGB.ini, 2049 B for update.cfg). The 256-byte blob
tail and the 64-byte trailer digest are opaque signatures over the text.

## Cipher

An unidentified stream cipher with a **fixed key and no per-file IV**: nProtect
encrypts every one of these files with the SAME keystream, which makes them a
many-time pad. The 600 bytes embedded below were recovered by cross-cribbing
three samples (ROMGoldenAge.ini, ROMGB.ini, a live update.cfg) against each other,
and verified independently — the CRC32 values in the decrypted update.cfg match
`zlib.crc32()` of the shipped GameMon.des / npggNT.des / npggNT64.des exactly.

Consequence: this tool can only touch the first 600 plaintext bytes. Both
.ini files fit entirely; update.cfg (2049 B of text) decrypts only up to that
point and cannot be re-encrypted. Going further needs another crib pass, not a
code change.

## Re-encrypting

`--encrypt` keeps the plaintext length EXACTLY as the original, so the tail
ciphertext and the whole trailer are copied through byte for byte and every
offset stays put. A shorter edit is padded with blank lines (harmless in INI); a
longer one is refused with the delta, so shorten a value or drop a line.

The 256-byte tail and the 64-byte digest are signatures we cannot recompute, so a
re-encrypted file carries the ORIGINAL ones.

Confirmed by runtime tracing (`rom-frida`'s `trace_gg_config.js`): GameMon DOES
verify. `NPGameDLL64.dll` reads the whole file, MD5-hashes exactly `textLen +
tailExtra` bytes — the blob up to the 256-byte block — and calls
`BCryptVerifySignature` with cbHash=16, cbSignature=256. That is
MD5(blob[0:textLen+tailExtra]) against an RSA-2048 signature. `npsc64.des` runs
the same check independently. So a patched config is rejected unless you forge
that signature.

There are TWO copies and the loader has a search order: it opens
`client/GameGuard/<game>.ini` first and falls back to the client-root copy when
that one is missing (same call sites, either path). BOTH get the identical
hash+verify, so deleting one buys nothing. The `GameGuard/` copy is also
self-healing — delete it and GameGuard restores it byte-identical mid-session,
which is why the two shipped copies drifted apart. Patch BOTH, or expect the
fallback to win. The 64-byte trailer digest is NOT explained by any observed
crypto call — its role is still unknown.

This tool NEVER writes over the source; it emits a patched COPY. Swap the copy in
at client/ROMGoldenAge.ini AND client/GameGuard/ROMGoldenAge.ini (they differ, so
patch each from its own source; keep backups).

Usage (from client/):
    python3 patchers/gameguard_config.py ROMGoldenAge.ini                     # analyse only
    python3 patchers/gameguard_config.py ROMGoldenAge.ini --decrypt           # plaintext to stdout
    python3 patchers/gameguard_config.py ROMGoldenAge.ini --decrypt --out rom.ini.txt
    python3 patchers/gameguard_config.py ROMGoldenAge.ini --encrypt rom.ini.txt --out ROMGoldenAge.patched.ini
"""
import argparse
import struct
import sys
from pathlib import Path

MAGIC = b"\x26\x81\x32"
TAIL_SIG_LEN = 256   # record 0x24's second u32: signature at the end of the blob
DIGEST_LEN = 64      # record 0x22's second u32: digest inside record 0x23

KEYSTREAM = bytes.fromhex(
    "b7992c958cbaeb028600f9e9631afc1ff9308688d973e090f8f63e538b846740"
    "6129ba13673939d9094cd75ed22751359cfda27635e4a14d9ba7fb004109dedc"
    "608ca1ead515a6ad383b8bb05fb7cdcfe087fe4187c8e4eeecfe5dc053a682a9"
    "44cd9551a9721c1f14b2da958c19fa7e28a0afda5e76a51567f7375bb3fdd1b2"
    "096e8e972ce6223d114f885375190efacc7099549ac8afb3052fdf27a7f50f2a"
    "9145c034dd446e78a0a46d9af0696f776ed9be8e7700d732a312a686fdc6f15e"
    "10258129adcec478eeffc6fc59c09064d0c436d6c257fa253efcfc4b70ca6932"
    "d4dc11089cfaa7a98e1f1e8be8984126d4cf622bbb51c6202a62f0bd3cbc0146"
    "66ee2e4efc14af4d1ff639363bfbf989387aea0449c88343f0a18da7caa2ca55"
    "9c0155577fe62d4cddf23378f4f09038b75328ecc37a4c834fae5e1665009994"
    "2fefb9c0ad14218ba18b96312afdbee924e1bc9f2b41f26f7215000cb95302af"
    "1afd16e4706feebf4268c58972b9a984b39db8970651fb77d79ceda2e73b89dc"
    "828dc0c37bc703e6ed87ba66caecea5098b6802db49313ff238419085e571377"
    "6741562f13c60f2b4347c2b0c89ceffdae871811f14bea241d7e7f34f3c00884"
    "7dec076f212013a1a08355b31d74b2251c7a94cd53a85b128174118114a3ae21"
    "e4916f54c854d6c23e31c50020f4d3533693af9de5d043bc5e586a2d49ab1b50"
    "0045785c666d9e666ea647ffba0122190cfc013eb0214482564626b3baa3201e"
    "ea1d2a43bbe231d24b7ac7146916a7767b7a9688c048e11c6734e6ab2ca19cfd"
    "28860f10b7e6189bdeda296a6fda899399a7a010036c1212"
)


def parse(data: bytes) -> dict:
    """Split a container into its encrypted blob and its plaintext trailer."""
    magic_at = data.find(MAGIC)
    if magic_at < 1:
        sys.exit("not an nProtect container: magic 26 81 32 not found")
    blob_end = magic_at - 1                      # the record tag sits BEFORE the magic
    blob, trailer = data[:blob_end], data[blob_end:]

    box = {"blob": blob, "trailer": trailer, "blob_end": blob_end}
    pos = 0
    while pos < len(trailer):
        tag = trailer[pos]
        if trailer[pos + 1:pos + 4] != MAGIC:
            sys.exit(f"bad record at 0x{blob_end + pos:x}: tag {tag:#04x} without magic")
        pos += 4
        if tag == 0x21:                          # EOF record, no payload
            break
        nxt = trailer.find(MAGIC, pos)
        payload = trailer[pos:nxt - 1] if nxt > 0 else trailer[pos:]
        if tag == 0x24:
            box["tail_extra"], box["tail_sig_len"] = struct.unpack("<II", payload)
        elif tag == 0x23:
            nul = payload.index(b"\0")
            box["filename"] = payload[:nul].decode("latin1")
            box["digest"] = payload[nul + 1:]
        elif tag == 0x22:
            box["name_len"], box["digest_len"] = struct.unpack("<II", payload)
        else:
            sys.exit(f"unknown record tag {tag:#04x} at 0x{blob_end + pos - 4:x}")
        pos = nxt - 1

    missing = {"tail_extra", "filename", "name_len"} - box.keys()
    if missing:
        sys.exit(f"incomplete container: missing record(s) for {sorted(missing)}")
    box["text_len"] = len(blob) - box["tail_extra"] - box["tail_sig_len"]
    if box["text_len"] < 1:
        sys.exit(f"nonsensical text length {box['text_len']} — not a config container?")
    return box


def xor(data: bytes) -> bytes:
    """The cipher is its own inverse: plaintext/ciphertext XOR the fixed keystream."""
    return bytes(b ^ k for b, k in zip(data, KEYSTREAM))


def decrypt(box: dict) -> tuple[str, int]:
    """Return (plaintext, number of text bytes past the known keystream)."""
    n = min(box["text_len"], len(KEYSTREAM))
    return xor(box["blob"][:n]).decode("latin1"), box["text_len"] - n


def pad_to(text: bytes, size: int) -> bytes:
    """Pad an edited INI out to the original byte length with blank lines."""
    short = size - len(text)
    if short < 0:
        sys.exit(
            f"edited plaintext is {len(text)} bytes but must be <= {size} "
            f"({-short} byte(s) too long); shorten a value or drop a line — the "
            f"length is fixed by the container's offsets"
        )
    if short == 0:
        return text
    if not text.endswith((b"\n", b"\r")):
        text += b"\r\n"
        short -= 2
        if short < 0:
            sys.exit("edited plaintext needs a trailing newline but leaves no room for it")
    filler = b"\r\n" * (short // 2) + b"\n" * (short % 2)
    print(f"[*] padded {short} byte(s) with blank lines to keep the length at {size}")
    return text + filler


def encrypt(box: dict, plaintext: bytes) -> bytes:
    """Rebuild the container around an edited plaintext of the SAME length."""
    if box["text_len"] > len(KEYSTREAM):
        sys.exit(
            f"cannot re-encrypt: text is {box['text_len']} bytes but only "
            f"{len(KEYSTREAM)} bytes of keystream are known"
        )
    body = pad_to(plaintext, box["text_len"])
    return xor(body) + box["blob"][box["text_len"]:] + box["trailer"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("container", type=Path, help="source .ini / update.cfg (never modified)")
    ap.add_argument("--decrypt", action="store_true",
                    help="write the plaintext INI (to --out, or stdout)")
    ap.add_argument("--encrypt", type=Path, metavar="PLAINTEXT",
                    help="re-encrypt this edited plaintext back into a container copy")
    ap.add_argument("--out", type=Path, help="write here (required with --encrypt)")
    args = ap.parse_args()

    if args.decrypt and args.encrypt:
        sys.exit("--decrypt and --encrypt are mutually exclusive")

    data = args.container.read_bytes()
    box = parse(data)

    if not args.decrypt and not args.encrypt:
        covered = min(box["text_len"], len(KEYSTREAM))
        state = ("fully decryptable" if covered == box["text_len"] else
                 f"{covered}/{box['text_len']} bytes decryptable "
                 f"({len(KEYSTREAM)} B of keystream known)")
        print(f"[+] {box['filename']!r}  container {len(data)} bytes")
        print(f"      blob      {len(box['blob'])} bytes @0x0")
        print(f"      text      {box['text_len']} bytes "
              f"(blob - {box['tail_extra']} - {box['tail_sig_len']})")
        print(f"      tail      {box['tail_extra']} + {box['tail_sig_len']} bytes (signature, opaque)")
        print(f"      trailer   {len(box['trailer'])} bytes @0x{box['blob_end']:x}"
              f"  name={box['name_len']} digest={box['digest_len']}")
        print(f"      keystream {state}")
        print("\n[*] analyse-only. Re-run with --decrypt, or --encrypt <plaintext> --out <file>.")
        return

    if args.decrypt:
        text, uncovered = decrypt(box)
        if uncovered:
            print(f"[!] {uncovered} byte(s) past the known keystream were skipped", file=sys.stderr)
        if args.out:
            args.out.write_bytes(text.encode("latin1"))
            print(f"[+] wrote {args.out}  ({len(text)} bytes)")
        else:
            sys.stdout.write(text)
        return

    if not args.out:
        sys.exit("--out is required with --encrypt")
    patched = encrypt(box, args.encrypt.read_bytes())
    if len(patched) != len(data):
        sys.exit(f"internal error: rebuilt {len(patched)} bytes, source was {len(data)}")
    args.out.write_bytes(patched)
    print(f"[patched] {args.encrypt} -> {args.out}  ({len(patched)} bytes, same size)")
    print("[!] the 256-byte tail and 64-byte digest are the ORIGINAL signatures and no")
    print("[!] longer match the text. GameMon VERIFIES this (MD5 over the first")
    print("[!] textLen+tailExtra bytes + RSA), so expect it to reject the file.")
    print("[!] It reads client/GameGuard/<game>.ini, falling back to the client-root copy.")


if __name__ == "__main__":
    main()
