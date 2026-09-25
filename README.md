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

### [`client/`](client/) — host redirect, table decoder, protocol catalog

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

**Table decoder** — `datatables/` decodes every table straight from
the patch bundle (`tablecrypto.unity`) into JSON (English text), with enum
values as member names. Row layouts are learned once per
build against rom-frida's runtime dump (`rom_dump.cs` + `tables_runtime.json`,
dropped under `resources/`); after that, decoding needs only the
bundle. Paths are set in
[`datatables/datatables.toml`](client/datatables/datatables.toml). Needs
UnityPy.

```bash
cd client && uv run --with 'UnityPy>=1.25' python3 -m datatables decode
```

**Field names** — obfuscated field names are recovered from the UI: rom-frida's
`trace_ui_text.js` records what the game displays, `datatables uitrace`
matches every trace against the decoded tables, and `datatables suggest`
writes one entry per obfuscated identifier to
[`datatables/names.toml`](client/datatables/names.toml). Each entry has a
`status`:

- `suggested` — generated from the evidence, and regenerated on every
  `suggest` run: the draft name can change when new traces come in.
- `confirmed` — reviewed by hand (edit the name, set `status = "confirmed"`).
  The tool never changes these, except to re-key them to a new build's
  obfuscated name.

`[decode].names` in `datatables.toml` picks what `decode` applies:

| Value | Applied |
|---|---|
| `confirmed` | confirmed entries only: field names change only when someone approves one (the default, and the current setting) |
| `all` | confirmed and suggested: more fields named, but a suggestion can rename a field between runs |
| `none` | nothing: every field keeps its obfuscated name |

```bash
cd client
uv run --with 'UnityPy>=1.25' python3 -m datatables uitrace /mnt/c/.../rom-frida/storage/ui_trace_*.jsonl
uv run --with 'UnityPy>=1.25' python3 -m datatables suggest
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

### [`decoder/`](decoder/) — pcap analyzer/decoder

Reassembles TCP streams from captured game traffic, splits them into protocol
frames, decrypts them (Rabbit stream cipher, session key derived from the
plaintext handshake — captures decrypt passively, no client instrumentation)
and decodes each frame against the protocol catalog above. A FastAPI + Preact
web inspector browses the result: a chronological **Packets** view (hex dump,
byte interpreter, decoded field tree) and an **Opcodes** view (counts, size
ranges, decode status, draft schema). Paths, wire framing and the static AES
key live in [`decoder/decoder.toml`](decoder/decoder.toml).

```bash
cd decoder && uv venv && uv pip install -e '.[dev]'
python3 -m decoder.web                              # http://127.0.0.1:8000
python3 -m decoder.analyze pcap/session.pcap        # framing brute-force + hexdump
```

Needs the protocol catalog (run `exporters.extract_protocol` first). Captures in
`decoder/pcap/` and the analysis cache in `decoder/.cache/` are git-ignored:
they carry real session data — account code, device ID and hardware names,
character list, per-session key material — so don't share them. Details in
[`decoder/README.md`](decoder/README.md).

## Layout

```
client/                  client host redirect, table decoder, protocol catalog
decoder/                 pcap decrypt/decode + web inspector
decoder/pcap/            captures to analyze (git-ignored, sensitive)
decoder/.cache/          per-capture analysis cache (git-ignored, sensitive)
launcher/                launcher config decrypt/encrypt
patcher/                 patch-server mirror downloader
resources/               local, git-ignored inputs/outputs (leaked client data)
resources/rom_dump.cs    il2cpp dump (rom-frida) — type model for datatables
resources/rom_dump.json  il2cpp type dump (rom-frida) — field types for exporters/extract_protocol.py
resources/tables_runtime.json  runtime table dump (rom-frida) — ground truth for datatables infer/verify
resources/datatables/    datatables output: layouts.json, schema.json, evidence.json, traces/, tables/<Table>.json
resources/client/        full client install (GameAssembly.dll, ROMGoldenAge_Data)
resources/launcher/      launcher configs (encrypted + decrypted)
tmp/                     scratch for bulk patch downloads (git-ignored)
```
