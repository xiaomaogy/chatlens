"""Persisted user preferences (currently just the Claude model selector).

Tiny on purpose — one JSON file under data_dir backs it. If the preference
surface grows past 2-3 fields, promote to a SQLite table.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import preferences

router = APIRouter(prefix="/api/preferences", tags=["preferences"])


class ModelReq(BaseModel):
    model: str


@router.get("")
def get_preferences() -> dict:
    return {
        "claude_model": preferences.get_model(),
        "allowed_models": list(preferences.ALLOWED_MODELS),
    }


@router.put("/model")
def set_model(req: ModelReq) -> dict:
    try:
        chosen = preferences.set_model(req.model)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"claude_model": chosen}
