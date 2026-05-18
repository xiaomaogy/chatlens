"""In-memory background-job tracker.

Used so the long-running bulk-summarize loop lives on the server (and survives
a browser refresh) instead of in the browser's JS context. Finished jobs are
also persisted to `data/jobs/<id>.json` so failures are inspectable after a
server restart — that lets the user see *which* groups failed and *why* in a
bulk run, instead of just a fading toast.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .config import settings

log = logging.getLogger(__name__)

_jobs: dict[str, "Job"] = {}


def _jobs_dir():
    p = settings.data_dir / "jobs"
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class Job:
    id: str
    kind: str               # "bulk_summarize" — currently the only kind
    total: int
    done: int = 0
    current_id: Optional[str] = None
    current_name: Optional[str] = None
    current_phase: str = ""           # human-readable phase hint, e.g. "Claude 生成精华中…"
    current_started_at: float = 0.0
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    status: str = "running"          # running | done | cancelled
    errors: list[dict] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)
    cancel_requested: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "total": self.total,
            "done": self.done,
            "current_id": self.current_id,
            "current_name": self.current_name,
            "current_phase": self.current_phase,
            "current_started_at": self.current_started_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "errors": self.errors,
            "results": self.results,
            "cancel_requested": self.cancel_requested,
        }


def create(kind: str, total: int) -> Job:
    job = Job(id=str(uuid.uuid4()), kind=kind, total=total)
    _jobs[job.id] = job
    return job


def get(job_id: str) -> Optional[Job]:
    return _jobs.get(job_id)


def active() -> list[Job]:
    """Return jobs that are still running. Used by the frontend on page load
    to re-attach to an in-flight bulk run."""
    return [j for j in _jobs.values() if j.status == "running"]


def request_cancel(job_id: str) -> bool:
    j = _jobs.get(job_id)
    if not j or j.status != "running":
        return False
    j.cancel_requested = True
    return True


def prune_finished(older_than_seconds: int = 3600) -> int:
    """Drop completed jobs after a while so the dict doesn't grow unbounded."""
    cutoff = time.time() - older_than_seconds
    to_drop = [jid for jid, j in _jobs.items()
               if j.status != "running" and (j.finished_at or 0) < cutoff]
    for jid in to_drop:
        _jobs.pop(jid, None)
    return len(to_drop)


def save_to_disk(job: Job) -> None:
    """Snapshot a finished job to disk for post-mortem inspection.

    Writes to a .tmp sibling and atomically renames into place so an interrupted
    write doesn't leave a 0-byte file. Previously a TypeError from json.dumps
    or a mid-write crash would create empty .json files, masking the very
    failures we wanted post-mortem access to.
    """
    path = _jobs_dir() / f"{job.id}.json"
    tmp = path.with_name(path.name + ".tmp")
    try:
        # Explicit utf-8 because job data routinely contains Chinese and
        # py2app launches inherit no $LANG → Python's default encoding falls
        # back to ASCII and write_text dies on the first non-ASCII char.
        tmp.write_text(json.dumps(job.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except (OSError, TypeError, ValueError) as e:
        log.warning("could not persist job %s: %s", job.id, e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def recent_from_disk(limit: int = 20) -> list[dict]:
    """Return recently-persisted finished jobs, newest first."""
    try:
        paths = sorted(
            _jobs_dir().glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]
    except OSError:
        return []
    out: list[dict] = []
    for p in paths:
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("could not read persisted job %s: %s", p.name, e)
    return out
