from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from .. import wechat
from ..db import Asset, Group, get_session, isoformat_utc

router = APIRouter(prefix="/api/groups", tags=["groups"])


_PALETTES = [
    "from-indigo-500 to-violet-500",
    "from-sky-500 to-cyan-500",
    "from-emerald-500 to-teal-500",
    "from-fuchsia-500 to-pink-500",
    "from-amber-500 to-orange-500",
    "from-rose-500 to-red-500",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("")
async def list_groups(
    pinned_only: bool = False,
    include_hidden: bool = False,
    db: Session = Depends(get_session),
) -> list[dict]:
    q = select(Group).where(Group.monitored == True)  # noqa: E712
    if pinned_only:
        q = q.where(Group.pinned == True)  # noqa: E712
    if not include_hidden:
        # Hide if either layer says hidden — WeChat-fold mirror OR user override.
        q = q.where(Group.hidden == False).where(Group.user_hidden == False)  # noqa: E712
    groups = list(db.exec(q).all())
    try:
        sessions = await wechat.sessions(limit=300)
    except wechat.WechatCLIError:
        sessions = []
    by_username = {s.username: s for s in sessions}
    asset_counts: dict[str, int] = {}
    for g in groups:
        asset_counts[g.id] = len(list(db.exec(select(Asset).where(Asset.group_id == g.id)).all()))
    out = []
    for g in groups:
        s = by_username.get(g.wechat_username)
        out.append({
            "id": g.id,
            "name": g.name,
            "wechat_username": g.wechat_username,
            "pinned": bool(g.pinned),
            # Effective hidden = either layer is set. Surfaced as a single
            # `hidden` flag so the frontend doesn't have to know about the
            # two-layer model.
            "hidden": bool(g.hidden or g.user_hidden),
            "unread": s.unread if s else 0,
            "last_message_time": s.time if s else "",
            "last_sender": s.sender if s else "",
            "last_message": s.last_message if s else "",
            "accent_color": g.accent_color,
            "asset_count": asset_counts.get(g.id, 0),
        })
    # Hidden groups sink to the bottom; then pinned first; then most-recent activity.
    out.sort(key=lambda r: (
        r["hidden"],
        not r["pinned"],
        -(int(r["last_message_time"].replace("-","").replace(":","").replace(" ","") or 0) if r["last_message_time"] else 0),
    ))
    return out


@router.post("/sync")
async def sync_groups(db: Session = Depends(get_session)) -> dict:
    sessions = [s for s in await wechat.sessions(limit=300) if s.is_group]
    created = 0
    auto_hidden = 0
    for i, s in enumerate(sessions):
        # WeChat is the source of truth for "minimized" state — every sync
        # overwrites Group.hidden to match. If the user un-folds a group in
        # WeChat it surfaces here; if they fold a new one it sinks. The eye
        # icon in the dashboard is a manual override that survives only until
        # the next sync, so the permanent way to hide a group is to fold it
        # in WeChat itself.
        wechat_hidden = s.minimized_in_wechat
        if wechat_hidden:
            auto_hidden += 1
        existing = db.exec(select(Group).where(Group.wechat_username == s.username)).first()
        if existing:
            existing.name = s.chat or existing.name
            existing.last_synced_at = _now()
            existing.hidden = wechat_hidden
            db.add(existing)
            continue
        db.add(Group(
            wechat_username=s.username,
            name=s.chat,
            accent_color=_PALETTES[i % len(_PALETTES)],
            last_synced_at=_now(),
            hidden=wechat_hidden,
        ))
        created += 1
    db.commit()
    return {
        "created": created,
        "total_groups_seen": len(sessions),
        "auto_hidden": auto_hidden,
    }


class PinReq(BaseModel):
    pinned: bool


@router.post("/{group_id}/pin")
def set_pin(group_id: str, req: PinReq, db: Session = Depends(get_session)) -> dict:
    g = db.get(Group, group_id)
    if not g:
        raise HTTPException(404, "group not found")
    g.pinned = req.pinned
    db.add(g)
    db.commit()
    return {"id": g.id, "pinned": g.pinned}


class HideReq(BaseModel):
    hidden: bool


@router.post("/{group_id}/hide")
def set_hidden(group_id: str, req: HideReq, db: Session = Depends(get_session)) -> dict:
    g = db.get(Group, group_id)
    if not g:
        raise HTTPException(404, "group not found")
    # Manual override layer — sticky across syncs. Sync only touches
    # `g.hidden` (the WeChat mirror), so user_hidden lets the operator tuck
    # away a group that isn't folded on the WeChat side.
    g.user_hidden = req.hidden
    db.add(g)
    db.commit()
    return {"id": g.id, "hidden": bool(g.hidden or g.user_hidden)}


@router.get("/{group_id}")
def get_group(group_id: str, db: Session = Depends(get_session)) -> dict:
    g = db.get(Group, group_id)
    if not g:
        raise HTTPException(404, "group not found")
    assets = list(db.exec(
        select(Asset).where(Asset.group_id == g.id).order_by(Asset.created_at.desc()).limit(200)
    ).all())
    return {
        "id": g.id,
        "name": g.name,
        "wechat_username": g.wechat_username,
        "pinned": bool(g.pinned),
        "accent_color": g.accent_color,
        "last_synced_at": isoformat_utc(g.last_synced_at) if g.last_synced_at else None,
        "assets": [
            {
                "id": a.id,
                "kind": a.kind,
                "title": a.title,
                "description": a.description,
                "for_date": a.for_date,
                "created_at": isoformat_utc(a.created_at),
                "updated_at": isoformat_utc(a.updated_at) if a.updated_at else None,
            } for a in assets
        ],
    }
