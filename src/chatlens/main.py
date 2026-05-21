"""FastAPI application entry point. Run with: `chatlens` (see pyproject scripts)."""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from . import wechat
from .config import settings
from .db import init_db
from .routers import assets, groups, jobs as jobs_router, preferences as prefs_router, setup as setup_router

# When ChatLens runs as a py2app .app bundle, stderr is discarded by Finder, so
# `logging.basicConfig` alone leaves us blind to crashes (e.g. the `claude -p`
# timeout that prompted writing this). Write to a rotating file in data_dir as
# well so post-mortem debugging doesn't depend on launching from a terminal.
_log_path = settings.data_dir / "chatlens.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(_log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
    ],
)
log = logging.getLogger("chatlens")
log.info("chatlens starting — logs at %s", _log_path)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ok, ver = await wechat.is_available()
    log.info("wechat-cli available=%s version=%r path=%s", ok, ver, settings.wechat_cli_path)
    if not ok:
        log.warning("wechat-cli not found — group sync will fail until you set WECHAT_CLI_PATH")
    yield


app = FastAPI(title="ChatLens", lifespan=lifespan)

app.include_router(groups.router)
app.include_router(assets.router)
app.include_router(jobs_router.router)
app.include_router(prefs_router.router)
app.include_router(setup_router.router)


@app.get("/api/health")
async def health() -> dict:
    from . import __version__, llm
    return {
        "ok": True,
        "version": __version__,
        "wechat_cli": await wechat.status(),
        "claude_cli": llm.health(),
    }


@app.get("/api/media/{asset_id}/{filename}")
def serve_media(asset_id: str, filename: str) -> FileResponse:
    # Defence-in-depth against path traversal — only allow simple names.
    if not asset_id.replace("-", "").isalnum():
        raise HTTPException(400, "invalid asset id")
    if "/" in filename or ".." in filename or filename.startswith("."):
        raise HTTPException(400, "invalid filename")
    p = (settings.data_dir / "media" / asset_id / filename).resolve()
    base = (settings.data_dir / "media").resolve()
    if not str(p).startswith(str(base) + "/") or not p.is_file():
        raise HTTPException(404, "media not found")
    return FileResponse(p)


@app.post("/api/media/{asset_id}/{filename}/save")
def save_media_to_downloads(asset_id: str, filename: str) -> dict:
    """Copy a media image into ~/Downloads.

    The reliable way to "download" an image from inside the pywebview native
    window — a browser-style `<a download>` is a no-op there, the same way the
    `window.open` the image lightbox replaced was. Mirrors the PDF-export flow.
    """
    if not asset_id.replace("-", "").isalnum():
        raise HTTPException(400, "invalid asset id")
    if "/" in filename or ".." in filename or filename.startswith("."):
        raise HTTPException(400, "invalid filename")
    p = (settings.data_dir / "media" / asset_id / filename).resolve()
    base = (settings.data_dir / "media").resolve()
    if not str(p).startswith(str(base) + "/") or not p.is_file():
        raise HTTPException(404, "media not found")
    downloads = Path.home() / "Downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    dst = downloads / filename
    if dst.exists():
        stem, suffix = dst.stem, dst.suffix
        n = 1
        while dst.exists():
            dst = downloads / f"{stem}-{n}{suffix}"
            n += 1
    shutil.copy2(p, dst)
    return {"ok": True, "filename": dst.name, "path": str(dst)}


def _static_dir() -> Path:
    # In a py2app bundle, data_files land in Contents/Resources/; from source
    # the static tree lives next to this module.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent / "Resources" / "static"
    return Path(__file__).parent / "static"


STATIC_DIR = _static_dir()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/assets/{name}.js")
def static_js(name: str) -> FileResponse:
    # Only allow simple names; falls back to 404 from FileResponse otherwise.
    if not name.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, "bad name")
    return FileResponse(STATIC_DIR / "assets" / f"{name}.js", media_type="application/javascript")


@app.get("/{full_path:path}", include_in_schema=False)
def spa_fallback(full_path: str) -> FileResponse:  # noqa: ARG001
    return FileResponse(STATIC_DIR / "index.html")


def _open_browser_when_ready(url: str) -> None:
    time.sleep(0.8)
    try:
        webbrowser.open(url)
    except Exception as e:
        log.warning("could not auto-open browser: %s", e)


def _wait_for_port(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _stop_previous_chatlens(host: str, port: int) -> None:
    """If a previous ChatLens server still owns our port, terminate it.

    Safety: confirms it's actually chatlens by probing /api/health first, so we
    never kill an unrelated process that happens to be on this port.
    """
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=2) as r:
            body = json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return  # nothing there, or doesn't look like an HTTP server
    if not isinstance(body, dict) or "wechat_cli" not in body:
        return  # something else is on the port — leave it alone

    me = os.getpid()
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return
    pids = {int(p) for p in result.stdout.split() if p.strip().isdigit()} - {me}
    for pid in pids:
        log.info("terminating previous chatlens listener pid=%d on port %d", pid, port)
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue

    # Wait briefly for the socket to free so our own bind succeeds.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((host, port))
            return
        except OSError:
            time.sleep(0.1)
    log.warning("port %d still busy after terminating previous listener(s)", port)


def _is_bundled() -> bool:
    return bool(getattr(sys, "frozen", False))


def run() -> None:
    url = f"http://localhost:{settings.port}/"

    # If a previous chatlens instance (source-mode dev server, another bundled
    # launch, etc.) still owns the port, replace it. Without this the new
    # process silently shares stale code with the old one.
    _stop_previous_chatlens(settings.host, settings.port)

    if _is_bundled():
        # In a packaged .app: run uvicorn on a background thread and show a
        # native window. pywebview must own the main thread on macOS.
        import webview  # imported lazily so source runs don't need it

        def _serve() -> None:
            uvicorn.run("chatlens.main:app", host=settings.host, port=settings.port, reload=False, log_config=None)

        threading.Thread(target=_serve, daemon=True).start()
        if not _wait_for_port(settings.host, settings.port):
            log.error("backend did not come up on %s:%s", settings.host, settings.port)
        log.info("ChatLens window opening at %s", url)
        # text_select=True: pywebview defaults this to False, which makes the
        # WKWebView swallow text selection entirely. We're rendering Markdown
        # the user will want to copy out of, so opt in.
        webview.create_window(
            "ChatLens", url, width=1280, height=860, min_size=(900, 600),
            text_select=True,
        )
        webview.start()
        return

    if settings.auto_open_browser:
        threading.Thread(target=_open_browser_when_ready, args=(url,), daemon=True).start()
    log.info("ChatLens starting at %s", url)
    uvicorn.run("chatlens.main:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    run()
