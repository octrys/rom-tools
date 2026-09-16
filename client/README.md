# client

Tools for adjusting the ROM: Golden Age **client** files.

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

## Requirements

- Python 3.8+ (standard library only)
