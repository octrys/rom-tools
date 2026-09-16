# rom-tools

Tools for the **ROM: Golden Age** client and launcher — decrypting launcher
config and mirroring files from the official patch server.

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

### [`client/`](client/) — client host redirect

Adjust the client's `global-metadata.dat` and `resources.assets` so the game
resolves its infrastructure hosts to your own server instead of
`patch.romgoldenage.com` / `auth.romgoldenage.com`. The patch/CDN host lives in
two places — an IL2CPP string literal (base A) and a Unity
`ProjectSettingData_Crypto_Win_Live` string (base B); `resources.assets` also
holds the auth host string. `resources_host.py` rewrites the patch and auth
strings in a single pass (`--patch-host` / `--auth-host`). Everything is edited in
place, emitting a patched copy without ever overwriting the source.

```bash
# base A — metadata literal
python3 client/metadata_host.py global-metadata.dat --new-host 192.168.1.50 --out global-metadata.patched.dat

# base B — resources.assets (patch + auth hosts, each exact byte length; no padding)
python3 client/resources_host.py resources.assets --patch-host patch.example.internal --auth-host auth.example.internal --out resources.assets.patched
```

Standard library only. Details in [`client/README.md`](client/README.md).

## Layout

```
client/              client host redirect (metadata + resources)
launcher/            launcher config decrypt/encrypt
patcher/             patch-server mirror downloader
resources/launcher/  launcher configs (encrypted + decrypted)
tmp/                 scratch for bulk patch downloads (git-ignored)
```
