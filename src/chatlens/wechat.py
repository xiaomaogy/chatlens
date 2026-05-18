"""Thin async-friendly wrapper around the local `wechat-cli` binary.

All access to the user's WeChat data goes through this module — routers
should never spawn `wechat-cli` directly.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._proc import child_env
from .config import settings

log = logging.getLogger(__name__)

# A single line in `wechat-cli history` looks like:
#   [2026-05-13 14:53] 环宇: [图片] (local_id=9784)
HISTORY_LINE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?)\]\s+"
    r"(?P<sender>[^:]+):\s+(?P<text>.*)$"
)

# With --media, image lines look like:
#   [图片] /var/folders/.../wechat_cli_media/5ac8fd2834a054902334155e.jpg (缩略图)
MEDIA_PATH = re.compile(r"\[图片\]\s+(?P<path>\S+\.(?:jpg|jpeg|png|gif|webp))(\s+\([^)]*\))?")

# Reply-quote payloads frequently include raw WeChat XML when the quoted
# message itself was media (image, voice, video, article, mini-program).
# Collapse that XML into a human tag so the LLM and the UI both see it as
# `↳ 回复 X: [图片]` instead of an unreadable blob.
_REPLY_XML = re.compile(r"(↳\s*回复\s*[^:]+:)\s*(<[a-zA-Z?].*)", re.DOTALL)
_XML_KIND_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"<img\b"), "[图片]"),
    (re.compile(r"<voicemsg\b|voiceformat\s*=", re.IGNORECASE), "[语音]"),
    (re.compile(r"<videomsg\b|<video\b", re.IGNORECASE), "[视频]"),
    (re.compile(r"<weappinfo\b|wxapp://", re.IGNORECASE), "[小程序]"),
    (re.compile(r"<emoji\b", re.IGNORECASE), "[表情]"),
]
_XML_TITLE = re.compile(r"<title>([^<]+)</title>")
_XML_URL = re.compile(r"<url>([^<]+)</url>")


def _xml_to_tag(payload: str) -> str:
    for pat, tag in _XML_KIND_PATTERNS:
        if pat.search(payload):
            return tag
    if "<title>" in payload and "<url>" in payload:
        t = _XML_TITLE.search(payload)
        u = _XML_URL.search(payload)
        if t and u:
            return f"[链接: {t.group(1).strip()} — {u.group(1).strip()}]"
    if "<title>" in payload:
        t = _XML_TITLE.search(payload)
        if t:
            return f"[卡片: {t.group(1).strip()}]"
    return "[附件]"


def _clean_reply_xml(text: str) -> str:
    """Convert `↳ 回复 X: <msg>...</msg>` payloads into short tags."""
    if "↳" not in text or "<" not in text:
        return text
    return _REPLY_XML.sub(lambda m: f"{m.group(1)} {_xml_to_tag(m.group(2))}", text)


class WechatCLIError(RuntimeError):
    pass


@dataclass(slots=True)
class Session:
    chat: str
    username: str
    is_group: bool
    unread: int
    last_message: str
    msg_type: str
    sender: str
    timestamp: int
    time: str
    # WeChat-side list-state flags (added by patched wechat-cli >= 0.2.4):
    #   is_hidden: the "不显示在聊天列表中" SessionTable toggle.
    #   status:    SessionTable bitmask. bit 1 (value 2) = 消息免打扰. Bit 2
    #              looked like fold initially but turned out to be unrelated —
    #              fold actually lives in contact.flag, not SessionTable.
    #   is_folded: derived from contact.flag bit 28 — WeChat's 折叠群聊.
    # Older wechat-cli versions don't emit these; they default to False/0 and
    # `minimized_in_wechat` safely returns False.
    is_hidden: bool = False
    status: int = 0
    is_folded: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Session":
        return cls(
            chat=d.get("chat", ""),
            username=d.get("username", ""),
            is_group=bool(d.get("is_group", False)),
            unread=int(d.get("unread", 0)),
            last_message=d.get("last_message", ""),
            msg_type=d.get("msg_type", ""),
            sender=d.get("sender", ""),
            timestamp=int(d.get("timestamp", 0)),
            time=d.get("time", ""),
            is_hidden=bool(d.get("is_hidden", False)),
            status=int(d.get("status", 0) or 0),
            is_folded=bool(d.get("is_folded", False)),
        )

    @property
    def minimized_in_wechat(self) -> bool:
        """True if the user has tucked this session out of WeChat's main chat
        list via any of WeChat's "minimize"-style toggles (折叠群聊 or
        不显示在聊天列表中). Mute alone (status bit 1) doesn't count — muted
        groups still appear in the main list."""
        return self.is_folded or self.is_hidden

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat": self.chat,
            "username": self.username,
            "is_group": self.is_group,
            "unread": self.unread,
            "last_message": self.last_message,
            "msg_type": self.msg_type,
            "sender": self.sender,
            "timestamp": self.timestamp,
            "time": self.time,
            "is_hidden": self.is_hidden,
            "status": self.status,
            "is_folded": self.is_folded,
        }


@dataclass(slots=True)
class Message:
    ts: str
    sender: str
    text: str
    image_paths: list[str]  # absolute paths to decrypted images, if any


def _bundled_site_dir() -> Path | None:
    """Return the directory holding the bundle's flattened third-party
    packages (wechat_cli, Crypto, zstandard, …) so we can put it on a
    subprocess PYTHONPATH. py2app's launcher does this via Resources/site.pyc
    when ChatLens itself boots, but a separately-spawned `Contents/MacOS/python`
    doesn't re-run that boot logic — so we have to set PYTHONPATH ourselves.
    """
    if not getattr(sys, "frozen", False):
        return None
    # `chatlens` sits in that same flat dir; its parent is what we need.
    import chatlens as _self
    return Path(_self.__file__).resolve().parent.parent


def _resolve_wechat_cli_cmd() -> list[str]:
    """How to invoke wechat-cli — bundled when frozen, PATH-based in source.

    Bundled: `Contents/MacOS/python -m wechat_cli`, with PYTHONPATH set so
    the bundled package is discoverable. Source: the PATH-resolved
    `wechat-cli` console script (or whatever WECHAT_CLI_PATH points to).
    """
    if getattr(sys, "frozen", False):
        bundled_python = Path(sys.executable).parent / "python"
        if bundled_python.exists():
            return [str(bundled_python), "-m", "wechat_cli"]
        log.warning(
            "frozen build but bundled python not found at %s — falling back to PATH",
            bundled_python,
        )
    return [settings.wechat_cli_path]


def _wechat_cli_env() -> dict[str, str]:
    env = dict(child_env())
    site = _bundled_site_dir()
    if site:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{site}:{existing}" if existing else str(site)
        # Point at Contents/Resources so the bundled python finds its stdlib
        # (python314.zip + lib-dynload) inside the .app instead of falling back
        # to the build machine's compile-time prefix — without this, the
        # subprocess silently loads stdlib from homebrew on the dev's laptop
        # and crashes with "No module named 'encodings'" on anyone else's.
        env["PYTHONHOME"] = str(site.parent.parent)
    return env


def wechat_cli_invocation() -> list[str]:
    """Public read-only view of the cmd prefix — useful for surfacing in the
    setup wizard so the user can copy-paste the right `sudo … init` line."""
    return _resolve_wechat_cli_cmd()


def wechat_cli_env() -> dict[str, str]:
    """Public view of the env we hand to wechat-cli subprocesses — the setup
    wizard's init command needs the same PYTHONPATH override or it'll fail
    with `ModuleNotFoundError: wechat_cli` after the user types sudo."""
    return _wechat_cli_env()


async def _run(*args: str) -> str:
    cmd = _resolve_wechat_cli_cmd()
    if settings.wechat_cli_config:
        cmd += ["--config", settings.wechat_cli_config]
    cmd += list(args)
    log.debug("wechat-cli %s", " ".join(args))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_wechat_cli_env(),
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise WechatCLIError(
            f"wechat-cli {' '.join(args)} exited {proc.returncode}: "
            f"{stderr.decode(errors='replace').strip()}"
        )
    return stdout.decode("utf-8", errors="replace")


async def sessions(limit: int = 50) -> list[Session]:
    out = await _run("sessions", "--limit", str(limit))
    data = json.loads(out)
    return [Session.from_dict(d) for d in data]


async def history(
    chat: str,
    limit: int = 200,
    offset: int = 0,
    start_time: str | None = None,
    end_time: str | None = None,
    with_media: bool = False,
) -> list[Message]:
    args = ["history", chat, "--limit", str(limit), "--offset", str(offset)]
    if start_time:
        args += ["--start-time", start_time]
    if end_time:
        args += ["--end-time", end_time]
    if with_media:
        args.append("--media")
    out = await _run(*args)
    data = json.loads(out)
    messages: list[Message] = []
    for line in data.get("messages") or []:
        m = HISTORY_LINE.match(line)
        ts = m["ts"] if m else ""
        sender = m["sender"].strip() if m else ""
        text = m["text"] if m else line
        text = _clean_reply_xml(text)
        image_paths: list[str] = []
        if with_media and "[图片]" in text:
            for mm in MEDIA_PATH.finditer(text):
                image_paths.append(mm["path"])
        messages.append(Message(ts=ts, sender=sender, text=text, image_paths=image_paths))
    return messages


async def is_available() -> tuple[bool, str]:
    try:
        out = await _run("--version")
        return True, out.strip()
    except (FileNotFoundError, WechatCLIError) as e:
        return False, str(e)


_WECHAT_PROCESS_NAMES = ("WeChat", "Weixin")

# Errors that signal the on-disk keys file is unusable — typically wechat-cli
# bubbling up an SQLCipher "file is not a database" / "could not decrypt"
# after it loaded `all_keys.json` and tried every key. Triggers the setup
# wizard to re-engage; see status().
_KEY_DECRYPT_FAIL = re.compile(
    r"(无法解密|解密失败|could not decrypt|decryption failed|"
    r"file is (?:encrypted|not a database)|无可用密钥|no usable key)",
    re.IGNORECASE,
)


async def is_app_running() -> bool:
    """True if the WeChat (or new Weixin) desktop client is currently running.

    pgrep -x matches the executable name exactly. We probe both because Tencent
    ships two macOS clients: the legacy `WeChat` bundle and the newer `Weixin`
    (微信 4.0+) client. Either being up is enough to talk to.
    """
    for name in _WECHAT_PROCESS_NAMES:
        try:
            proc = await asyncio.create_subprocess_exec(
                "pgrep", "-x", "-q", name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=child_env(),
            )
            if await proc.wait() == 0:
                return True
        except FileNotFoundError:
            # pgrep is part of base macOS; bail if it's somehow missing.
            return False
    return False


def _wechat_cli_initialized() -> bool:
    """True iff wechat-cli has been initialised AND `all_keys.json` actually
    contains at least one key.

    The bare existence check (the original implementation) lets a corrupt
    `init` slip through: when task_for_pid runs but extracts zero keys —
    e.g. WeChat is running but not yet logged in, or its ad-hoc signature
    was reverted by an auto-update — wechat-cli still writes a `[]` or `{}`
    placeholder file and exits 0. ChatLens then skipped the setup wizard and
    dumped the user onto an empty dashboard with an opaque "无法解密 session.db"
    error. Verify the keys file contains real content before declaring init
    complete; if it's a placeholder, behave as if init was never run."""
    state_dir = Path.home() / ".wechat-cli"
    if not (state_dir / "config.json").exists():
        return False
    keys = state_dir / "all_keys.json"
    if not keys.exists() or keys.stat().st_size < 10:
        return False
    try:
        data = json.loads(keys.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if isinstance(data, (dict, list)):
        return len(data) > 0
    return False


async def status() -> dict[str, Any]:
    """Richer health snapshot used by /api/health and the UI banner.

    In addition to the boolean flags below, returns a `setup_state` enum the
    frontend wizard uses to render state-specific guidance. Every non-`ready`
    state corresponds to a distinct corner case observed during onboarding:

      - `binary_unavailable`   - bundled python failed to load (rare since 0.1.9)
      - `needs_init`           - no key extraction ever succeeded
      - `keys_empty`           - init ran but produced 0 keys (WeChat wasn't
                                  fully logged in, or task_for_pid was blocked)
      - `keys_stale`           - keys existed but decrypt fails now (WeChat
                                  updated and rotated keys)
      - `needs_app`            - everything else is fine but WeChat isn't open
      - `db_locked`            - WeChat is busy writing; sessions() timed out
      - `sessions_failed`      - some other wechat-cli error
      - `ready`                - dashboard can render

    Flag fields preserved for backward compatibility:
      - available, app_running, connected, needs_init, version, error
    """
    ok, ver = await is_available()
    app_running = await is_app_running()
    state_dir = Path.home() / ".wechat-cli"
    has_config = (state_dir / "config.json").exists()
    has_keys_file = (state_dir / "all_keys.json").exists()
    initialized = _wechat_cli_initialized()

    def _make(setup_state: str, **kw: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "available": ok, "app_running": app_running, "connected": False,
            "needs_init": False, "version": ver if ok else "",
            "error": None, "last_message_at": None,
            "setup_state": setup_state,
        }
        base.update(kw)
        return base

    if not ok:
        return _make("binary_unavailable", error=ver)
    if not initialized:
        # Distinguish "never ran init" from "init ran but extracted 0 keys" —
        # the latter is the most common silent failure and deserves its own
        # explanation in the wizard.
        state = "keys_empty" if (has_config and has_keys_file) else "needs_init"
        return _make(state, needs_init=True,
                     error=("init 产生的 all_keys.json 为空 — 密钥提取没有真的成功"
                            if state == "keys_empty"
                            else "需要先运行一次 wechat-cli init 以提取微信本地数据库密钥"))
    if not app_running:
        return _make("needs_app", error="WeChat / Weixin 桌面应用未启动")
    try:
        recent = await asyncio.wait_for(sessions(limit=20), timeout=5.0)
        last_ts = max((s.timestamp for s in recent if s.timestamp), default=0) or None
        return _make("ready", connected=True, last_message_at=last_ts)
    except asyncio.TimeoutError:
        return _make("db_locked", error="wechat-cli sessions timed out — 数据库可能被锁")
    except (WechatCLIError, FileNotFoundError, json.JSONDecodeError) as e:
        err = str(e)
        # A "can't decrypt session.db" error means the on-disk keys file exists
        # but the keys inside don't match the current WeChat DB — typically
        # because WeChat updated and rotated keys, or the earlier init ran but
        # task_for_pid silently returned zero keys.
        if _KEY_DECRYPT_FAIL.search(err):
            return _make("keys_stale", needs_init=True, error=err[:400])
        return _make("sessions_failed", error=err[:400])
