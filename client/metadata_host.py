#!/usr/bin/env python3
"""Redirect the client's CDN host by editing a string literal in global-metadata.dat.

This is the client's redirect **base A**. The retail client builds EVERY patch URL
as String.Format("{0}/{1}", base, path), where the base host
`patch.romgoldenage.com` is parsed (System.Uri) out of the metadata string literal:

    https://patch.romgoldenage.com/NewPCwemix/      (42 bytes)

Both URL shapes derive from it:
  - https://patch.romgoldenage.com/NewPCwemix/Real/patch/manifest.json  (direct)
  - https://patch.romgoldenage.com/real/domaindata.json                 (host reused)
      -> live-auth-region-sa.romgoldenage.com/api/worldList (from domaindata) -> :17701

So changing the HOST inside that ONE literal redirects the whole bootstrap
(domaindata + auth + game). IL2CPP string literals are stored as
`Il2CppStringLiteral{ uint32 length; int32 dataIndex }` + a raw stringLiteralData
blob, each with an explicit length -> an in-place edit needs only: overwrite the
bytes and rewrite the length field. No offset rebuild while the new string is
<= the original length (host->IP shrinks it; https->http shrinks it further).

The literal's PATH suffix is preserved (only scheme+host are swapped) so
`/NewPCwemix/` stays `/NewPCwemix/`. By default only the active WEMIX prod root is
patched; `--include-pc` also patches the `/pc/` installer root.

This tool NEVER writes over the source; it emits a patched COPY. Swap the copy in
at client/ROMGoldenAge_Data/il2cpp_data/Metadata/global-metadata.dat (keep a
backup; GameGuard hashes it).

Usage:
    python3 metadata_host.py global-metadata.dat                      # analyse only
    python3 metadata_host.py global-metadata.dat --new-host 192.168.1.50 --out gm.patched.dat
    python3 metadata_host.py global-metadata.dat --new-host 192.168.1.50 --scheme http --out ...
"""
import argparse
import struct
import sys
from pathlib import Path

PRIMARY = "https://patch.romgoldenage.com/NewPCwemix/"   # active WEMIX prod root
PC_ROOT = "https://patch.romgoldenage.com/pc/"           # installer/bootstrap root

SECTIONS = [
    "stringLiteral", "stringLiteralData", "string", "events", "properties",
    "methods", "parameterDefaultValues", "fieldDefaultValues",
    "fieldAndParameterDefaultValueData", "fieldMarshaledSizes", "parameters",
    "fields", "genericParameters", "genericParameterConstraints",
    "genericContainers", "nestedTypes", "interfaces", "vtableMethods",
    "interfaceOffsets", "typeDefinitions", "images", "assemblies", "fieldRefs",
    "referencedAssemblies", "attributeData", "attributeDataRange",
    "unresolvedVirtualCallParameterTypes", "unresolvedVirtualCallParameterRanges",
    "windowsRuntimeTypeNames", "windowsRuntimeStrings", "exportedTypeDefinitions",
]


def parse_sections(data: bytes) -> dict:
    magic, version = struct.unpack_from("<II", data, 0)
    if magic != 0xFAB11BAF or version != 31:
        sys.exit(f"unexpected magic/version {magic:x}/{version} (need FAB11BAF/31)")
    hdr = struct.unpack_from("<" + "I" * (2 + 2 * len(SECTIONS)), data, 0)
    sec, i = {}, 2
    for name in SECTIONS:
        sec[name] = (hdr[i], hdr[i + 1])
        i += 2
    return sec


def find_literals(data: bytes, sec: dict, target: str):
    """Return every (entry_off, data_abs_off, length) whose literal == target."""
    sl_off, sl_size = sec["stringLiteral"]
    sld_off, _ = sec["stringLiteralData"]
    want = target.encode("utf-8")
    matches = []
    for entry_off in range(sl_off, sl_off + sl_size, 8):
        length, didx = struct.unpack_from("<Ii", data, entry_off)
        if data[sld_off + didx: sld_off + didx + length] == want:
            matches.append((entry_off, sld_off + didx, length))
    return matches


def rebuild(original: str, new_host: str, scheme: str | None) -> str:
    """Swap scheme+host, PRESERVE the path suffix. original = 'https://host/path...'."""
    after_scheme = original.split("://", 1)[1]           # host/path...
    path = after_scheme[after_scheme.index("/"):]        # /NewPCwemix/  (leading slash)
    sch = scheme if scheme else original.split("://", 1)[0]
    return f"{sch}://{new_host}{path}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("md", type=Path, help="source global-metadata.dat")
    ap.add_argument("--new-host", help="replacement host/IP (e.g. 192.168.1.50)")
    ap.add_argument("--scheme", choices=["http", "https"], default=None,
                    help="force scheme (default: keep https). http only if it propagates")
    ap.add_argument("--include-pc", action="store_true",
                    help="also patch the /pc/ installer root")
    ap.add_argument("--out", type=Path, help="write patched copy here (required with --new-host)")
    args = ap.parse_args()

    data = bytearray(args.md.read_bytes())
    sec = parse_sections(data)

    targets = [PRIMARY] + ([PC_ROOT] if args.include_pc else [])
    plan = []
    for target in [PRIMARY, PC_ROOT]:
        matches = find_literals(data, sec, target)
        tag = "PRIMARY" if target == PRIMARY else "pc     "
        if not matches:
            print(f"[!] NOT FOUND ({tag}): {target!r}")
            continue
        note = f"  ({len(matches)} occurrences — all will be patched)" if len(matches) > 1 else ""
        print(f"[+] {tag} {target!r}{note}")
        for entry_off, data_off, length in matches:
            print(f"      length={length}  entry@0x{entry_off:x}  bytes@0x{data_off:x}")
            if target in targets:
                plan.append((target, entry_off, data_off, length))

    if not args.new_host:
        print("\n[*] analyse-only. Re-run with --new-host and --out to write a patched copy.")
        return
    if not args.out:
        sys.exit("--out is required with --new-host")

    for target, entry_off, data_off, length in plan:
        new_url = rebuild(target, args.new_host, args.scheme)
        nb = new_url.encode("utf-8")
        if len(nb) > length:
            sys.exit(f"{new_url!r} is {len(nb)} bytes; must be <= {length} (slot for {target!r})")
        data[data_off: data_off + length] = b"\x00" * length
        data[data_off: data_off + len(nb)] = nb
        struct.pack_into("<I", data, entry_off, len(nb))
        print(f"[patched] {target!r} -> {new_url!r} (len {length} -> {len(nb)})")

    args.out.write_bytes(data)
    print(f"\n[+] wrote {args.out}  ({len(data)} bytes)")
    print("[!] swap in at client/ROMGoldenAge_Data/il2cpp_data/Metadata/global-metadata.dat")
    print("[!] keep the original as backup; watch for a GameGuard integrity check on launch.")


if __name__ == "__main__":
    main()
