# client

Tools for the ROM: Golden Age **client** — redirecting its infrastructure hosts
(`metadata_host.py`, `resources_host.py`), turning the runtime table dump into
typed JSON (`type_tables.py`), and rebuilding the network-protocol catalog
(`extract_protocol.py`).

## Host redirect

The retail client resolves its CDN host from **two** places, and every bootstrap
URL (patch manifest, `domaindata.json`, auth, game server) derives from one of
them. Both are `patch.romgoldenage.com`, so redirecting the client to your own
server means editing both:

| Base | Lives in | String | Builds | Tool |
|---|---|---|---|---|
| **A** | IL2CPP string literal in `global-metadata.dat` | `https://patch.romgoldenage.com/NewPCwemix/` (42 B) | `/NewPCwemix/Real/patch/manifest.json` | `metadata_host.py` |
| **B** | `ProjectSettingData_Crypto_Win_Live` string in `resources.assets` | `https://patch.romgoldenage.com/real/` (36 B) | `/real/domaindata.json`, `/real/maintenances.json`, `/real/ROMGoldenAge_WemixPay_Crypto.json`, `/real/patch/Windows/*` | `resources_host.py` |

Both tools edit the host **in place** (no offset rebuild) and always emit a
patched **copy** — they never overwrite the source. Swap the copies in on the
client, keep the originals as backups, and expect GameGuard to hash both files.

For the full set of infrastructure domains compiled into the client (patch, auth,
billing, WEMIX, NHN, etc.), see [`DOMAINS.md`](DOMAINS.md).

## metadata_host.py — base A

Redirects the host inside the IL2CPP string literal in `global-metadata.dat`. The
literal is `{ u32 length; i32 dataIndex }` + a raw data blob, so an in-place edit
only overwrites the bytes and rewrites the length field. Works while the new URL
is `<=` the original length (host→IP shrinks it; `https`→`http` shrinks it more).
The path suffix (`/NewPCwemix/`) is preserved; `--include-pc` also patches the
`/pc/` installer root.

```bash
# analyse only (find the literal, change nothing)
python3 metadata_host.py global-metadata.dat

# write a patched copy pointing at your host
python3 metadata_host.py global-metadata.dat --new-host 192.168.1.50 --out global-metadata.patched.dat

# force plain http (only if it propagates end to end)
python3 metadata_host.py global-metadata.dat --new-host 192.168.1.50 --scheme http --out global-metadata.patched.dat
```

Swap in at `client/ROMGoldenAge_Data/il2cpp_data/Metadata/global-metadata.dat`.

## resources_host.py — resources.assets host rewrite

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
python3 resources_host.py resources.assets

# rewrite the patch host only
python3 resources_host.py resources.assets --patch-host patch.example.internal --out resources.assets.patched

# rewrite both hosts in one pass
python3 resources_host.py resources.assets \
    --patch-host patch.example.internal --auth-host auth.example.internal \
    --out resources.assets.patched
```

Swap in at `client/ROMGoldenAge_Data/resources.assets`.

## type_tables.py — runtime table dump → typed, named JSON

The shipped bundle can only be decoded losslessly offline (raw bytes): its
on-wire field order is bespoke, and some values are resolved only at runtime
(localized names, `.unity`-suffixed scene paths). This tool instead consumes the
**runtime** dump from rom-frida's `dump_tables.js`
(`resources/tables_runtime.json`) — every table's rows, fully parsed and
resolved, keyed by il2cpp-**obfuscated** field names with enum values as raw
integers — and makes it readable:

1. **Enum resolution** — every enum value becomes its member name (enum members
   are *not* obfuscated), e.g. `2 → "MT_FIELD"`, using the type model parsed
   from `rom_dump.cs`.
2. **Field naming** — obfuscated names are renamed via
   [`table_names.toml`](table_names.toml), a curated map with two scopes:
   `[types.<Struct>]` (shared nested value types, named once) and
   `[tables.<Table>]` (a table's top-level fields). Unmapped fields keep their
   obfuscated name — nothing is lost — and `_coverage.json` tracks progress so
   naming can be driven table by table.

Config-driven ([`type_tables.toml`](type_tables.toml)), standard library only
(Python 3.11+ for `tomllib`). Reads the frida artifacts, so run those first (see
the `rom-frida` repo) and place `rom_dump.cs` + `tables_runtime.json` under
`resources/`.

```bash
python3 type_tables.py            # reads ./type_tables.toml
```

Writes one typed `<Table>.json` per table into the configured output dir, plus
`_coverage.json` (named vs. obfuscated fields per table). Shared internal code
lives in [`libs/`](libs/): `libs/table_schema.py` (the dump parser, imported by
the tool) and `libs/inspect_table.py` (a dev helper that dumps a table's fields
+ sample values to drive naming — `python3 -m libs.inspect_table <Table>`).

## extract_protocol.py — network-protocol catalog → JSON

Rebuilds the message catalog the server speaks: **985 messages** with opcodes,
ordered field names, and field types. It merges two client artifacts:

| Source | Gives | Why it's needed |
|---|---|---|
| `global-metadata.dat` | message classes, `__ID__` opcodes, ordered field names | clean (unencrypted) on PC, parsed directly — no il2cpp dumper (the binary is Themida-packed) |
| `rom_dump.json` | each field's **type** | the type table lives inside the packed `GameAssembly.dll`, decrypted only at runtime, so types can't be read statically — this is a frida-il2cpp-bridge dump |

Like its sibling tools it is **config-driven, not CLI**: edit
[`extract_protocol.toml`](extract_protocol.toml) (metadata, dump, output paths)
and run it. Standard library only, but needs `tomllib` (Python 3.11+).

```bash
python3 extract_protocol.py            # reads ./extract_protocol.toml
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

- `metadata_host.py`, `resources_host.py`: Python 3.8+ (standard library only)
- `type_tables.py`: Python 3.11+ (`tomllib`), standard library only
- `extract_protocol.py`: Python 3.11+ (`tomllib`), standard library only
