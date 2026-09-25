# `decoder/` — pcap analyzer/decoder

Reassembles TCP streams from captured **ROM: Golden Age** traffic, splits them
into protocol frames, **decrypts** them (Rabbit stream cipher, session key
derived from the plaintext handshake), and decodes each frame against the
message catalog the client exporter produces
(`resources/protocol/messages_typed.json`). A small FastAPI app lets you pick a
capture and inspect the decoded messages in the browser.

Because the per-session key material rides in the plaintext handshake frame,
captures decrypt **passively** — no client instrumentation, no live keys.

## Stack

- **`dpkt`** — pcap parsing (Ethernet → IP → TCP), pure-Python, no libpcap.
- **stdlib TCP reassembly** — per-4-tuple, per-direction, seq-ordered.
- **Rabbit cipher + `pycryptodome`** — RFC 4503 keystream (ported and validated
  against the server's golden vectors) plus AES-128-ECB body-key derivation.
- **`struct`** — decodes primitive IL2CPP field types from decrypted bodies.
- **`FastAPI` + `uvicorn`** — serves the JSON API and the static inspector UI.
- **`Preact` + `htm`** — the UI, loaded from an ESM CDN (no build step, no
  `node_modules`); plain native ES modules under `static/`.

## Setup

```bash
cd decoder
uv venv && uv pip install -e '.[dev]'   # or: python3 -m venv .venv && pip install -e '.[dev]'
```

This also installs two console scripts, `rom-decoder-web` and
`rom-decoder-analyze`, equivalent to the `python3 -m` commands below.

Requires the protocol catalog. Generate it first if missing:

```bash
cd ../client && python3 -m exporters.extract_protocol
```

## Config

All paths and the wire framing live in [`decoder.toml`](decoder.toml) (relative
paths resolve against its directory, like the other tools' `.toml` configs):

- `[paths].catalog` — the typed protocol catalog. Regenerating it (a client
  update rotates every opcode) is picked up with no change here.
- `[paths].type_dump` — the frida runtime dump (`rom_dump.json`) for struct
  layouts / inheritance / enums (see Field decoding). `""` disables it.
- `[paths].pcap_dir` — directory scanned for captures.
- `[paths].cache_dir` — where per-capture analysis is cached (one JSON plus a
  small `.sig.json` signature per pcap).
- `[framing]` — the reversed wire framing (see below). Required, with all five
  keys; it is the single source of truth for how streams are split.
- `[crypto].static_key` — the AES-128-ECB key the client applies to the
  handshake key halves. It rotates on client updates; update it here when it
  changes (it lives in `GameAssembly.dll`, mirrored in the reference server's
  `transport.StaticKey`).

Both commands take `--config` to use another config file; the web app also
takes `--pcap-dir` to override the capture directory.

## Use

Drop captures into `decoder/pcap/` (git-ignored) and start the web app:

```bash
python3 -m decoder.web            # http://127.0.0.1:8000
```

Selecting a capture in the top-right picker **analyzes it on first open**
(framing detection + decrypt + decode) and caches the result under `cache_dir`;
a `●` in the picker marks captures already analyzed, and repeat opens are served
from cache. The cache invalidates itself when the pcap is edited or replaced, or
when the decoder's output changes (the signature is the file's size + mtime plus
`ANALYSIS_VERSION` in `pipeline.py` — bump it when you change what the decoder
produces). To force a fresh run, request `GET /api/pcaps/<name>?refresh=1`.

Two views:

- **Packets** — frames shown **chronologically** (C2S and S2C interleaved by
  capture timestamp, column `t` = seconds since the session's first packet), so
  a session reads as the actual request/response conversation. Filter by
  direction, opcode/name, size or hex bytes; select a frame for its hex dump, a
  byte interpreter (LE/BE) and the decoded field tree (or JSON).
- **Opcodes** — every opcode seen in the session with its count, size range and
  decode status (`mapped` / `partial` / `unknown`), a size histogram and a draft
  YAML schema.

If the framing detected in a capture differs from `[framing]`, the Packets view
shows a warning: the protocol may have changed.

The `analyze` CLI reverses framing on the terminal (e.g. after a client
update); it is not needed for browsing:

```bash
python3 -m decoder.analyze pcap/session.pcap        # framing brute-force + hexdump
```

## Wire framing (reversed from the sample captures)

Confirmed against `01-character.pcap` / `02-wrongname.pcap`: a **2-byte
little-endian length prefix that counts the whole frame** (prefix included),
followed by the body. This splits 100% of every half-stream in both directions
with zero remainder. Those values are locked into `[framing]` in `decoder.toml`.

[`framing.py`](src/decoder/framing.py) brute-forces length size/endianness,
header inclusion, and opcode endianness, scoring candidates by recognised
catalog opcodes. The web app runs it on every analysis (that is the mismatch
warning); `decoder.analyze` prints the ranking — use it to re-confirm framing
after a client update or on a new capture.

## Decryption

Frame bodies are encrypted with the **Rabbit** stream cipher (eSTREAM /
RFC 4503), one keystream per direction. The session is bootstrapped by a
plaintext handshake:

1. The server's first S2C frame is `S2C_CheckConnection`, sent in the clear. It
   carries `m_clientSendIV`, `m_serverSendIV` and the encryption-key halves
   `m_encryptionKeyLow` / `m_encryptionKeyHigh`.
2. `rawKey = LE64(low) || LE64(high)` (16 bytes);
   `bodyKey = AES-128-ECB-decrypt(static_key, rawKey)`.
3. Two Rabbit ciphers are seeded with `bodyKey` and the two IVs — one decrypts
   client-send frames, one server-send.
4. The handshake frame consumes no keystream; every other frame advances its
   direction's keystream to the next 16-byte boundary
   (`ceil(len/16)*16` bytes).

This is all in [`crypto.py`](src/decoder/crypto.py) (cipher, key derivation,
per-session cipher pair) and [`session.py`](src/decoder/session.py) (handshake
decode + per-direction decrypt). A handshake that is truncated or missing a key
field is reported as a session error instead of producing garbage. The port is byte-checked against the server's golden keystreams in
[`tests/test_crypto.py`](tests/test_crypto.py). Decryption is confirmed by the
client echoing the handshake's `socketUID` / `connectionKey` back in its first
`C2S_VersionCheck`.

## Field decoding

Full field-level decode, matching the server's `codec.go`:

- primitives (`System.Int32`, `Int64`, `Single`, `Boolean`, …);
- `System.String` (`u32` char count + UTF-16LE) and `System.Char[]`
  (NUL-terminated UTF-16LE);
- `List<T>` (`u32` count + elements), including lists of structs and strings;
- **enums** (decoded as their underlying integer);
- **fixed arrays** `T[]` (no length prefix — sized from `structs.FIXED_ARRAYS`);
- **nested structs**, resolved recursively;
- **inherited base fields**, base-class-first — notably the `__state__` header
  byte on every message inheriting `Network.IBSerializableList` (all the `*List`
  messages). `Network.IBSerializable.__id__` is *not* on the wire (denylisted),
  confirmed against captures.

Struct layouts, the inheritance chain and enum underlying types come from the
frida runtime dump (`resources/rom_dump.json`, `[paths].type_dump`);
[`structs.py`](src/decoder/structs.py) builds the catalog and
[`body.py`](src/decoder/body.py) consumes it. On the sample captures this
decodes **100%** of framed messages (nested item lists, equipment, etc.).

Remaining gaps are data, not code: a fixed array whose size isn't in
`FIXED_ARRAYS` (add it, or dump `.Length` via frida), or a genuinely new type.
Such a field is marked `unresolved`, the rest of that body stays hex, and
decoding stops there. Set `[paths].type_dump = ""` to fall back to
self-describing-only decode. The UI shows the raw body hex for every frame
regardless.

Note on reassembly: a capture that dropped packets leaves gaps in a stream;
those are zero-padded so offsets and the keystream stay aligned, but any frame
that straddles a gap decodes wrong. `capture.py` logs one summary per affected
stream.

## Layout

```
decoder/
    pcap/                     captures (git-ignored)
    decoder.toml              paths + framing + crypto config
    src/decoder/
        config.py             load decoder.toml
        catalog.py            load messages_typed.json, index by opcode
        capture.py            dpkt read + TCP half-stream reassembly
        crypto.py             Rabbit cipher + AES body-key derivation
        session.py            handshake parse + per-direction decrypt + decode
        structs.py            struct layouts / inheritance / enums from rom_dump.json
        framing.py            wire framing: frame split + framing detection
        body.py               message body / field decode
        analyze.py            framing brute-force CLI (hexdump + ranked framings)
        pipeline.py           per-capture analysis: detect + decode + shape
        cache.py              persistent per-capture analysis cache
        web.py                FastAPI app (analyze-on-select + cache)
        static/               Preact + htm UI as native ES modules (no build step)
            index.html        shell that loads app.css + app.js
            app.css           theme (color tokens on :root)
            app.js            entry: capture loading, view state, layout
            lib/preact.js     pinned Preact / htm CDN imports
            lib/format.js     pure helpers (hex, sizes, direction, text match)
            components/       header, packets (list + filters), detail, hex,
                              tree (field tree), opcodes, common (icon, copy, toggle)
    tests/                    pytest suite (mirrors the modules above)
    pyproject.toml
```

## Tests

```bash
pytest                   # unit tests: crypto golden vectors, framing, body and
                         # struct decoding, session round-trip, config, cache
uvx ruff format && uvx ruff check   # ruff is not a dev dependency
```

The tests synthesize their own data — no captures needed.
