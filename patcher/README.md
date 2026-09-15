# patcher

Tools for the ROM: Golden Age patch server (`https://patch.romgoldenage.com`).

## patch_downloader.py

Mirrors a release's files from the patch server: read `manifest.json` and
download each per-file zip as-is. The archives are kept compressed — they are
**not** extracted. By default it mirrors both the `launcher` and `patch` (game
client) components.

Layout (default release `NewPCwemix`, per `<component>`):

```
manifest: https://patch.romgoldenage.com/NewPCwemix/Real/<component>/manifest.json
archive:  https://patch.romgoldenage.com/NewPCwemix/Real/<component>/<Archive>
local:    resources/patch/NewPCwemix/Real/<component>/<Archive>
```

Each downloaded zip is checked against `ArchiveSize` from the manifest.
Archives already present locally with that size are skipped, so re-running only
fetches what changed. The `patch` component is ~2 GB across 123 files.

### Usage

```bash
# default: release NewPCwemix, env Real, components launcher + patch
python3 patch_downloader.py

# a single component
python3 patch_downloader.py --component launcher

# other release / env
python3 patch_downloader.py --release NewPCwemix --env Real

# custom mirror root
python3 patch_downloader.py --out /path/to/resources/patch
```

The launcher's `appsettings.json` (inside `appsettings.json.zip`) is an `ENC:`
blob — once unzipped, decrypt it with
[`../launcher/appsettings_crypto.py`](../launcher/appsettings_crypto.py).

### Requirements

- Python 3.8+ (standard library only)
