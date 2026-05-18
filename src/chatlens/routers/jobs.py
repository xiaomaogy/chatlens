"""Background-job endpoints. Currently powers bulk-summarize."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import date as _date, timedelta as _td

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from .. import jobs, llm
from ..db import Asset, Group, engine, get_session
from .assets import summarize_group

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["jobs"])


async def _run_with_cancel(coro, job: "jobs.Job", poll_interval: float = 0.5):
    """Run `coro` while watching `job.cancel_requested`. If the flag flips
    while the coroutine is in flight (e.g. mid-Claude call), cancel the
    underlying task — which kills the subprocess via _run_claude's handler —
    and re-raise asyncio.CancelledError. The caller treats that as "user
    cancelled mid-iteration".
    """
    task = asyncio.create_task(coro)
    try:
        while not task.done():
            if job.cancel_requested:
                task.cancel()
                break
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=poll_interval)
            except asyncio.TimeoutError:
                continue
        return await task
    except asyncio.CancelledError:
        raise


# Backoff schedule for Claude rate-limit retries: 1, 2, 5, 10, 20 min, then
# plateau at 20 min. Picked so that:
#   - the first wait is short enough that quick five-hour-limit blips
#     resolve within a single attempt
#   - later waits don't burn battery polling every minute
#   - it never gives up, since the user said they want auto-resume; only
#     job cancellation breaks the loop.
_RATE_LIMIT_BACKOFF_S = [60, 120, 300, 600, 1200, 1200]


async def _cancellable_sleep(seconds: int, job: "jobs.Job") -> None:
    """Sleep `seconds`, but check job.cancel_requested every ~2 seconds so a
    cancel during retry-wait propagates promptly instead of hanging until the
    full backoff elapses."""
    end = time.monotonic() + seconds
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        if job.cancel_requested:
            raise asyncio.CancelledError()
        await asyncio.sleep(min(2.0, remaining))


async def _run_with_rate_limit_retry(coro_factory, job: "jobs.Job"):
    """Call `coro_factory()` and run the result with cancellation support;
    if it raises ClaudeRateLimitError, wait per the backoff schedule and
    retry. Loops indefinitely on rate-limit until cancellation or success —
    the operator can cancel the job to stop. Non-rate-limit exceptions bubble
    up unchanged.

    `coro_factory` is a zero-arg callable that builds a fresh coroutine each
    invocation (coroutines can only be awaited once, so we can't pre-build).
    """
    attempt = 0
    while True:
        try:
            return await _run_with_cancel(coro_factory(), job)
        except asyncio.CancelledError:
            raise
        except llm.ClaudeRateLimitError as e:
            delay = _RATE_LIMIT_BACKOFF_S[min(attempt, len(_RATE_LIMIT_BACKOFF_S) - 1)]
            attempt += 1
            mins = delay // 60
            job.current_phase = f"Claude 限流中，{mins} 分钟后重试 (第 {attempt} 次)"
            log.warning(
                "rate-limit on job %s; sleeping %ds (attempt %d): %s",
                job.id, delay, attempt, str(e)[:200],
            )
            await _cancellable_sleep(delay, job)


class BulkSummarizeReq(BaseModel):
    group_ids: list[str]
    # Two ways to specify the date range:
    #   `dates` — explicit list of YYYY-MM-DD strings. Takes precedence when
    #             present; used by the "1 天 (昨天)" option where the date
    #             window isn't a today-anchored count.
    #   `days`  — today-anchored count. days=3 means today + yesterday +
    #             day-before, processed today-first. Clamped 1-30 server-side.
    days: int = 1
    dates: list[str] | None = None


@router.post("/groups/{group_id}/summarize")
async def start_single_summarize(
    group_id: str,
    days: int = 1,
    db: Session = Depends(get_session),
) -> dict:
    g = db.get(Group, group_id)
    if not g:
        raise HTTPException(404, "group not found")
    days = max(1, min(days, 30))  # safety clamp; backfill more than 30 days at a time is silly
    job = jobs.create("single_summarize", total=days)
    job.current_id = group_id
    job.current_name = g.name
    job.current_started_at = time.time()
    asyncio.create_task(_run_single_summarize(job.id, group_id, g.name, days))
    return {"job_id": job.id, "days": days}


async def _run_single_summarize(job_id: str, group_id: str, name: str, days: int) -> None:
    job = jobs.get(job_id)
    if not job:
        return

    # Backfill ordering: process today first, then walk backwards. Most users
    # want today's summary fastest, and per-day failures shouldn't block the
    # rest of the range — record and keep going.
    today = _date.today()
    dates = [(today - _td(days=i)).isoformat() for i in range(days)]
    log.info("single-summarize job %s starting (%s / %s, days=%d, dates=%s)",
             job_id, group_id, name, days, dates)

    for date_str in dates:
        if job.cancel_requested:
            job.status = "cancelled"
            break
        job.current_id = group_id
        job.current_name = f"{name} · {date_str}" if days > 1 else name
        job.current_started_at = time.time()
        try:
            # Each retry attempt needs a fresh db session and a fresh
            # summarize_group coroutine. The factory rebuilds both.
            def _attempt(d=date_str):
                async def _go():
                    with Session(engine) as db:
                        return await summarize_group(group_id, db, target_date=d, job=job)
                return _go()
            result = await _run_with_rate_limit_retry(_attempt, job)
            job.results.append({"group_id": group_id, **result})
        except asyncio.CancelledError:
            log.info("single job %s cancelled mid-day %s", job_id, date_str)
            job.status = "cancelled"
            break  # don't bump job.done — this iteration didn't complete
        except Exception as e:
            log.exception("single job %s failed for %s", job_id, date_str)
            job.errors.append({
                "group_id": group_id, "name": name,
                "for_date": date_str, "msg": str(e),
            })
        job.done += 1

    if job.status == "running":
        job.status = "done"
    job.finished_at = time.time()
    log.info("single-summarize job %s finished: %d/%d ok",
             job_id, job.done - len(job.errors), job.total)
    jobs.save_to_disk(job)


@router.post("/groups/bulk-summarize")
async def start_bulk_summarize(req: BulkSummarizeReq, db: Session = Depends(get_session)) -> dict:
    ids = list(dict.fromkeys(req.group_ids))  # dedupe, preserve order
    if not ids:
        raise HTTPException(400, "no group_ids provided")
    if req.dates:
        # Explicit dates beat the days-count. Dedupe while preserving the
        # caller's order so results land in the order they asked for.
        dates = list(dict.fromkeys(req.dates))
        if not dates:
            raise HTTPException(400, "dates list cannot be empty")
    else:
        days = max(1, min(req.days, 30))  # safety clamp
        today = _date.today()
        dates = [(today - _td(days=i)).isoformat() for i in range(days)]
    # Resolve names up-front so the job has them even after the request session closes.
    rows = db.exec(select(Group).where(Group.id.in_(ids))).all()
    names = {g.id: g.name for g in rows}
    job = jobs.create("bulk_summarize", total=len(ids) * len(dates))
    asyncio.create_task(_run_bulk_summarize(job.id, ids, names, dates))
    return {"job_id": job.id, "dates": dates}


async def _run_bulk_summarize(
    job_id: str, group_ids: list[str], names: dict[str, str], dates: list[str],
) -> None:
    job = jobs.get(job_id)
    if not job:
        return
    # The caller decides the date order — today-anchored counts come in
    # today-first so the digest step at the end has fresh today-data; an
    # explicit single-date list (e.g. yesterday-only) is honoured as-is.
    log.info(
        "bulk-summarize job %s starting (%d groups × %d dates, dates=%s)",
        job_id, len(group_ids), len(dates), dates,
    )
    today_str = _date.today().isoformat()
    # Show the date in the chip whenever the range isn't simply "today" —
    # multi-day backfills AND single-non-today dates (e.g. yesterday-only)
    # need the date so the user can see which iteration they're watching.
    show_date_in_chip = len(dates) > 1 or any(d != today_str for d in dates)
    cancelled = False
    for date_str in dates:
        if cancelled:
            break
        for gid in group_ids:
            if job.cancel_requested:
                job.status = "cancelled"
                cancelled = True
                break
            job.current_id = gid
            label = names.get(gid, gid)
            job.current_name = f"{label} · {date_str}" if show_date_in_chip else label
            job.current_started_at = time.time()
            try:
                def _attempt(g=gid, d=date_str):
                    async def _go():
                        with Session(engine) as db:
                            return await summarize_group(g, db, target_date=d, job=job)
                    return _go()
                result = await _run_with_rate_limit_retry(_attempt, job)
                job.results.append({"group_id": gid, "for_date": date_str, **result})
            except asyncio.CancelledError:
                log.info("bulk job %s cancelled mid-group %s", job_id, gid)
                job.status = "cancelled"
                cancelled = True
                break  # don't bump job.done — this iteration didn't complete
            except Exception as e:
                log.exception("bulk job %s failed on group %s (date %s)", job_id, gid, date_str)
                job.errors.append({
                    "group_id": gid, "name": label,
                    "for_date": date_str, "msg": str(e),
                })
            job.done += 1
    # When the per-group loop is done, produce a cross-group 每日精华 for
    # each unique date that actually saw a successful per-group summary in
    # this job (today, yesterday, whatever the user picked). Previously this
    # was hard-coded to today; that broke yesterday-only and multi-day jobs.
    # Errors here are non-fatal — the per-group summaries are already saved.
    if job.status == "running" and job.results:
        result_dates = sorted({
            r.get("for_date") for r in job.results
            if r.get("for_date") and "daily_digest_id" not in r
        })
        for d in result_dates:
            try:
                digest_id = await _build_daily_digest_for_date(d, job)
                job.results.append({"daily_digest_id": digest_id, "for_date": d})
            except Exception as e:
                log.exception("daily-digest step failed for %s in job %s", d, job_id)
                job.errors.append({
                    "group_id": "__digest__",
                    "name": f"每日精华·{d}",
                    "msg": str(e),
                })
    if job.status == "running":
        job.status = "done"
    job.finished_at = time.time()
    log.info("bulk-summarize job %s finished: %d/%d ok, %d errors",
             job_id, job.done - len(job.errors), job.total, len(job.errors))
    for e in job.errors:
        log.warning("bulk job %s · group %s · %s",
                    job_id, e.get("name") or e.get("group_id"), (e.get("msg") or "")[:300])
    jobs.save_to_disk(job)


def _build_digest_payloads(db: Session, summaries: list[Asset]) -> list[dict]:
    """For each daily_summary asset, gather the topics (from its meta_json) and
    its sibling guest_share assets (same group + date). The resulting list is
    what llm.daily_digest() needs to produce a link-rich cross-group digest.

    A group can accumulate multiple daily_summary rows for the same date (each
    click of 「生成精华」 inserts a new one). Keep only the freshest per
    (group_id, for_date) so the digest doesn't render the same group twice."""
    by_key: dict[tuple[str, str], Asset] = {}
    for s in summaries:
        key = (s.group_id, s.for_date or "")
        cur = by_key.get(key)
        if cur is None or (s.created_at or 0) > (cur.created_at or 0):
            by_key[key] = s
    summaries = sorted(by_key.values(), key=lambda a: a.created_at or 0)
    group_names = {g.id: g.name for g in db.exec(select(Group)).all()}
    payloads: list[dict] = []
    for s in summaries:
        if not (s.body_md or "").strip():
            continue
        try:
            meta = json.loads(s.meta_json) if s.meta_json else {}
        except json.JSONDecodeError:
            meta = {}
        topics = [
            {"id": str(t.get("id", "")).strip(), "title": str(t.get("title", "")).strip()}
            for t in (meta.get("topics") or [])
            if str(t.get("id", "")).strip()
        ]
        shares = list(db.exec(
            select(Asset)
            .where(Asset.kind == "guest_share", Asset.group_id == s.group_id, Asset.for_date == s.for_date)
            .order_by(Asset.created_at)
        ).all())
        share_payload = []
        for sh in shares:
            try:
                sh_meta = json.loads(sh.meta_json) if sh.meta_json else {}
            except json.JSONDecodeError:
                sh_meta = {}
            share_payload.append({
                "id": sh.id,
                "title": sh.title,
                "speaker": str(sh_meta.get("speaker", "")).strip(),
            })
        payloads.append({
            "name": group_names.get(s.group_id, ""),
            "asset_id": s.id,
            "body_md": s.body_md,
            "topics": topics,
            "shares": share_payload,
        })
    return payloads


_DIGEST_ANCHOR_RE = re.compile(r"\]\(#(G\d+)(?:\.(T\d+|S\d+))?\)")


def _resolve_digest_anchors(body: str, payloads: list[dict]) -> str:
    """Swap short-token anchors emitted by the model (#G1, #G1.T2, #G1.S1) for
    real SPA hashes. Unknown tokens fall back to the group's summary page so a
    minor model hallucination doesn't produce a dead link."""
    g_to_asset: dict[str, str] = {}
    topic_lookup: dict[str, tuple[str, str]] = {}   # "G1.T2" -> (asset_id, "T2")
    share_lookup: dict[str, str] = {}                # "G1.S1" -> share_asset_id
    for i, p in enumerate(payloads, start=1):
        tag = f"G{i}"
        g_to_asset[tag] = p["asset_id"]
        for t in p.get("topics", []):
            tid = t.get("id")
            if tid:
                topic_lookup[f"{tag}.{tid}"] = (p["asset_id"], tid)
        for j, s in enumerate(p.get("shares", []), start=1):
            share_lookup[f"{tag}.S{j}"] = s["id"]

    def sub(m: re.Match) -> str:
        head, sub_tok = m.group(1), m.group(2)
        if not sub_tok:
            asset_id = g_to_asset.get(head)
            return f"](#asset/{asset_id})" if asset_id else m.group(0)
        full = f"{head}.{sub_tok}"
        if sub_tok.startswith("T"):
            hit = topic_lookup.get(full)
            if hit:
                return f"](#topic/{hit[0]}/{hit[1]})"
        if sub_tok.startswith("S"):
            sid = share_lookup.get(full)
            if sid:
                return f"](#asset/{sid})"
        asset_id = g_to_asset.get(head)
        return f"](#asset/{asset_id})" if asset_id else m.group(0)

    return _DIGEST_ANCHOR_RE.sub(sub, body)


async def _build_daily_digest_for_date(date_str: str, job: "jobs.Job") -> str:
    """Aggregate every `daily_summary` asset with for_date == date_str into a
    single `daily_digest` asset. Replaces any existing digest for that date so
    re-runs don't pile up. Returns the new digest's id. Idempotent — safe to
    call again after a rate-limit / partial failure once Claude is available."""
    job.current_id = "__digest__"
    job.current_name = f"每日精华汇总（{date_str}）"
    job.current_started_at = time.time()
    with Session(engine) as db:
        summaries = list(db.exec(
            select(Asset).where(Asset.kind == "daily_summary", Asset.for_date == date_str)
        ).all())
        if not summaries:
            raise RuntimeError(f"{date_str} 还没有任何单群精华，先生成几个再来")
        payloads = _build_digest_payloads(db, summaries)
        if not payloads:
            raise RuntimeError(f"{date_str} 的单群精华都没有正文内容")
        raw = await llm.daily_digest(date_str, payloads)
        body = _resolve_digest_anchors(raw, payloads)
        existing = list(db.exec(
            select(Asset).where(Asset.kind == "daily_digest", Asset.for_date == date_str)
        ).all())
        for old in existing:
            db.delete(old)
        digest = Asset(
            group_id="__cross__",
            kind="daily_digest",
            title=f"{date_str} · 每日精华",
            description=" ".join(body.split())[:240],
            body_md=body,
            for_date=date_str,
            meta_json=json.dumps(
                {"sources": [p["asset_id"] for p in payloads]},
                ensure_ascii=False,
            ),
        )
        db.add(digest)
        db.commit()
        db.refresh(digest)
        return digest.id


@router.post("/digest/rebuild")
async def rebuild_digest(date: str | None = None) -> dict:
    """Kick off a background job that rebuilds the cross-group 每日精华 for
    `date` (default today). Used to recover from a bulk run where the digest
    step failed (e.g. rate-limited) — once Claude is available again, run this
    instead of redoing all the per-group work."""
    target = date or _date.today().isoformat()
    with Session(engine) as db:
        n = len(list(db.exec(
            select(Asset).where(Asset.kind == "daily_summary", Asset.for_date == target)
        ).all()))
    if n == 0:
        raise HTTPException(400, f"{target} 还没有任何单群精华")
    job = jobs.create("digest_rebuild", total=1)
    job.current_id = "__digest__"
    job.current_name = f"每日精华汇总（{target}）"
    job.current_started_at = time.time()
    asyncio.create_task(_run_digest_rebuild(job.id, target))
    return {"job_id": job.id, "source_count": n, "date": target}


async def _run_digest_rebuild(job_id: str, date_str: str) -> None:
    job = jobs.get(job_id)
    if not job:
        return
    log.info("digest-rebuild job %s for %s", job_id, date_str)
    try:
        digest_id = await _build_daily_digest_for_date(date_str, job)
        job.results.append({"daily_digest_id": digest_id, "date": date_str})
    except Exception as e:
        log.exception("digest-rebuild job %s failed", job_id)
        job.errors.append({"group_id": "__digest__", "name": "每日精华", "msg": str(e)})
    job.done += 1
    job.status = "done"
    job.finished_at = time.time()
    log.info("digest-rebuild job %s finished", job_id)
    jobs.save_to_disk(job)


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    j = jobs.get(job_id)
    if j:
        return j.to_dict()
    # Fall back to disk so users can inspect a job after a server restart —
    # bulk failures used to vanish with the process; now they're recoverable.
    for snap in jobs.recent_from_disk(limit=100):
        if snap.get("id") == job_id:
            return snap
    raise HTTPException(404, "job not found")


@router.get("/jobs")
def list_active_jobs(include_recent: bool = False, limit: int = 20) -> list[dict]:
    out = [j.to_dict() for j in jobs.active()]
    if include_recent:
        seen = {j["id"] for j in out}
        for snap in jobs.recent_from_disk(limit=limit):
            if snap.get("id") in seen:
                continue
            out.append(snap)
            seen.add(snap.get("id"))
    return out


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    if not jobs.request_cancel(job_id):
        raise HTTPException(404, "job not found or not running")
    return {"ok": True}
