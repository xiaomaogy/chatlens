"""Persisted app-wide preferences. Single-user, so a tiny JSON file under
data_dir is enough — avoids adding another SQLite table for one knob.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .config import settings

log = logging.getLogger(__name__)

_PREF_FILE = settings.data_dir / "preferences.json"

ALLOWED_MODELS = ("sonnet", "opus")
DEFAULT_MODEL = "sonnet"

_DEFAULTS: dict[str, Any] = {
    "claude_model": DEFAULT_MODEL,
}


def _load_raw() -> dict[str, Any]:
    if not _PREF_FILE.exists():
        return {}
    try:
        return json.loads(_PREF_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("preferences.json unreadable, falling back to defaults: %s", e)
        return {}


def all_prefs() -> dict[str, Any]:
    """Effective preferences = defaults overlaid with persisted overrides."""
    out = dict(_DEFAULTS)
    out.update(_load_raw())
    return out


def get_model() -> str:
    """The Claude model alias to pass via `claude -p --model …`. Falls back to
    DEFAULT_MODEL if the persisted value isn't a known alias — that protects
    against a stale preferences.json after we tighten the allow-list."""
    m = all_prefs().get("claude_model", DEFAULT_MODEL)
    return m if m in ALLOWED_MODELS else DEFAULT_MODEL


def set_model(model: str) -> str:
    if model not in ALLOWED_MODELS:
        raise ValueError(f"unsupported model: {model!r}; expected one of {ALLOWED_MODELS}")
    prefs = _load_raw()
    prefs["claude_model"] = model
    _PREF_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PREF_FILE.write_text(json.dumps(prefs, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("preferences: claude_model = %s", model)
    return model
