"""py2app entry point — invoked by the launcher inside ChatLens.app."""
import json
import subprocess
import sys
from pathlib import Path


def _refuse_dmg_launch_if_needed() -> None:
    """If ChatLens is launched directly from a mounted .dmg (or any other
    read-only `/Volumes/` mount) without first being dragged to
    /Applications, show a dialog and bail.

    Why: when the user double-clicks the .app inside the mounted .dmg,
    everything *appears* to work — until they eject the .dmg. After that,
    relaunch finds a now-broken alias, the dashboard data in
    ~/Library/Application Support/ChatLens/ is orphaned, and the user has
    no easy way to fix it. Catching this at startup with a clear "please
    drag me to Applications first" dialog is much kinder than letting them
    debug an empty-window problem later.

    Skipped in source mode (sys.frozen is False) and when the .app sits
    anywhere other than /Volumes/* — that covers /Applications, ~/Desktop,
    ~/Downloads, etc.
    """
    if not getattr(sys, "frozen", False):
        return
    exe = Path(sys.executable).resolve()
    if not str(exe).startswith("/Volumes/"):
        return
    msg = (
        "请先把 ChatLens 拖到「应用程序 / Applications」文件夹再启动。\n\n"
        "现在你是直接从挂载的 .dmg 里跑的 —— 等你弹出 .dmg，"
        "ChatLens 就消失了，数据库也会变成孤儿。\n\n"
        "1. 把 .dmg 窗口里的 ChatLens 拖到右边的 Applications 文件夹\n"
        "2. 弹出 .dmg\n"
        "3. 在「应用程序」里双击 ChatLens"
    )
    # `display dialog` is modal and blocks until the user clicks. The
    # osascript script string is double-quoted; json.dumps gives us an
    # AppleScript-safe string literal for the message body.
    try:
        subprocess.run(
            [
                "osascript", "-e",
                (
                    f"display dialog {json.dumps(msg)} "
                    'buttons {"我知道了"} default button 1 '
                    'with icon caution with title "请先安装到应用程序"'
                ),
            ],
            check=False, timeout=60,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        # If osascript itself is missing or fails (extremely unlikely on
        # macOS), exit silently — better than crashing the launcher with a
        # cryptic Python traceback in Console.app.
        pass
    sys.exit(0)


_refuse_dmg_launch_if_needed()

from chatlens.main import run  # noqa: E402

if __name__ == "__main__":
    run()
