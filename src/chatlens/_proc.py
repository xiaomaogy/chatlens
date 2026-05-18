"""Helpers for spawning external CLIs from a py2app bundle.

The bundle's launcher sets PYTHONHOME / PYTHONPATH (and sometimes DYLD_* paths)
so the embedded Python loads from inside Contents/Resources. Those settings
poison any child process that has its own Python interpreter — pipx-installed
CLIs such as `wechat-cli`, or the `claude` CLI when it shells back to Node —
because they pick up the bundle's stdlib and crash on stale dylibs.

Use `child_env()` whenever spawning external tools so they see a clean
environment.
"""
from __future__ import annotations

import os

_BAD_PREFIXES = ("PYTHON",)
_BAD_KEYS = {
    "DYLD_LIBRARY_PATH",
    "DYLD_FRAMEWORK_PATH",
    "DYLD_FALLBACK_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
}


def child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items()
            if not k.startswith(_BAD_PREFIXES) and k not in _BAD_KEYS}
