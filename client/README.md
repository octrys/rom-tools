# client

Tools for the ROM: Golden Age **client** — redirecting its infrastructure hosts
(`patchers/metadata_host.py`, `patchers/resources_host.py`), reading and rewriting
GameGuard's own encrypted config (`patchers/gameguard_config.py`), decoding the
game-data tables (`datatables/`), and rebuilding the network-protocol catalog
(`exporters/extract_protocol.py`).

Both are run from this directory as modules (`python3 -m datatables <command>`,
`python3 -m exporters.extract_protocol`).

## datatables — offline table decoder

[`datatables/`](datatables/) decodes every game-data table straight from the
patch bundle (`tablecrypto.unity`) into JSON (English text), with enum values
as member names. It also writes an annotated
column schema. Row layouts are inferred once per build against rom-frida's
runtime dump; after that, decoding needs only the bundle. Input paths live in
[`datatables/datatables.toml`](datatables/datatables.toml); details in
[`datatables/README.md`](datatables/README.md).

```bash
uv run --with 'UnityPy>=1.25' python3 -m datatables infer    # once per build
uv run --with 'UnityPy>=1.25' python3 -m datatables decode   # -> resources/datatables/tables/
```

Field names are recovered from the UI: rom-frida's `trace_ui_text.js` records
what the game displays, `datatables uitrace` matches it against the decoded
tables (accumulating every trace), and `datatables suggest` writes one entry per
obfuscated identifier to [`datatables/names.toml`](datatables/names.toml) for
review. Confirmed names are applied by `decode` and carried across builds.

## Host redirect

The retail client resolves its CDN host from **two** places, and every bootstrap
URL (patch manifest, `domaindata.json`, auth, game server) derives from one of
them. Both are `patch.romgoldenage.com`, so redirecting the client to your own
server means editing both:

| Base | Lives in | String | Builds | Tool |
|---|---|---|---|---|
| **A** | IL2CPP string literal in `global-metadata.dat` | `https://patch.romgoldenage.com/NewPCwemix/` (42 B) | `/NewPCwemix/Real/patch/manifest.json` | `patchers/metadata_host.py` |
| **B** | `ProjectSettingData_Crypto_Win_Live` string in `resources.assets` | `https://patch.romgoldenage.com/real/` (36 B) | `/real/domaindata.json`, `/real/maintenances.json`, `/real/ROMGoldenAge_WemixPay_Crypto.json`, `/real/patch/Windows/*` | `patchers/resources_host.py` |
| **GG** | `UPDATE_SERVER` in the encrypted `ROMGoldenAge.ini` | `patch.romgoldenage.com` + `/gameguard/real/` | `/gameguard/real/update.cfg` (GameMon's own update descriptor) | `patchers/gameguard_config.py` |

Both host tools edit the string **in place** (no offset rebuild) and always emit a
patched **copy** — they never overwrite the source. Swap the copies in on the
client, keep the originals as backups, and expect GameGuard to hash both files.

**GameGuard does not use either literal.** GameMon reads its update host from its
own encrypted config, so patching bases A and B leaves GG talking to production —
a live external dependency. `patchers/gameguard_config.py` is the third,
independent lever (and the only one whose file is signed).

For the full set of infrastructure domains compiled into the client (patch, auth,
billing, WEMIX, NHN, etc.), see [`DOMAINS.md`](DOMAINS.md).

## patchers/metadata_host.py — base A

Redirects the host inside the IL2CPP string literal in `global-metadata.dat`. The
literal is `{ u32 length; i32 dataIndex }` + a raw data blob, so an in-place edit
only overwrites the bytes and rewrites the length field. Works while the new URL
is `<=` the original length (host→IP shrinks it; `https`→`http` shrinks it more).
The path suffix (`/NewPCwemix/`) is preserved; `--include-pc` also patches the
`/pc/` installer root.

```bash
# analyse only (find the literal, change nothing)
python3 patchers/metadata_host.py global-metadata.dat

# write a patched copy pointing at your host
python3 patchers/metadata_host.py global-metadata.dat --new-host 192.168.1.50 --out global-metadata.patched.dat

# force plain http (only if it propagates end to end)
python3 patchers/metadata_host.py global-metadata.dat --new-host 192.168.1.50 --scheme http --out global-metadata.patched.dat
```

Swap in at `client/ROMGoldenAge_Data/il2cpp_data/Metadata/global-metadata.dat`.

## patchers/resources_host.py — resources.assets host rewrite

Rewrites hardcoded host strings inside `resources.assets`. Two targets, each with
its own flag, applied in a single pass:

| Flag | Original string | Length | Redirects |
|---|---|---|---|
| `--patch-host` | `https://patch.romgoldenage.com/real/` | 36 B | base B: `domaindata.json`, `maintenances.json`, WemixPay, `/real/patch/Windows/*` |
| `--auth-host` | `https://auth.romgoldenage.com/` | 30 B | the auth host |

`resources.assets` is a Unity `SerializedFile`, so changing a string's **length**
shifts every later offset and corrupts the file — the edit keeps the exact byte
length. The replacement swaps scheme+host but preserves the path suffix (`/real/`
for patch, `/` for auth) and must be **exactly** the original length; the tool
does **not** pad it. If the host/scheme yields a different length it refuses and
reports how far off it is, so pick a host of the right length (over `https`, a
22-character host for patch, a 21-character host for auth). For patch, a
redirect webserver is expected to strip everything before `/real/`.

```bash
# analyse only — list both targets, their offsets, and confirm each is unique
python3 patchers/resources_host.py resources.assets

# rewrite the patch host only
python3 patchers/resources_host.py resources.assets --patch-host patch.example.internal --out resources.assets.patched

# rewrite both hosts in one pass
python3 patchers/resources_host.py resources.assets \
    --patch-host patch.example.internal --auth-host auth.example.internal \
    --out resources.assets.patched
```

Swap in at `client/ROMGoldenAge_Data/resources.assets`.

## patchers/gameguard_config.py — GameGuard's encrypted config

Decrypts and re-encrypts the nProtect container GameGuard ships its per-game
config in: `client/ROMGoldenAge.ini` (mirrored, with different contents, in
`client/GameGuard/`) and the `gameguard/real/update.cfg` served by the patch CDN.
The plaintext is a plain INI:

```ini
[GAMEMON]
GAME_NAME=ROMGoldenAge
UPDATE_SERVER=patch.romgoldenage.com      ; GG's update host
UPDATE_PATH=/gameguard/real/              ; ...+ path => /gameguard/real/update.cfg
79085e5a=mgr.gameguard.co.kr              ; nProtect's own live-check hosts (x5)
NO_USE_SCAN=1
SPEEDCHECK_INTERVAL=1000
HTTP_PORT=443
```

A full decrypted sample (CRLF, 588 bytes, `UPDATE_SERVER` already pointed at
`rmpatch.rom.octrys.dev`) is in
[`patchers/samples/ROMGoldenAge.decrypted.ini`](patchers/samples/ROMGoldenAge.decrypted.ini).

**Container** — little-endian, plaintext trailer of `[u8 tag][26 81 32][payload]`
records with the tag *before* the magic, counting down `0x24` → `0x21`
(`tailExtra`+`256`; `filename`+64-byte digest; the lengths of the previous record;
EOF). The blob decrypts to `[INI text][tailExtra + 256]`, so
`textLen = len(blob) - tailExtra - 256` — which lands exactly on the end of the
text in every known sample.

**Cipher** — an unidentified stream cipher with a **fixed key and no per-file IV**:
every file nProtect ships uses the same keystream, making them a many-time pad.
The 600 bytes embedded in the tool were recovered by cross-cribbing three samples
against each other, and verified independently — the CRC32 values in the decrypted
`update.cfg` match `zlib.crc32()` of the shipped `GameMon.des` / `npggNT.des` /
`npggNT64.des` exactly. Both `.ini` files fit inside those 600 bytes; `update.cfg`
(2049 B of text) only decrypts up to that point and cannot be re-encrypted.

```bash
# analyse only — records, text length, keystream coverage
python3 patchers/gameguard_config.py ROMGoldenAge.ini

# plaintext to stdout, or to a file
python3 patchers/gameguard_config.py ROMGoldenAge.ini --decrypt
python3 patchers/gameguard_config.py ROMGoldenAge.ini --decrypt --out rom.ini.txt

# edit rom.ini.txt, then re-encrypt into a patched copy
python3 patchers/gameguard_config.py ROMGoldenAge.ini --encrypt rom.ini.txt --out ROMGoldenAge.patched.ini
```

`--encrypt` holds the plaintext length **exactly**, so the tail ciphertext and the
whole trailer pass through byte for byte and no offset moves — re-encrypting an
unmodified decrypt reproduces the source bit for bit. A shorter edit is padded
with blank lines; a longer one is refused with the delta.

⚠️ The 256-byte tail and 64-byte digest are signatures that cannot be recomputed,
so a patched file carries the originals. **GameGuard does verify them** — runtime
tracing (`rom-frida`'s `trace_gg_config.js`) caught `NPGameDLL64.dll` reading all
1007 bytes of `client/GameGuard/ROMGoldenAge.ini`, MD5-hashing exactly
`textLen + tailExtra` (638) bytes and calling `BCryptVerifySignature`. So a
modified config is rejected without an RSA forgery. It opens the `GameGuard/`
copy first and **falls back to the client-root copy** when that is missing —
both are verified the same way, so patch both. The two are **not** identical
(they differ in one flag value), because GameGuard restores its own copy.

## exporters/extract_protocol.py — network-protocol catalog → JSON

Rebuilds the message catalog the server speaks: **985 messages** with opcodes,
ordered field names, and field types. It merges two client artifacts:

| Source | Gives | Why it's needed |
|---|---|---|
| `global-metadata.dat` | message classes, `__ID__` opcodes, ordered field names | clean (unencrypted) on PC, parsed directly — no il2cpp dumper (the binary is Themida-packed) |
| `rom_dump.json` | each field's **type** | the type table lives inside the packed `GameAssembly.dll`, decrypted only at runtime, so types can't be read statically — this is a frida-il2cpp-bridge dump |

Like its sibling tools it is **config-driven, not CLI**: edit
[`extract_protocol.toml`](exporters/extract_protocol.toml) (metadata, dump, output
paths) and run it. Standard library only, but needs `tomllib` (Python 3.11+).

```bash
python3 -m exporters.extract_protocol    # reads exporters/extract_protocol.toml
```

Writes into the configured output dir:

- **`messages.json` / `messages.md`** — opcodes + ordered field names (metadata
  alone; ~80% of the protocol).
- **`messages_typed.json` / `messages_typed.md`** — the above plus field types
  (metadata + dump). Leave `[paths].dump = ""` to emit the untyped catalog only.

**A client update rotates every opcode** (the body serialization stays identical),
so after an update re-point `[paths].metadata` at the new `global-metadata.dat`
and re-run to remigrate the catalog.

## Requirements

- `patchers/metadata_host.py`, `patchers/resources_host.py`, `patchers/gameguard_config.py`: Python 3.10+ (standard library only)
- `datatables/`: Python 3.11+ (`tomllib`) and `UnityPy>=1.25`
- `exporters/extract_protocol.py`: Python 3.11+ (`tomllib`), standard library only
