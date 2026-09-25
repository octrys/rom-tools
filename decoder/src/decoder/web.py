"""FastAPI app: pick a capture and inspect its decoded messages in the browser.

Endpoints:
    GET /                       the single-page inspector UI
    GET /api/pcaps              list capture files under the pcap directory
    GET /api/pcaps/{name}       reassemble, frame and decode one capture

Run it with:
    python3 -m decoder.web            # serves on http://127.0.0.1:8000
    python3 -m decoder.web --port 9000 --pcap-dir /path/to/pcap
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .cache import AnalysisCache
from .config import Config, load_config
from .pipeline import ANALYSIS_VERSION, Analyzer

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_CAPTURE_SUFFIXES = {".pcap", ".pcapng", ".cap"}


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="ROM decoder")
    analyzer = Analyzer.from_config(config)
    cache = AnalysisCache(config.cache_dir, ANALYSIS_VERSION)
    pcap_dir = config.pcap_dir

    def _capture_path(name: str) -> Path:
        path = pcap_dir / name
        # Guard against path traversal — only files directly in pcap_dir.
        if path.parent != pcap_dir or not path.is_file():
            raise HTTPException(status_code=404, detail="capture not found")
        return path

    @app.get("/api/pcaps")
    def list_pcaps() -> dict[str, list[dict[str, object]]]:
        if not pcap_dir.is_dir():
            return {"pcaps": []}
        entries = []
        for path in sorted(pcap_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in _CAPTURE_SUFFIXES:
                continue
            entries.append({"name": path.name, "analyzed": cache.is_fresh(path)})
        return {"pcaps": entries}

    @app.get("/api/pcaps/{name}")
    def decode_pcap(name: str, refresh: bool = False) -> dict[str, object]:
        path = _capture_path(name)
        # Taken before analyzing, so a capture edited mid-analysis stays stale.
        signature = cache.signature(path)

        if not refresh:
            cached = cache.load(path, signature)
            if cached is not None:
                cached["cached"] = True
                return cached

        result = analyzer.analyze(path)
        cache.store(path, result, signature)
        result["cached"] = False
        return result

    @app.middleware("http")
    async def revalidate_static(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # The UI is plain ES modules with no build step or hashed names: make the
        # browser revalidate (cheap 304s via ETag) so an edit is never stale.
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=None, help="path to decoder.toml"
    )
    parser.add_argument(
        "--pcap-dir", type=Path, default=None, help="override [paths].pcap_dir"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = load_config(args.config)
    if args.pcap_dir:
        config = dataclasses.replace(config, pcap_dir=args.pcap_dir.resolve())
    uvicorn.run(create_app(config), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
