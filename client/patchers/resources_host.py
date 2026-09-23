#!/usr/bin/env python3
"""Rewrite a hardcoded host string inside the client's resources.assets in place.

Some of the client's infrastructure hosts are NOT in the IL2CPP metadata but in
Unity-serialized strings inside `ROMGoldenAge_Data/resources.assets`. Each is a
`[u32 length][UTF-8]` string. Two are redirect targets:

    patch  "https://patch.romgoldenage.com/real/"   (36 bytes) -> base B: domaindata,
           maintenances, WemixPay, /real/patch/Windows/* (ProjectSettingData_Crypto_Win_Live)
    auth   "https://auth.romgoldenage.com/"         (30 bytes) -> the auth host

resources.assets is a Unity SerializedFile: changing a string's LENGTH shifts all
later byte offsets and corrupts the file. So we edit IN PLACE at the SAME byte
length — overwrite the data bytes, keep the length prefix. The replacement swaps
scheme+host but PRESERVES the original path suffix (`/real/` for patch, `/` for
auth), and must be EXACTLY the original length; the tool does NOT pad it. If the
chosen host/scheme yields a different length it refuses and reports how far off it
is, so pick a host of the right length. For example, patch needs a 22-char host
and auth needs a 21-char host over https:

    "https://patch.example.internal/real/"   (36 bytes)
    "https://auth.example.internal/"         (30 bytes)

Base A (the patch manifest host) is a metadata literal, redirected separately via
patchers/metadata_host.py.

NEVER writes over the source; emits a patched COPY. Swap it in at
client/ROMGoldenAge_Data/resources.assets (keep a backup; GameGuard hashes it).

Both hosts are rewritten in a single pass: pass --patch-host, --auth-host, or
both, and the tool applies every requested edit to one patched copy.

Usage (from client/):
    python3 patchers/resources_host.py resources.assets                          # analyse all targets
    python3 patchers/resources_host.py resources.assets --patch-host patch.example.internal --out resources.assets.patched
    python3 patchers/resources_host.py resources.assets --patch-host patch.example.internal --auth-host auth.example.internal --out resources.assets.patched
"""
import argparse
import struct
import sys
from pathlib import Path

# target name -> original Unity string (length-prefixed, edited in place)
TARGETS = {
    "patch": b"https://patch.romgoldenage.com/real/",   # base B patch host, 36 bytes
    "auth": b"https://auth.romgoldenage.com/",           # auth host, 30 bytes
}


def build_replacement(original: bytes, host: str, scheme: str) -> bytes:
    """Swap scheme+host, PRESERVE the path suffix, require the SAME byte length.

    resources.assets is a Unity SerializedFile, so the replacement MUST match the
    original byte length or every later offset shifts and the file corrupts. No
    padding is added: if the host/scheme does not produce a same-length URL this
    refuses and reports how far off it is, so the caller can pick another host.
    """
    text = original.decode()
    after_scheme = text.split("://", 1)[1]           # host/path...
    path = after_scheme[after_scheme.index("/"):]    # /real/  or  /   (leading slash)
    url = f"{scheme}://{host}{path}"
    encoded = url.encode()
    if len(encoded) != len(original):
        diff = len(encoded) - len(original)
        direction = "long" if diff > 0 else "short"
        raise SystemExit(
            f"replacement {url!r} is {len(encoded)} bytes, but must be exactly "
            f"{len(original)} ({abs(diff)} byte(s) too {direction}); choose a "
            f"host/scheme that yields a {len(original)}-byte URL"
        )
    return encoded


def locate(data: bytes, original: bytes) -> int:
    """Return the absolute offset of the string data (after the u32 length prefix)."""
    prefixed = struct.pack("<I", len(original)) + original
    n = data.count(prefixed)
    idx = data.find(prefixed)
    if idx < 0:
        raise SystemExit(f"string {original.decode()!r} not found")
    if n != 1:
        raise SystemExit(f"expected 1 occurrence of {original.decode()!r}, found {n} — abort (ambiguous)")
    return idx


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("assets", type=Path, help="source resources.assets")
    ap.add_argument("--patch-host", help="replacement host for the patch/base-B string; "
                    "the resulting <scheme>://<host>/real/ must match the original byte length")
    ap.add_argument("--auth-host", help="replacement host for the auth string; "
                    "the resulting <scheme>://<host>/ must match the original byte length")
    ap.add_argument("--scheme", choices=["http", "https"], default="https")
    ap.add_argument("--out", type=Path, help="write patched copy here (required with any --*-host)")
    args = ap.parse_args()

    data = bytearray(args.assets.read_bytes())

    # target name -> requested replacement host (None if not being rewritten)
    requested = {"patch": args.patch_host, "auth": args.auth_host}

    if not any(requested.values()):
        for name, original in sorted(TARGETS.items()):
            idx = locate(data, original)
            print(f"[+] {name:5} {original.decode()!r}")
            print(f"      length={len(original)}  prefix@0x{idx:x}  bytes@0x{idx + 4:x}  (unique)")
        print("\n[*] analyse-only. Re-run with --patch-host and/or --auth-host and --out to write a patched copy.")
        return
    if not args.out:
        sys.exit("--out is required when a replacement host is given")

    for target, host in requested.items():
        if not host:
            continue
        original = TARGETS[target]
        idx = locate(data, original)
        data_off = idx + 4
        new = build_replacement(original, host, args.scheme)
        data[data_off: data_off + len(original)] = new   # same length -> no offset rebuild
        print(f"[patched] {target}: {original.decode()!r} -> {new.decode()!r} (len {len(new)}, same size)")

    args.out.write_bytes(data)
    print(f"[+] wrote {args.out}  ({len(data)} bytes)")
    print("[!] swap in at client/ROMGoldenAge_Data/resources.assets (keep a backup)")


if __name__ == "__main__":
    main()
