"""py2app build config — used by scripts/build_app.sh.

Build with:
    python3 setup.py py2app

Do NOT replace pyproject.toml; this file exists solely so py2app has a setup()
to read. Day-to-day installs still go through `pip install -e .`.
"""
from setuptools import setup

# py2app 0.28 hard-rejects any install_requires on the distribution, but modern
# setuptools auto-fills it from pyproject.toml's [project.dependencies]. Strip
# it on the py2app command itself before its finalize_options check runs.
import py2app.build_app as _build_app  # noqa: E402

_orig_finalize = _build_app.py2app.finalize_options


def _patched_finalize(self):
    self.distribution.install_requires = None
    _orig_finalize(self)


_build_app.py2app.finalize_options = _patched_finalize


APP = ["scripts/chatlens_app.py"]

# Recursive: the whole static tree lands at Contents/Resources/static/.
DATA_FILES = ["src/chatlens/static"]

PLIST = {
    "CFBundleName": "ChatLens",
    "CFBundleDisplayName": "ChatLens",
    "CFBundleIdentifier": "net.chatlens.app",
    "CFBundleVersion": "0.1.17",
    "CFBundleShortVersionString": "0.1.17",
    "LSMinimumSystemVersion": "12.0",
    "NSHighResolutionCapable": True,
    "LSUIElement": False,
    "NSHumanReadableCopyright": "ChatLens — local-only.",
}

OPTIONS = {
    "argv_emulation": False,
    "iconfile": "assets/ChatLens.icns",
    "plist": PLIST,
    "packages": [
        "chatlens",
        "fastapi",
        "starlette",
        "uvicorn",
        "pydantic",
        "pydantic_core",
        "pydantic_settings",
        "sqlmodel",
        "sqlalchemy",
        "webview",
        # Bundled wechat-cli + its runtime deps. We invoke it via
        # `Contents/MacOS/python -m wechat_cli`, so no PATH/console-script
        # gymnastics needed at runtime.
        "wechat_cli",
        "Crypto",       # pycryptodome
        "zstandard",
        # PDF export (chatlens.pdfexport) — fpdf2 is imported lazily, so list
        # it explicitly for py2app's static scanner. Pure Python; no dylibs.
        "fpdf",
    ],
    "includes": [
        "sqlalchemy.dialects.sqlite",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.lifespan.on",
        "h11",
        "websockets",
        "anyio",
        # anyio loads its backend lazily via importlib at first run_sync call;
        # py2app's static scanner misses it and FileResponse for sync routes
        # blows up with ModuleNotFoundError. Force-include.
        "anyio._backends",
        "anyio._backends._asyncio",
        "sniffio",
        "click",
        "dotenv",
    ],
    "excludes": ["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6"],
}

setup(
    app=APP,
    name="ChatLens",
    version="0.1.17",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
)
