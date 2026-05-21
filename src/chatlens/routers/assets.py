"""Asset routes: list / view / delete, plus the summarize pipeline that
generates both a daily summary AND any topical-share writeups it detects."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from datetime import date as _date, datetime, time as _time, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlmodel import Session, select

from .. import llm, pdfexport, wechat
from ..config import settings
from ..db import Asset, Group, _now, get_session, isoformat_utc

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["assets"])

_MD_IMG = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"'】）)]+")
_LINK_TAG = re.compile(r"\[链接:\s*(?P<title>[^—\]]+)\s+—\s+(?P<url>https?://[^\s\]]+)\]")
_IMG_TAG = re.compile(r"\[图片\s+([^\]]+)\]")
_FILE_TAG = re.compile(r"\[文件[:\s]+([^\]\s]+)")

# Below this many (full-path) or new (incremental) messages we skip the LLM
# call entirely — a half-dozen messages produce a hollow summary and chew
# tokens. The full and incremental paths share the threshold so the user
# gets the same skip behaviour on first-run vs. re-click.
_MIN_MESSAGES_TO_SUMMARIZE = 10


def _is_day_closed(for_date: str, generated_at: datetime) -> bool:
    """True if `generated_at` is after `for_date`'s local-clock end. Used to
    short-circuit regeneration of past days: once the calendar day is over,
    no new WeChat messages can land on it, so an existing summary is
    permanently final and re-clicking 「生成精华」 should just no-op.

    `for_date` is the YYYY-MM-DD label wechat-cli/ChatLens uses — interpreted
    in the system's local timezone since wechat-cli emits local clock times.
    `generated_at` comes from SQLite and is naive (UTC-by-convention), so we
    attach UTC before comparing.
    """
    try:
        d = _date.fromisoformat(for_date)
    except ValueError:
        return False
    local_tz = datetime.now().astimezone().tzinfo
    next_midnight_local = datetime.combine(d + timedelta(days=1), _time.min, tzinfo=local_tz)
    gen = generated_at if generated_at.tzinfo else generated_at.replace(tzinfo=timezone.utc)
    return gen >= next_midnight_local


@router.get("/assets")
def list_assets(
    kind: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_session),
) -> list[dict]:
    # Sort by last-touched time so the dashboard's "最新生成" surfaces freshly
    # updated summaries too — not just brand-new ones. COALESCE picks
    # updated_at when set (incremental merges), else falls back to created_at.
    last_touched = func.coalesce(Asset.updated_at, Asset.created_at)
    q = select(Asset).order_by(last_touched.desc()).limit(limit)
    if kind:
        q = q.where(Asset.kind == kind)
    items = list(db.exec(q).all())
    groups = {g.id: g.name for g in db.exec(select(Group)).all()}
    return [
        {
            "id": a.id, "kind": a.kind, "title": a.title,
            "description": a.description, "for_date": a.for_date,
            "group_id": a.group_id, "group_name": groups.get(a.group_id, ""),
            "created_at": isoformat_utc(a.created_at),
            "updated_at": isoformat_utc(a.updated_at) if a.updated_at else None,
        } for a in items
    ]


@router.get("/assets/{asset_id}")
def get_asset(asset_id: str, db: Session = Depends(get_session)) -> dict:
    a = db.get(Asset, asset_id)
    if not a:
        raise HTTPException(404, "asset not found")
    g = db.get(Group, a.group_id)
    meta: dict = {}
    if a.meta_json:
        try:
            meta = json.loads(a.meta_json)
        except json.JSONDecodeError:
            meta = {}
    # Resolve topic lines into their actual message objects for the frontend.
    messages = meta.get("messages") or {}
    topics_out = []
    for t in meta.get("topics") or []:
        topic_msgs = [messages.get(str(ln)) for ln in (t.get("lines") or [])]
        topic_msgs = [m for m in topic_msgs if m]
        topics_out.append({"id": t.get("id"), "title": t.get("title"), "messages": topic_msgs})
    return {
        "id": a.id, "kind": a.kind, "title": a.title,
        "description": a.description, "body_md": a.body_md,
        "for_date": a.for_date, "group_id": a.group_id,
        "group_name": g.name if g else "",
        "speaker": meta.get("speaker"),
        "created_at": isoformat_utc(a.created_at),
        "updated_at": isoformat_utc(a.updated_at) if a.updated_at else None,
        "topics": topics_out,
    }


@router.post("/assets/{asset_id}/export")
def export_asset_pdf(asset_id: str, db: Session = Depends(get_session)) -> dict:
    """Render an asset (精华 / 每日精华 / 嘉宾分享) to a PDF written straight
    into ~/Downloads — note text plus its inline images, no dialog."""
    a = db.get(Asset, asset_id)
    if not a:
        raise HTTPException(404, "asset not found")
    g = db.get(Group, a.group_id)
    try:
        path = pdfexport.export_asset_to_downloads(a, g.name if g else "")
    except pdfexport.PDFExportUnavailable as e:
        raise HTTPException(503, str(e))
    except Exception as e:  # noqa: BLE001 — surface render failures, don't 500 blind
        log.exception("PDF export failed for asset %s", asset_id)
        raise HTTPException(500, f"PDF 生成失败：{e}")
    return {"ok": True, "filename": path.name, "path": str(path)}


@router.delete("/assets/{asset_id}")
def delete_asset(asset_id: str, db: Session = Depends(get_session)) -> dict:
    a = db.get(Asset, asset_id)
    if not a:
        raise HTTPException(404, "asset not found")
    media_dir = settings.data_dir / "media" / a.id
    if media_dir.exists():
        shutil.rmtree(media_dir, ignore_errors=True)
    db.delete(a)
    db.commit()
    return {"ok": True}


def _copy_share_media(asset_id: str, body_md: str, image_paths: dict[str, str]) -> str:
    """Rewrite `![](name.jpg)` refs to `/api/media/<asset_id>/<name>` and copy
    the actual files into ./data/media/<asset_id>/."""
    target_dir = settings.data_dir / "media" / asset_id

    def _replace(m: re.Match) -> str:
        alt, name = m.group(1), m.group(2)
        if "/" in name or name.startswith(("http://", "https://", "data:")):
            return m.group(0)
        src = image_paths.get(name)
        if not src or not Path(src).exists():
            return m.group(0)
        target_dir.mkdir(parents=True, exist_ok=True)
        dst = target_dir / name
        if not dst.exists():
            try:
                shutil.copy2(src, dst)
            except OSError as e:
                log.warning("failed to copy media %s: %s", src, e)
                return m.group(0)
        return f"![{alt}](/api/media/{asset_id}/{name})"

    return _MD_IMG.sub(_replace, body_md)


def _collect_share_resources(share, messages_index: dict) -> tuple[dict[str, str], list[str], list[str]]:
    """Walk the messages a share covers and pull out every URL / image / file
    so we can append a guaranteed 「📎 原始资源」 footer.

    Returns (urls_by_label, image_filenames, file_names).
    `urls_by_label` maps URL → human-friendly label (article title, sender, etc.).
    """
    urls: dict[str, str] = {}
    images: list[str] = []
    files: list[str] = []
    seen_imgs: set[str] = set()
    seen_files: set[str] = set()
    for ln in share.lines:
        msg = messages_index.get(str(ln)) or {}
        text = msg.get("text", "")
        sender = msg.get("sender", "")
        # [链接: title — url] format produced by wechat condensation
        for m in _LINK_TAG.finditer(text):
            u = m["url"].strip()
            title = m["title"].strip()
            if u not in urls:
                urls[u] = title or sender or u
        # Bare URLs anywhere in the text
        for u in _URL_IN_TEXT.findall(text):
            u = u.rstrip(".,;:")
            if u not in urls:
                urls[u] = sender or "群友分享"
        # Image filenames (always listed by build_transcript with [图片 xxx.jpg])
        for fname in _IMG_TAG.findall(text):
            if fname not in seen_imgs:
                images.append(fname); seen_imgs.add(fname)
        # File names mentioned in [文件 xxx.pdf] etc
        for fname in _FILE_TAG.findall(text):
            if fname not in seen_files:
                files.append(fname); seen_files.add(fname)
    return urls, images, files


def _append_resource_footer(body_md: str, urls: dict[str, str], images: list[str], files: list[str]) -> str:
    if not urls and not images and not files:
        return body_md
    parts = ["", "", "## 📎 原始资源", ""]
    if urls:
        parts.append("**链接：**")
        for url, label in urls.items():
            # If the body already contains this URL as a Markdown link, skip it.
            if f"]({url})" in body_md:
                continue
            parts.append(f"- [{label}]({url})")
        parts.append("")
    if images:
        # Only list images that aren't already embedded in the body.
        missing = [name for name in images if f"]({name})" not in body_md and f"/{name})" not in body_md]
        if missing:
            parts.append("**图片：**")
            for name in missing:
                parts.append(f"- ![]({name})")
            parts.append("")
    if files:
        parts.append("**文件：**")
        for f in files:
            parts.append(f"- `{f}`")
        parts.append("")
    return body_md.rstrip() + "\n" + "\n".join(parts).rstrip() + "\n"


def _persist_all_images(asset_id: str, image_paths: dict[str, str]) -> None:
    if not image_paths:
        return
    target = settings.data_dir / "media" / asset_id
    target.mkdir(parents=True, exist_ok=True)
    for name, src in image_paths.items():
        if "/" in name or name.startswith("."):
            continue
        if not Path(src).exists():
            continue
        dst = target / name
        if not dst.exists():
            try:
                shutil.copy2(src, dst)
            except OSError as e:
                log.warning("failed to copy media %s: %s", src, e)


def _first_heading(body_md: str) -> str:
    for line in body_md.splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("# ").strip()
        if s:
            return s[:80]
    return ""


def _load_increment_cursor(asset: Asset) -> tuple[dict, int, str] | None:
    """If `asset` is a reusable prior daily_summary, return (old_meta, max_line,
    last_ts) so the caller can fetch only messages newer than `last_ts`. Returns
    None if the meta is missing/unparseable or has no line tracking — those
    cases fall back to a full regeneration.
    """
    if not asset.meta_json:
        return None
    try:
        old_meta = json.loads(asset.meta_json)
    except json.JSONDecodeError:
        return None
    messages = old_meta.get("messages") or {}
    if not messages:
        return None
    try:
        max_line = max(int(k) for k in messages.keys())
    except (ValueError, TypeError):
        return None
    last_ts = max((v.get("ts") for v in messages.values() if v.get("ts")), default="")
    if not last_ts:
        return None
    return old_meta, max_line, last_ts


def _skip_boundary(messages: list, boundary_ts: str, already_covered: int) -> list:
    """Drop the first `already_covered` messages whose ts equals boundary_ts.

    `wechat-cli --start-time` is inclusive at minute resolution, so a fresh
    fetch starting at the prior run's last covered ts re-emits everything from
    that minute. Counting how many of that minute's rows we already had — and
    skipping that many from the new fetch's head — exactly removes the overlap
    without depending on text-content comparison (which would break for
    image/voice/article messages whose persisted text differs from the live
    wechat-cli output).
    """
    if already_covered <= 0:
        return messages
    skipped = 0
    out = []
    for m in messages:
        if skipped < already_covered and m.ts == boundary_ts:
            skipped += 1
            continue
        out.append(m)
    return out


async def _fetch_day(
    username: str,
    start_time: str,
    end_time: str,
    _phase,
    boundary: tuple[str, int] | None = None,
):
    """Fetch a date range from wechat-cli. First pass is text-only (fast);
    a second pass with `--media` runs only if any image messages are present.
    `boundary=(ts, n)` skips the first `n` messages at `ts` to remove overlap
    with a prior run.
    """
    msgs = await wechat.history(
        username, limit=800, start_time=start_time, end_time=end_time, with_media=False
    )
    if boundary:
        msgs = _skip_boundary(msgs, *boundary)
    if not msgs:
        return msgs
    if any("[图片]" in m.text for m in msgs):
        _phase("加载图片…")
        msgs = await wechat.history(
            username, limit=800, start_time=start_time, end_time=end_time, with_media=True
        )
        if boundary:
            msgs = _skip_boundary(msgs, *boundary)
    return msgs


def _persist_share(
    db: Session,
    group_id: str,
    date_str: str,
    share,
    messages_index: dict,
    image_paths: dict[str, str],
) -> str:
    """Build + commit one guest_share asset, including resource footer and
    media-path rewrite. Returns the new asset id. Shared by full and
    incremental paths.
    """
    urls, image_names, file_names = _collect_share_resources(share, messages_index)
    body_with_footer = _append_resource_footer(share.body_md, urls, image_names, file_names)
    meta = {
        "topics": [{"id": "S", "title": "原文相关消息", "lines": share.lines}],
        "messages": {
            str(ln): messages_index[str(ln)]
            for ln in share.lines if str(ln) in messages_index
        },
        "speaker": share.speaker,
    }
    asset = Asset(
        group_id=group_id,
        kind="guest_share",
        title=share.title,
        description=" ".join(body_with_footer.split())[:240],
        body_md=body_with_footer,
        meta_json=json.dumps(meta, ensure_ascii=False),
        for_date=date_str,
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    rewritten = _copy_share_media(asset.id, body_with_footer, image_paths)
    if rewritten != asset.body_md:
        asset.body_md = rewritten
        db.add(asset)
        db.commit()
    return asset.id


def _merge_topic_metas(old_topics: list[dict], new_topics: list) -> list[dict]:
    """Merge the topic list emitted by the incremental LLM call with the
    persisted old topics. For each new topic that matches an old ID, append
    its lines to the old topic's lines; new IDs come through as-is; old IDs
    the LLM omitted are preserved (so drill-down for the old portion keeps
    working even if the model dropped a topic from its output).
    """
    by_id = {str(t.get("id", "")): t for t in old_topics if t.get("id")}
    out: list[dict] = []
    seen: set[str] = set()
    for t in new_topics:
        seen.add(t.id)
        old = by_id.get(t.id)
        if old is None:
            out.append({"id": t.id, "title": t.title, "lines": list(t.lines)})
            continue
        out.append({
            "id": t.id,
            "title": t.title or old.get("title", ""),
            "lines": list(old.get("lines") or []) + list(t.lines),
        })
    for old in old_topics:
        tid = str(old.get("id", ""))
        if tid and tid not in seen:
            out.append(dict(old))
    return out


def _persist_full_summary(
    db: Session, g: Group, date_str: str, result, msg_count: int, _phase,
) -> dict:
    """Synchronous tail of the full summarize path — runs via asyncio.to_thread
    so its SQLite writes and media-file copies don't freeze the event loop (and
    with it every page request) while a job is in flight.

    Any prior daily_summary + guest_share rows for this (group, date) are stale
    once the LLM call succeeds; delete + insert in one session so the commit is
    atomic — a failed Claude call earlier leaves the old summary intact.
    """
    prev = list(db.exec(
        select(Asset).where(
            Asset.group_id == g.id,
            Asset.for_date == date_str,
            Asset.kind.in_(("daily_summary", "guest_share")),
        )
    ).all())
    for old in prev:
        media_dir = settings.data_dir / "media" / old.id
        if media_dir.exists():
            shutil.rmtree(media_dir, ignore_errors=True)
        db.delete(old)

    messages_index = {str(k): v for k, v in result.line_to_message.items()}
    summary_meta = {
        "topics": [{"id": t.id, "title": t.title, "lines": t.lines} for t in result.topics],
        "messages": messages_index,
    }
    summary_asset = Asset(
        group_id=g.id,
        kind="daily_summary",
        title=_first_heading(result.summary_md) or f"{g.name} · {date_str}",
        description=" ".join(result.summary_md.split())[:240],
        body_md=result.summary_md,
        meta_json=json.dumps(summary_meta, ensure_ascii=False),
        for_date=date_str,
    )
    _phase("保存每日精华…")
    db.add(summary_asset)
    db.commit()
    db.refresh(summary_asset)
    _persist_all_images(summary_asset.id, result.image_paths_by_name)

    share_ids: list[str] = []
    for i, share in enumerate(result.shares, start=1):
        _phase(f"保存嘉宾分享 {i}/{len(result.shares)}…")
        share_ids.append(_persist_share(
            db, g.id, date_str, share, messages_index, result.image_paths_by_name,
        ))

    return {
        "summary_id": summary_asset.id,
        "title": summary_asset.title,
        "for_date": date_str,
        "message_count": msg_count,
        "share_ids": share_ids,
        "share_count": len(share_ids),
    }


def _persist_incremental_summary(
    db: Session, g: Group, date_str: str, existing_summary: Asset, result,
    old_topics: list[dict], old_messages: dict, new_msg_count: int, _phase,
) -> dict:
    """Synchronous tail of the incremental summarize path — runs via
    asyncio.to_thread for the same event-loop reason as _persist_full_summary.
    """
    new_messages_index = {str(k): v for k, v in result.line_to_message.items()}
    merged_meta = {
        "topics": _merge_topic_metas(old_topics, result.topics),
        "messages": {**old_messages, **new_messages_index},
    }
    existing_summary.title = _first_heading(result.summary_md) or existing_summary.title
    existing_summary.description = " ".join(result.summary_md.split())[:240]
    existing_summary.body_md = result.summary_md
    existing_summary.meta_json = json.dumps(merged_meta, ensure_ascii=False)
    # Stamp so the UI can switch from "生成于" to "更新于" for in-place updates.
    # created_at stays at the original first-generation time.
    existing_summary.updated_at = _now()
    _phase("保存每日精华…")
    db.add(existing_summary)
    db.commit()
    db.refresh(existing_summary)
    _persist_all_images(existing_summary.id, result.image_paths_by_name)

    share_ids: list[str] = []
    for i, share in enumerate(result.shares, start=1):
        _phase(f"保存嘉宾分享 {i}/{len(result.shares)}…")
        share_ids.append(_persist_share(
            db, g.id, date_str, share, new_messages_index, result.image_paths_by_name,
        ))

    return {
        "summary_id": existing_summary.id,
        "title": existing_summary.title,
        "for_date": date_str,
        "message_count": new_msg_count,
        "share_ids": share_ids,
        "share_count": len(share_ids),
        "incremental": True,
    }


async def summarize_group(
    group_id: str,
    db: Session,
    target_date: str | None = None,
    job=None,
) -> dict:
    """Core summarize pipeline, callable both from the HTTP route and from
    the bulk-summarize background task. Generates one daily_summary (and any
    guest_shares) for the given date — defaults to today.

    `job` (optional) is a chatlens.jobs.Job; if provided we update its
    current_phase as the pipeline advances so the UI can show real progress
    instead of just an elapsed-time counter.

    Two paths:

    - **Incremental** (taken when a prior daily_summary exists for this
      (group, date) and its meta_json carries usable line tracking): fetch only
      messages newer than the prior run's last covered timestamp, ask Claude to
      merge them into the existing summary, then update the existing asset in
      place. Existing guest_shares are preserved as-is; any newly detected
      shares in the new portion are appended.
    - **Full** (otherwise — first run for this date, or old meta unreadable):
      regenerate from scratch and atomically replace any prior rows.
    """
    def _phase(text: str) -> None:
        if job is not None:
            job.current_phase = text

    if not llm.is_configured():
        raise HTTPException(400, "claude CLI not found on PATH")
    g = db.get(Group, group_id)
    if not g:
        raise HTTPException(404, "group not found")

    date_str = target_date or _date.today().isoformat()

    # Incremental path detection: most-recent reusable daily_summary for this
    # (group, date). If found, we'll skip re-reading the morning's messages.
    existing_summary = db.exec(
        select(Asset)
        .where(
            Asset.group_id == g.id,
            Asset.for_date == date_str,
            Asset.kind == "daily_summary",
        )
        .order_by(Asset.created_at.desc())
    ).first()

    # Fast-path skip: if a summary already exists AND it was generated after
    # the for_date's local-clock end, the day is permanently closed — no new
    # messages can arrive — so a re-click is pure waste. Most useful inside
    # multi-day backfills where past-day re-runs would otherwise replay the
    # full pipeline.
    if existing_summary and _is_day_closed(date_str, existing_summary.created_at):
        return {
            "summary_id": existing_summary.id,
            "title": existing_summary.title,
            "for_date": date_str,
            "message_count": 0,
            "share_ids": [],
            "share_count": 0,
            "skipped": True,
            "reason": "day_closed",
        }

    cursor = _load_increment_cursor(existing_summary) if existing_summary else None

    if cursor and existing_summary:
        return await _summarize_group_incremental(
            db, g, date_str, existing_summary, cursor, _phase
        )

    _phase("拉取聊天记录…")
    msgs = await _fetch_day(
        g.wechat_username, f"{date_str} 00:00", f"{date_str} 23:59:59", _phase,
    )
    if not msgs:
        # An empty day isn't a failure — the group just didn't talk that
        # date. Return as a skip result so bulk jobs don't surface it as a
        # red 「失败」.
        return {
            "summary_id": None,
            "title": f"{g.name} · {date_str}",
            "for_date": date_str,
            "message_count": 0,
            "share_ids": [],
            "share_count": 0,
            "skipped": True,
            "reason": "no_messages",
        }
    # Activity gate: below this many messages there's no signal to summarize,
    # so skip the LLM call entirely instead of burning tokens on "今日水群无
    # 实质内容". The threshold matches the incremental path's gate so behaviour
    # is consistent between first-run and re-runs.
    if len(msgs) < _MIN_MESSAGES_TO_SUMMARIZE:
        return {
            "summary_id": None,
            "title": f"{g.name} · {date_str}",
            "for_date": date_str,
            "message_count": len(msgs),
            "share_ids": [],
            "share_count": 0,
            "skipped": True,
            "reason": "low_activity",
        }
    _phase(f"Claude 正在生成精华({len(msgs)} 条消息)…")
    result = await llm.summarize_chat(g.name, date_str, msgs)

    # The persist step is synchronous (SQLite writes + media-file copies). Run
    # it on a worker thread so it doesn't freeze the server's event loop — and
    # with it every page request — for the duration of the writes.
    return await asyncio.to_thread(
        _persist_full_summary, db, g, date_str, result, len(msgs), _phase,
    )


async def _summarize_group_incremental(
    db: Session,
    g: Group,
    date_str: str,
    existing_summary: Asset,
    cursor: tuple[dict, int, str],
    _phase,
) -> dict:
    """Incremental sibling of summarize_group. Fetches only messages newer
    than the prior run's last covered ts, asks Claude to merge them, and
    updates `existing_summary` in place. Old guest_share assets are kept
    untouched; new shares Claude detects in the delta are appended.
    """
    old_meta, max_line, last_ts = cursor
    old_topics = old_meta.get("topics") or []
    old_messages = old_meta.get("messages") or {}
    boundary_count = sum(1 for v in old_messages.values() if v.get("ts") == last_ts)

    _phase("拉取新增聊天记录…")
    new_msgs = await _fetch_day(
        g.wechat_username, last_ts, f"{date_str} 23:59:59", _phase,
        boundary=(last_ts, boundary_count),
    )
    if not new_msgs:
        # No new messages since the prior summary — asset is unchanged. Mark
        # as a skip so it's bucketed as 跳过 in the bulk bar, not counted as
        # a fresh success. (`no_new_messages` kept for backward-compat with
        # any caller that checked it.)
        return {
            "summary_id": existing_summary.id,
            "title": existing_summary.title,
            "for_date": date_str,
            "message_count": 0,
            "share_ids": [],
            "share_count": 0,
            "incremental": True,
            "skipped": True,
            "reason": "no_new_messages",
            "no_new_messages": True,
        }
    # Tiny delta — fewer than the activity gate's worth of new messages — is
    # unlikely to materially change the summary, so leave the existing asset
    # alone instead of burning a Claude call to fold in one or two lines.
    if len(new_msgs) < _MIN_MESSAGES_TO_SUMMARIZE:
        return {
            "summary_id": existing_summary.id,
            "title": existing_summary.title,
            "for_date": date_str,
            "message_count": len(new_msgs),
            "share_ids": [],
            "share_count": 0,
            "incremental": True,
            "skipped": True,
            "reason": "delta_too_small",
        }

    _phase(f"Claude 正在合并新精华({len(new_msgs)} 条新消息)…")
    result = await llm.summarize_chat_incremental(
        g.name, date_str, new_msgs,
        old_summary_md=existing_summary.body_md,
        old_topics=old_topics,
        next_line=max_line + 1,
    )

    # Persist on a worker thread — see _persist_full_summary for the why.
    return await asyncio.to_thread(
        _persist_incremental_summary,
        db, g, date_str, existing_summary, result, old_topics, old_messages,
        len(new_msgs), _phase,
    )


# The POST /groups/{id}/summarize endpoint lives in routers/jobs.py — it
# kicks off a background job so closing the browser tab doesn't lose work.
# summarize_group() above stays here as the shared worker function.
