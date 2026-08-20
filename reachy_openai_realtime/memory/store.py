# ABOUTME: SQLite persistence for robot memory (spec §4): append-only notes,
# ABOUTME: rewritable summary tree, FTS5 index. Sync API; async callers use asyncio.to_thread.
from __future__ import annotations

import re
import secrets
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

NOTE_TEXT_MAX_CHARS = 500
SUMMARY_TEXT_MAX_CHARS = 1000
NOTE_KINDS = ("fact", "preference", "person", "place", "project", "note")
PINNED_BOOST = 2.0
RECENCY_BOOST = 1.0
RECENCY_WINDOW_DAYS = 7
FTS_MAX_TOKENS = 12

# Spec §4 DDL, verbatim. Migration 1 is frozen once shipped; schema changes add new versions.
MIGRATIONS: dict[int, str] = {
    1: """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE notes (
    id            TEXT PRIMARY KEY,
    scope         TEXT NOT NULL DEFAULT 'default',
    kind          TEXT NOT NULL,
    text          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    expires_at    TEXT,
    source        TEXT NOT NULL,
    confidence    REAL NOT NULL DEFAULT 1.0,
    pinned        INTEGER NOT NULL DEFAULT 0,
    deleted_at    TEXT,
    summarized_by TEXT
);

CREATE TABLE summaries (
    id          TEXT PRIMARY KEY,
    parent_id   TEXT,
    level       INTEGER NOT NULL,
    text        TEXT NOT NULL,
    covers_from TEXT NOT NULL,
    covers_to   TEXT NOT NULL,
    stale       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE VIRTUAL TABLE memory_fts USING fts5(
    id UNINDEXED,
    entry_type UNINDEXED,
    text
);
""",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def normalize_text(text: str) -> str:
    return " ".join(text.casefold().split())


def fts_match_expression(text: str) -> str:
    tokens = re.findall(r"\w+", text)[:FTS_MAX_TOKENS]
    return " OR ".join(f'"{token}"' for token in tokens)


@dataclass(frozen=True)
class Note:
    id: str
    kind: str
    text: str
    created_at: str
    last_used_at: str | None
    source: str
    pinned: bool
    summarized_by: str | None


@dataclass(frozen=True)
class Summary:
    id: str
    parent_id: str | None
    level: int
    text: str
    covers_from: str
    covers_to: str
    stale: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SearchHit:
    id: str
    entry_type: str
    kind: str
    text: str
    pinned: bool
    score: float


class MemoryStore:
    """Synchronous SQLite store. One connection, serialized by a lock, shared
    across event loops (main.py reruns asyncio.run per session)."""

    def __init__(
        self, path: Path, *, now: Callable[[], str] = utc_now, busy_timeout_ms: int = 5000
    ) -> None:
        self._path = Path(path)
        self._now = now
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None

    def open(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self._path, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
            self._migrate(connection)
            self._connection = connection

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _migrate(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        applied: set[int] = set()
        if row is not None:
            applied = {
                r["version"] for r in connection.execute("SELECT version FROM schema_migrations")
            }
        for version in sorted(MIGRATIONS):
            if version in applied:
                continue
            with connection:
                connection.executescript(MIGRATIONS[version])
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, self._now()),
                )

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise sqlite3.ProgrammingError("memory store is not open")
        return self._connection

    def insert_note(self, text: str, kind: str, source: str) -> Note:
        with self._lock:
            note_id = "mem_" + secrets.token_hex(8)
            created_at = self._now()
            with self._db() as db:
                db.execute(
                    "INSERT INTO notes (id, kind, text, created_at, source) VALUES (?, ?, ?, ?, ?)",
                    (note_id, kind, text, created_at, source),
                )
                db.execute(
                    "INSERT INTO memory_fts (id, entry_type, text) VALUES (?, 'note', ?)",
                    (note_id, text),
                )
            return Note(note_id, kind, text, created_at, None, source, False, None)

    def get_note(self, note_id: str) -> Note | None:
        with self._lock:
            row = self._db().execute(
                "SELECT * FROM notes WHERE id = ? AND deleted_at IS NULL", (note_id,)
            ).fetchone()
            return None if row is None else _note_from_row(row)

    def find_live_note_by_normalized(self, normalized: str) -> Note | None:
        with self._lock:
            rows = self._db().execute("SELECT * FROM notes WHERE deleted_at IS NULL").fetchall()
        for row in rows:
            if normalize_text(row["text"]) == normalized:
                return _note_from_row(row)
        return None

    def touch_notes(self, note_ids: list[str]) -> None:
        if not note_ids:
            return
        with self._lock:
            timestamp = self._now()
            with self._db() as db:
                db.executemany(
                    "UPDATE notes SET last_used_at = ? WHERE id = ?",
                    [(timestamp, note_id) for note_id in note_ids],
                )


def _note_from_row(row: sqlite3.Row) -> Note:
    return Note(
        id=row["id"],
        kind=row["kind"],
        text=row["text"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        source=row["source"],
        pinned=bool(row["pinned"]),
        summarized_by=row["summarized_by"],
    )
