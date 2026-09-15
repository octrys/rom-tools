#!/usr/bin/env python3
"""Mirror files from the ROM: Golden Age patch server.

The launcher's ``GameUpdater`` fetches a ``manifest.json`` and, for each entry,
downloads a per-file zip archive from the same directory. This script mirrors
that: it reads the manifest and downloads every ``Archive`` as-is, saved under a
local mirror that follows the remote layout. The archives are kept compressed —
they are not extracted.

By default it mirrors both the ``launcher`` and ``patch`` (game client)
components.

Layout, for the default release ``NewPCwemix`` and component ``<component>``:

    manifest: https://patch.romgoldenage.com/NewPCwemix/Real/<component>/manifest.json
    archive:  https://patch.romgoldenage.com/NewPCwemix/Real/<component>/<Archive>
    local:    resources/patch/NewPCwemix/Real/<component>/<Archive>

Each manifest entry (matching ``Launcher.Shared.ManifestFile``):

    Path         final file path, relative to the component root (once extracted)
    Size         size of the extracted file
    Sha256       SHA-256 of the extracted file
    Archive      relative path of the zip on the server (usually "<Path>.zip")
    ArchiveSize  size of the zip

Downloads are verified against ``ArchiveSize``. An archive already present
locally with that size is skipped, so the mirror is idempotent and resumable
across runs.
"""

from __future__ import annotations

import argparse
import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urljoin

PATCH_HOST = "https://patch.romgoldenage.com"
RELEASE = "NewPCwemix"
ENV = "Real"
COMPONENTS = ("launcher", "patch")

# The launcher sends this UA on every patch request.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

logger = logging.getLogger("patch_downloader")


class PatchError(Exception):
    """A patch could not be fetched, verified, or extracted."""


def _http_get(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read()


def fetch_manifest(base_url: str) -> dict:
    """Download and parse ``manifest.json`` from ``base_url``."""
    manifest_url = urljoin(base_url, "manifest.json")
    logger.info("Fetching manifest: %s", manifest_url)
    return json.loads(_http_get(manifest_url).decode("utf-8"))


def download_entry(entry: dict, base_url: str, dest_root: Path) -> bool:
    """Download and verify one manifest entry's zip archive (kept compressed).

    The archive is saved as-is, mirroring the remote structure; it is not
    extracted. Returns ``True`` if a file was written, ``False`` if it was
    already present and up to date.
    """
    archive_rel = entry["Archive"]
    archive_size = entry["ArchiveSize"]
    dest_path = dest_root / archive_rel

    if dest_path.is_file() and dest_path.stat().st_size == archive_size:
        logger.info("up to date  %s", archive_rel)
        return False

    # Archive names may contain spaces and other characters that must be
    # percent-encoded in the URL (the on-disk path keeps the raw name).
    archive_url = urljoin(base_url, quote(archive_rel, safe="/"))
    logger.info("downloading %s", archive_rel)
    archive_bytes = _http_get(archive_url)

    if len(archive_bytes) != archive_size:
        raise PatchError(
            f"{archive_rel}: archive size {len(archive_bytes)} != "
            f"manifest {archive_size}"
        )

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(archive_bytes)
    logger.info("saved       %s  (%d bytes)", archive_rel, len(archive_bytes))
    return True


def download_patch(base_url: str, dest_root: Path) -> None:
    """Mirror every file in the manifest at ``base_url`` into ``dest_root``."""
    manifest = fetch_manifest(base_url)
    files = manifest.get("Files") or []
    logger.info("manifest version %s, %d files", manifest.get("Version"), len(files))

    dest_root.mkdir(parents=True, exist_ok=True)
    (dest_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    written = 0
    for entry in files:
        if download_entry(entry, base_url, dest_root):
            written += 1

    logger.info("done: %d downloaded, %d already current", written, len(files) - written)


def build_base_url(host: str, release: str, env: str, component: str) -> str:
    return f"{host.rstrip('/')}/{release}/{env}/{component}/"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--release", default=RELEASE, help=f"release (default: {RELEASE})")
    parser.add_argument("--env", default=ENV, help=f"environment (default: {ENV})")
    parser.add_argument("--component", nargs="+", default=list(COMPONENTS),
                        help=f"components to mirror (default: {' '.join(COMPONENTS)})")
    parser.add_argument("--host", default=PATCH_HOST, help=f"patch host (default: {PATCH_HOST})")
    parser.add_argument("--out", default="resources/patch", type=Path,
                        help="local mirror root (default: resources/patch)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    for component in args.component:
        base_url = build_base_url(args.host, args.release, args.env, component)
        dest_root = args.out / args.release / args.env / component
        logger.info("=== component: %s ===", component)
        try:
            download_patch(base_url, dest_root)
        except (PatchError, OSError, urllib.error.URLError) as error:
            logger.error("failed (%s): %s", component, error)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
