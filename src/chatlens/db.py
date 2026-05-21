"""SQLModel-based persistence layer. SQLite by default.

Local-only single-user mode — no User table; the operator IS the app.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import event, inspect, text
from sqlmodel import Field, Session, SQLModel, create_engine

from .config import settings

log = logging.getLogger(__name__)


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_utc(dt: datetime) -> str:
    """ISO 8601 string with an explicit UTC offset.

    SQLite stores datetimes as TEXT without tzinfo, so values written as aware
    UTC come back naive. `.isoformat()` on a naive datetime emits no offset,
    and JavaScript's Date.parse then interprets that as **local** time — which
    silently shifts timestamps by the viewer's UTC offset (e.g. -8h for
    Beijing). Attaching `+00:00` here keeps the wire format unambiguous.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class Group(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    wechat_username: str = Field(index=True, unique=True)
    name: str
    members: int = 0
    monitored: bool = True
    pinned: bool = Field(default=False, index=True)
    # Two-layer hide state, both default False; the effective "is this group
    # hidden from the dashboard" check is `hidden OR user_hidden`.
    #   hidden       — mirrors the WeChat-side fold / 不显示 toggles. Synced
    #                  from `wechat-cli sessions` on every dashboard load and
    #                  overwritten unconditionally.
    #   user_hidden  — sticky manual override from the dashboard's eye button.
    #                  Survives syncs, so the operator can tuck away a group
    #                  in ChatLens even when WeChat itself hasn't folded it.
    hidden: bool = Field(default=False, index=True)
    user_hidden: bool = Field(default=False, index=True)
    accent_color: str = "from-indigo-500 to-violet-500"
    last_synced_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_now)


class Asset(SQLModel, table=True):
    """Generated content — a daily summary or a guest-share writeup."""
    id: str = Field(default_factory=_uuid, primary_key=True)
    group_id: str = Field(index=True, foreign_key="group.id")
    kind: str = Field(index=True)  # "daily_summary" | "guest_share"
    title: str
    description: str = ""
    body_md: str = ""
    # JSON blob: {"topics":[{id,title,lines:[int]}], "messages":{line:{ts,sender,text}}}
    meta_json: str = ""
    for_date: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_now)
    # Set when an existing asset's body/meta is overwritten in place (only
    # happens via the incremental path today). Stays None on fresh creates,
    # so the UI can render "生成于" vs "更新于" off of `is updated_at None?`.
    updated_at: Optional[datetime] = None


def _engine_args() -> dict:
    if settings.database_url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return {}


engine = create_engine(settings.database_url, **_engine_args())


if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record) -> None:  # noqa: ARG001
        """Tune every new SQLite connection.

        - journal_mode=WAL: readers no longer block on a writer. Without it, a
          background summarize job's COMMIT takes an exclusive lock and any
          page-load query stalls (up to busy_timeout) until the write finishes.
        - busy_timeout: wait briefly for a lock instead of erroring instantly.
        - synchronous=NORMAL: safe under WAL, and trims fsync stalls that would
          otherwise freeze the event loop during a commit.
        """
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


# Each entry: (table, column, sqlite-friendly DDL fragment to add the column)
_ADDITIVE_MIGRATIONS: list[tuple[str, str, str]] = [
    ("group", "pinned", "BOOLEAN NOT NULL DEFAULT 0"),
    ("asset", "meta_json", "TEXT NOT NULL DEFAULT ''"),
    ("group", "hidden", "BOOLEAN NOT NULL DEFAULT 0"),
    ("group", "user_hidden", "BOOLEAN NOT NULL DEFAULT 0"),
    ("asset", "updated_at", "TIMESTAMP"),
]


def _apply_additive_migrations() -> None:
    """Add columns that are present in the SQLModel but missing from the DB.

    SQLModel's `create_all` only creates missing tables, not missing columns.
    SQLite supports `ALTER TABLE … ADD COLUMN`, so we patch on the fly.
    """
    insp = inspect(engine)
    if not engine.url.get_backend_name().startswith("sqlite"):
        return
    with engine.begin() as conn:
        for table, column, ddl in _ADDITIVE_MIGRATIONS:
            if table not in insp.get_table_names():
                continue
            cols = {c["name"] for c in insp.get_columns(table)}
            if column in cols:
                continue
            log.info("migrating: adding %s.%s", table, column)
            conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {column} {ddl}'))


def init_db() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(engine)
    _apply_additive_migrations()


def get_session():
    with Session(engine) as session:
        yield session
