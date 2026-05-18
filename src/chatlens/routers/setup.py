"""First-launch setup helpers.

Only thing exposed today is the wechat-cli init flow: ChatLens bundles
wechat-cli but the init step (which scans WeChat process memory to extract
encryption keys) needs sudo, which a GUI can't easily prompt for without
PyObjC bridging. So we hand off to Terminal.app via osascript and let the
user authenticate there.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shlex

from fastapi import APIRouter, HTTPException

from .. import wechat

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/setup", tags=["setup"])


def _init_command() -> str:
    """The shell command the user (or osascript) should run to initialise
    wechat-cli. Uses the bundled python in frozen builds; falls back to the
    PATH-resolved `wechat-cli` script in source mode.

    In bundled mode we wrap with `sudo env PYTHONHOME=… PYTHONPATH=… …` because
    macOS sudo strips both vars by default. Without PYTHONPATH the bundled
    interpreter can't find the `wechat_cli` module; without PYTHONHOME it can't
    even find its own stdlib and dies with `No module named 'encodings'`
    (it falls back to the build-machine's compile-time prefix, which doesn't
    exist on the user's machine). Forward every bundle-essential var that the
    in-process subprocess env carries.
    """
    parts = wechat.wechat_cli_invocation()
    env = wechat.wechat_cli_env()
    quoted_cmd = " ".join(shlex.quote(p) for p in parts)
    env_pairs = [
        f"{var}={shlex.quote(env[var])}"
        for var in ("PYTHONHOME", "PYTHONPATH")
        if env.get(var)
    ]
    if env_pairs:
        return f"sudo env {' '.join(env_pairs)} {quoted_cmd} init"
    return f"sudo {quoted_cmd} init"


@router.get("/init-command")
def get_init_command() -> dict:
    """Surface the exact init command so the wizard can show it for
    copy-paste in addition to the one-click Terminal launcher."""
    return {"command": _init_command()}


@router.post("/run-init")
async def run_init() -> dict:
    """Open Terminal.app and start the init command inside it. The user
    enters their sudo password in Terminal; we don't see or handle it.

    Returns the command we asked Terminal to run so the UI can display it
    (and the user can re-paste it manually if Terminal didn't open for any
    reason — e.g. AppleScript permissions denied).
    """
    cmd = _init_command()
    # The outer osascript invocation passes a single -e string to AppleScript.
    # json.dumps gives us a correctly-quoted AppleScript string literal that
    # survives both shells (we go through asyncio.create_subprocess_exec, not
    # the shell, but Terminal's `do script` still re-parses the command).
    osa = f'tell application "Terminal" to do script {json.dumps(cmd)}\n' \
          'tell application "Terminal" to activate'
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", osa,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        # Most likely: AppleScript automation permission was denied. The user
        # can still complete setup by copying the command shown in the wizard.
        msg = stderr.decode(errors="replace").strip() or "osascript exited non-zero"
        log.warning("could not open Terminal for init: %s", msg)
        raise HTTPException(500, f"无法打开 Terminal: {msg[:200]}")
    return {"command": cmd}
