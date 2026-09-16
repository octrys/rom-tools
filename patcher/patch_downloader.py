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

# Standalone files mirrored as-is, relative to the patch host. Unlike a
# component manifest, these have no archive/size metadata, so they are fetched
# and overwritten on every run.
EXTRA_FILES = (
    "real/ROMGoldenAge_WemixPay_Crypto.json",
    "real/maintenances.json",
    "real/patch/Windows/table.dat",
    "real/patch/Windows/AssetBundlesVersion.txt",
    "real/patch/Windows/bundlegamedata.dat",
    "real/patch/Windows/lobby.dat",
    "real/patch/Windows/tablecrypto.dat",
    "real/patch/Windows/defult.dat",
)

# Movie index listing the video files under ``moviecrypto/``. Each entry carries
# an ``m_FileSize`` used to verify and skip already-mirrored movies. The index
# itself is mirrored alongside the movies it references.
MOVIE_INDEX = "real/patch/Windows/moviecrypto/MovieFile.json"

# Asset-bundle index. ``keys`` are bundle paths (some with subdirectories),
# ``values`` carry the matching ``m_BundleSize`` used to verify and skip already
# mirrored bundles. Bundles are served from the index's own directory.
BUNDLE_INDEX = "real/patch/Windows/BundleInfo.dat"

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


def _http_download(url: str, dest_path: Path) -> int:
    """Stream ``url`` to ``dest_path`` (large files are not buffered in memory).

    The download is written to a temporary sibling and moved into place only on
    success, so an interrupted run never leaves a truncated file behind. Returns
    the number of bytes written.
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
    )
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_name(dest_path.name + ".part")
    written = 0
    with urllib.request.urlopen(request, timeout=300) as response:
        with tmp_path.open("wb") as out_file:
            while chunk := response.read(1 << 20):
                out_file.write(chunk)
                written += len(chunk)
    tmp_path.replace(dest_path)
    return written


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


def download_file(host: str, rel_path: str, out_root: Path) -> None:
    """Mirror a standalone file at ``host/rel_path`` into ``out_root``.

    The file is saved under ``out_root`` following its remote path and is
    fetched fresh on every run (no manifest metadata to verify against).
    """
    file_url = urljoin(f"{host.rstrip('/')}/", quote(rel_path, safe="/"))
    dest_path = out_root / rel_path
    logger.info("downloading %s", rel_path)
    file_bytes = _http_get(file_url)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(file_bytes)
    logger.info("saved       %s  (%d bytes)", rel_path, len(file_bytes))


def download_movies(host: str, index_rel: str, out_root: Path) -> None:
    """Mirror the movie index at ``index_rel`` and every movie it lists.

    The index (``MovieFile.json``) and the movies live in the same directory.
    Each movie is verified against its ``m_FileSize`` and skipped when already
    present locally at that size, so the mirror is idempotent and resumable.
    """
    base = f"{host.rstrip('/')}/"
    index_url = urljoin(base, quote(index_rel, safe="/"))
    logger.info("downloading %s", index_rel)
    index_bytes = _http_get(index_url)

    index_path = out_root / index_rel
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_bytes(index_bytes)

    movie_dir = Path(index_rel).parent
    items = json.loads(index_bytes.decode("utf-8")).get("items") or []
    logger.info("movie index: %d movies", len(items))

    written = 0
    for item in items:
        filename = item["m_Filename"]
        size = item["m_FileSize"]
        rel_path = f"{movie_dir.as_posix()}/{filename}"
        dest_path = out_root / rel_path

        if dest_path.is_file() and dest_path.stat().st_size == size:
            logger.info("up to date  %s", filename)
            continue

        movie_url = urljoin(base, quote(rel_path, safe="/"))
        logger.info("downloading %s  (%d bytes)", filename, size)
        movie_bytes = _http_get(movie_url)

        if len(movie_bytes) != size:
            raise PatchError(
                f"{filename}: size {len(movie_bytes)} != index {size}"
            )

        dest_path.write_bytes(movie_bytes)
        written += 1

    logger.info("movies done: %d downloaded, %d already current", written, len(items) - written)


def download_bundles(host: str, index_rel: str, out_root: Path) -> None:
    """Mirror the asset-bundle index at ``index_rel`` and every bundle it lists.

    ``BundleInfo.dat`` holds parallel ``keys`` (bundle paths) and ``values``
    (each with an ``m_BundleSize``); bundles are served from the index's own
    directory. Each bundle is streamed to disk, verified against its
    ``m_BundleSize``, and skipped when already present at that size, so the
    mirror is idempotent and resumable across runs.
    """
    base = f"{host.rstrip('/')}/"
    index_url = urljoin(base, quote(index_rel, safe="/"))
    logger.info("downloading %s", index_rel)
    index_bytes = _http_get(index_url)

    index_path = out_root / index_rel
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_bytes(index_bytes)

    index = json.loads(index_bytes.decode("utf-8"))
    keys = index.get("keys") or []
    values = index.get("values") or []
    if len(keys) != len(values):
        raise PatchError(
            f"{index_rel}: keys ({len(keys)}) and values ({len(values)}) "
            "length mismatch"
        )

    bundle_dir = Path(index_rel).parent
    logger.info("bundle index: %d bundles", len(keys))

    written = 0
    for key, value in zip(keys, values):
        size = value["m_BundleSize"]
        rel_path = f"{bundle_dir.as_posix()}/{key}"
        dest_path = out_root / rel_path

        if dest_path.is_file() and dest_path.stat().st_size == size:
            logger.info("up to date  %s", key)
            continue

        bundle_url = urljoin(base, quote(rel_path, safe="/"))
        logger.info("downloading %s  (%d bytes)", key, size)
        downloaded = _http_download(bundle_url, dest_path)

        if downloaded != size:
            dest_path.unlink(missing_ok=True)
            raise PatchError(f"{key}: size {downloaded} != index {size}")

        written += 1

    logger.info("bundles done: %d downloaded, %d already current", written, len(keys) - written)


def build_base_url(host: str, release: str, env: str, component: str) -> str:
    return f"{host.rstrip('/')}/{release}/{env}/{component}/"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--release", default=RELEASE, help=f"release (default: {RELEASE})")
    parser.add_argument("--env", default=ENV, help=f"environment (default: {ENV})")
    parser.add_argument("--component", nargs="+", default=list(COMPONENTS),
                        help=f"components to mirror (default: {' '.join(COMPONENTS)})")
    parser.add_argument("--host", default=PATCH_HOST, help=f"patch host (default: {PATCH_HOST})")
    parser.add_argument("--extra-file", nargs="+", default=list(EXTRA_FILES),
                        help="standalone files to mirror, relative to the host "
                             f"(default: {' '.join(EXTRA_FILES)})")
    parser.add_argument("--movie-index", default=MOVIE_INDEX,
                        help=f"movie index to mirror with its movies (default: {MOVIE_INDEX})")
    parser.add_argument("--no-movies", action="store_true",
                        help="skip mirroring the movie index and its movies")
    parser.add_argument("--bundle-index", default=BUNDLE_INDEX,
                        help=f"asset-bundle index to mirror with its bundles (default: {BUNDLE_INDEX})")
    parser.add_argument("--no-bundles", action="store_true",
                        help="skip mirroring the bundle index and its bundles")
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

    for rel_path in args.extra_file:
        logger.info("=== file: %s ===", rel_path)
        try:
            download_file(args.host, rel_path, args.out)
        except (OSError, urllib.error.URLError) as error:
            logger.error("failed (%s): %s", rel_path, error)
            return 1

    if not args.no_movies:
        logger.info("=== movies: %s ===", args.movie_index)
        try:
            download_movies(args.host, args.movie_index, args.out)
        except (PatchError, OSError, urllib.error.URLError) as error:
            logger.error("failed (movies): %s", error)
            return 1

    if not args.no_bundles:
        logger.info("=== bundles: %s ===", args.bundle_index)
        try:
            download_bundles(args.host, args.bundle_index, args.out)
        except (PatchError, OSError, urllib.error.URLError) as error:
            logger.error("failed (bundles): %s", error)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
