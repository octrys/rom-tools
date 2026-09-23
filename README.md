# rom-tools

Tools for the **ROM: Golden Age** client and launcher — decrypting launcher
config, mirroring files from the official patch server, redirecting the client's
infrastructure hosts, extracting the game's data tables to JSON, and rebuilding
the network-protocol catalog.

## Tools

### [`launcher/`](launcher/) — launcher config crypto

[`appsettings_crypto.py`](launcher/appsettings_crypto.py) decrypts and encrypts
the launcher's `appsettings.json` (`ENC: <base64>` blob).

The scheme, reversed from `Launcher.Settings`: AES-256-CBC, key =
`SHA256(p1 + p4 + p2 + p3)`, IV = the first 16 bytes of that hash, PKCS7. Key +
IV are fixed, so encryption is deterministic — re-encrypting a decrypted config
reproduces the shipped ciphertext byte-for-byte.

```bash
python3 launcher/appsettings_crypto.py decrypt appsettings.json -o appsettings.decrypted.json
python3 launcher/appsettings_crypto.py encrypt appsettings.decrypted.json -o appsettings.json
```

Needs `pycryptodome`. Details in [`launcher/README.md`](launcher/README.md).

### [`patcher/`](patcher/) — patch-server mirror

[`patch_downloader.py`](patcher/patch_downloader.py) mirrors a release from the
patch server (`https://patch.romgoldenage.com`). It reads each component's
`manifest.json` and downloads every per-file zip as-is (kept compressed, **not**
extracted), verifying each against `ArchiveSize` and skipping archives already
present. By default it mirrors both the `launcher` and `patch` (game client)
components of release `NewPCwemix`.

```bash
# default: release NewPCwemix, env Real, components launcher + patch
python3 patch_downloader.py

# a single component
python3 patch_downloader.py --component launcher
```

Standard library only. Details in [`patcher/README.md`](patcher/README.md).

### [`client/`](client/) — host redirect, table typing, protocol catalog

Detailed in [`client/README.md`](client/README.md):

**Host redirect** — adjust the client's `global-metadata.dat` and
`resources.assets` so the game resolves its infrastructure hosts to your own
server instead of `patch.romgoldenage.com` / `auth.romgoldenage.com`. The
patch/CDN host lives in two places — an IL2CPP string literal (base A) and a Unity
`ProjectSettingData_Crypto_Win_Live` string (base B); `resources.assets` also
holds the auth host string. `patchers/resources_host.py` rewrites the patch and auth
strings in a single pass (`--patch-host` / `--auth-host`). Everything is edited in
place, emitting a patched copy without ever overwriting the source. Standard
library only.

```bash
# base A — metadata literal
python3 client/patchers/metadata_host.py global-metadata.dat --new-host 192.168.1.50 --out global-metadata.patched.dat

# base B — resources.assets (patch + auth hosts, each exact byte length; no padding)
python3 client/patchers/resources_host.py resources.assets --patch-host patch.example.internal --auth-host auth.example.internal --out resources.assets.patched
```

**Data extraction** — `exporters/extract_tables.py` turns the runtime table dump
(from rom-frida's `dump_tables.js`) into typed, named JSON for the server: it
resolves every enum to its member name and renames obfuscated fields via a
curated map. Config-driven
([`exporters/extract_tables.toml`](client/exporters/extract_tables.toml)),
standard library only. Run the frida dump first and drop `rom_dump.cs` +
`tables_runtime.json` under `resources/`.

```bash
cd client && python3 -m exporters.extract_tables    # reads exporters/extract_tables.toml
```

**Protocol catalog** — `exporters/extract_protocol.py` rebuilds the message
catalog the server speaks (985 messages: opcodes, ordered field names, and field
types) by merging the client's `global-metadata.dat` with a frida runtime type
dump (`resources/rom_dump.json`). Also config-driven
([`exporters/extract_protocol.toml`](client/exporters/extract_protocol.toml)),
standard library only. A client update rotates every opcode, so re-point it at
the new metadata and re-run.

```bash
cd client && python3 -m exporters.extract_protocol    # reads exporters/extract_protocol.toml
```

## Layout

```
client/                  client host redirect, table typing, protocol catalog
launcher/                launcher config decrypt/encrypt
patcher/                 patch-server mirror downloader
resources/               local, git-ignored inputs/outputs (leaked client data)
resources/rom_dump.cs    il2cpp dump (rom-frida) — type model for exporters/extract_tables.py
resources/tables_runtime.json  runtime table dump (rom-frida) — exporters/extract_tables.py input
resources/tables_typed/  typed, named tables (JSON) + _coverage.json — output
resources/client/        full client install (GameAssembly.dll, ROMGoldenAge_Data)
resources/launcher/      launcher configs (encrypted + decrypted)
tmp/                     scratch for bulk patch downloads (git-ignored)
```
