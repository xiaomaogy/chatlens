"""First-launch setup helpers.

Only thing exposed today is the wechat-cli init flow: ChatLens bundles
wechat-cli but the init step (which scans WeChat process memory to extract
encryption keys) needs sudo, which a GUI can't easily prompt for without
PyObjC bridging. So we hand off to Terminal.app and let the user
authenticate there.

Why `open -n -a Terminal /tmp/chatlens-init.command` instead of
`osascript tell Terminal to do script`:
  - osascript's `do script` reuses the user's existing Terminal process if
    one is running. That process started BEFORE the user granted App
    Management / Full Disk Access to Terminal, so it doesn't yet have those
    TCC permissions — sudo wechat-cli init then hits codesign
    `Operation not permitted` even though Settings says Terminal is allowed.
  - `open -n` forces a brand-new Terminal.app instance. Fresh process =
    fresh TCC query = the just-granted permissions take effect immediately.
    User doesn't have to ⌘Q their existing Terminal first.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException

from .. import wechat

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/setup", tags=["setup"])


def _init_command(force: bool = False) -> str:
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

    force=True appends `--force` to make wechat-cli re-extract keys even when
    `~/.wechat-cli/all_keys.json` already exists. Used by the keys_partial
    recovery flow when the user wants to backfill missing DB keys after
    clicking into more chats.
    """
    parts = wechat.wechat_cli_invocation()
    env = wechat.wechat_cli_env()
    quoted_cmd = " ".join(shlex.quote(p) for p in parts)
    env_pairs = [
        f"{var}={shlex.quote(env[var])}"
        for var in ("PYTHONHOME", "PYTHONPATH")
        if env.get(var)
    ]
    init_args = "init --force" if force else "init"
    if env_pairs:
        return f"sudo env {' '.join(env_pairs)} {quoted_cmd} {init_args}"
    return f"sudo {quoted_cmd} {init_args}"


@router.get("/init-command")
def get_init_command(force: bool = False) -> dict:
    """Surface the exact init command so the wizard can show it for
    copy-paste in addition to the one-click Terminal launcher."""
    return {"command": _init_command(force=force)}


# Path to the .command launcher script. Placed in $TMPDIR so it gets cleaned
# up by macOS's periodic daily script after 3 days; we overwrite it every
# time anyway so persistence doesn't matter.
_LAUNCHER_PATH = Path(tempfile.gettempdir()) / "chatlens-init.command"


@router.post("/run-init")
async def run_init(force: bool = False) -> dict:
    """Open a brand-new Terminal instance and run the init command in it.

    The launcher script writes a tiny `.command` file then `open -n -a
    Terminal` runs it. The user enters their sudo password in Terminal; we
    don't see or handle it.

    Why a `.command` file + open -n instead of `osascript do script`: see
    module docstring. Short version: `open -n` forces a fresh Terminal
    process, which picks up TCC grants (App Management, Full Disk Access)
    the user just made in System Settings. Reusing an existing Terminal
    process means the grants don't take effect until the user ⌘Qs and
    reopens it manually.

    Returns the command we wrote into the script so the UI can display it
    (and the user can re-paste it manually if Terminal didn't open).
    """
    cmd = _init_command(force=force)
    # Body of the .command file. `clear` keeps the new window tidy. Banner
    # makes it obvious to the user which Terminal window is ours.
    body = (
        "#!/bin/bash\n"
        "clear\n"
        "echo '========================================'\n"
        "echo 'ChatLens — running wechat-cli init'\n"
        "echo '========================================'\n"
        "echo\n"
        f"{cmd}\n"
        "STATUS=$?\n"
        "echo\n"
        "if [ $STATUS -eq 0 ]; then\n"
        "  echo '✓ init exited cleanly. You can close this window and click'\n"
        "  echo '  \"我已完成\" back in ChatLens.'\n"
        "else\n"
        "  echo \"✗ init exited with status $STATUS — see output above.\"\n"
        "fi\n"
        "echo\n"
        "echo 'Press Enter to close.'\n"
        "read\n"
    )
    try:
        _LAUNCHER_PATH.write_text(body)
        _LAUNCHER_PATH.chmod(0o755)
    except OSError as e:
        log.warning("could not stage launcher script %s: %s", _LAUNCHER_PATH, e)
        raise HTTPException(500, f"无法准备启动脚本: {e}")

    # -n forces a new Terminal instance even if Terminal is already running.
    # That new process queries TCC on launch, so the App Management / FDA
    # grants the user just made in Settings are honoured immediately —
    # no need for the user to ⌘Q their existing Terminal.
    proc = await asyncio.create_subprocess_exec(
        "open", "-n", "-a", "Terminal", str(_LAUNCHER_PATH),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        msg = stderr.decode(errors="replace").strip() or "open exited non-zero"
        log.warning("could not open Terminal: %s", msg)
        raise HTTPException(500, f"无法打开 Terminal: {msg[:200]}")
    return {"command": cmd}
