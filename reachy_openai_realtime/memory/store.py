# ABOUTME: SQLite persistence for robot memory (spec §4): append-only notes,
# ABOUTME: rewritable summary tree, FTS5 index. Sync API; async callers use asyncio.to_thread.
# Known limitation: FTS5 unicode61 tokenizer does not segment CJK well; Japanese recall is limited.
from __future__ import annotations

import re
import secrets
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
            # Wrap DDL + version-row INSERT in a single script so both land atomically.
            # A crash between executescript (which auto-commits DDL) and the INSERT would
            # leave tables present but no version row, causing the next open to fail.
            # Both values are internal (int from our dict, ISO string we generated) — no injection surface.
            timestamp = self._now()
            atomic_script = (
                f"BEGIN;\n"
                f"{MIGRATIONS[version]}\n"
                f"INSERT INTO schema_migrations (version, applied_at) VALUES ({version}, '{timestamp}');\n"
                f"COMMIT;\n"
            )
            connection.executescript(atomic_script)

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
        # All IDs in the batch share a single timestamp so last_used_at is consistent
        # within the call — no skew between the first and last note in a retrieval set.
        if not note_ids:
            return
        with self._lock:
            timestamp = self._now()
            with self._db() as db:
                db.executemany(
                    "UPDATE notes SET last_used_at = ? WHERE id = ?",
                    [(timestamp, note_id) for note_id in note_ids],
                )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, match_expression: str, limit: int) -> list[SearchHit]:
        if not match_expression:
            return []
        with self._lock:
            rows = self._db().execute(
                "SELECT id, entry_type, bm25(memory_fts) AS rank FROM memory_fts "
                "WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?",
                (match_expression, limit * 3),  # overfetch: tombstone filtering happens below
            ).fetchall()
            cutoff = _recency_cutoff(self._now())
            hits: list[SearchHit] = []
            for row in rows:
                if row["entry_type"] == "note":
                    note_row = self._db().execute(
                        "SELECT * FROM notes WHERE id = ? AND deleted_at IS NULL", (row["id"],)
                    ).fetchone()
                    if note_row is None:
                        continue
                    score = float(row["rank"])
                    if note_row["pinned"]:
                        score -= PINNED_BOOST
                    last_used = note_row["last_used_at"] or ""
                    if max(note_row["created_at"], last_used) >= cutoff:
                        score -= RECENCY_BOOST
                    hits.append(
                        SearchHit(
                            note_row["id"], "note", note_row["kind"], note_row["text"],
                            bool(note_row["pinned"]), score,
                        )
                    )
                else:
                    summary_row = self._db().execute(
                        "SELECT * FROM summaries WHERE id = ?", (row["id"],)
                    ).fetchone()
                    if summary_row is None:
                        continue
                    hits.append(
                        SearchHit(summary_row["id"], "summary", "summary", summary_row["text"],
                                  False, float(row["rank"]))
                    )
            hits.sort(key=lambda hit: hit.score)
            return hits[:limit]

    # ------------------------------------------------------------------
    # Pin management
    # ------------------------------------------------------------------

    def set_pinned(self, note_id: str, pinned: bool) -> bool:
        with self._lock:
            with self._db() as db:
                cursor = db.execute(
                    "UPDATE notes SET pinned = ? WHERE id = ? AND deleted_at IS NULL",
                    (1 if pinned else 0, note_id),
                )
            return cursor.rowcount > 0

    def pinned_notes(self) -> list[Note]:
        with self._lock:
            rows = self._db().execute(
                "SELECT * FROM notes WHERE deleted_at IS NULL AND pinned = 1 ORDER BY created_at"
            ).fetchall()
            return [_note_from_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Tombstoning
    # ------------------------------------------------------------------

    def tombstone_note(self, note_id: str) -> bool:
        with self._lock:
            note_row = self._db().execute(
                "SELECT summarized_by FROM notes WHERE id = ? AND deleted_at IS NULL", (note_id,)
            ).fetchone()
            if note_row is None:
                return False
            deleted_at = self._now()
            summarized_by = note_row["summarized_by"]
            with self._db() as db:
                db.execute(
                    "UPDATE notes SET deleted_at = ? WHERE id = ?", (deleted_at, note_id)
                )
                db.execute("DELETE FROM memory_fts WHERE id = ?", (note_id,))
                # Walk up the ancestor chain marking each summary stale.
                current_id = summarized_by
                while current_id is not None:
                    db.execute("UPDATE summaries SET stale = 1 WHERE id = ?", (current_id,))
                    parent_row = db.execute(
                        "SELECT parent_id FROM summaries WHERE id = ?", (current_id,)
                    ).fetchone()
                    current_id = parent_row["parent_id"] if parent_row is not None else None
            return True

    # ------------------------------------------------------------------
    # Summary queries
    # ------------------------------------------------------------------

    def root_summary(self) -> Summary | None:
        with self._lock:
            row = self._db().execute(
                "SELECT * FROM summaries WHERE parent_id IS NULL ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            return None if row is None else _summary_from_row(row)

    def get_summary(self, summary_id: str) -> Summary | None:
        with self._lock:
            row = self._db().execute(
                "SELECT * FROM summaries WHERE id = ?", (summary_id,)
            ).fetchone()
            return None if row is None else _summary_from_row(row)

    def children_of(self, summary_id: str) -> list[Summary]:
        with self._lock:
            rows = self._db().execute(
                "SELECT * FROM summaries WHERE parent_id = ? ORDER BY created_at", (summary_id,)
            ).fetchall()
            return [_summary_from_row(r) for r in rows]

    def notes_covered_by(self, summary_id: str) -> list[Note]:
        with self._lock:
            rows = self._db().execute(
                "SELECT * FROM notes WHERE summarized_by = ? AND deleted_at IS NULL ORDER BY created_at",
                (summary_id,),
            ).fetchall()
            return [_note_from_row(r) for r in rows]

    def unconsolidated_notes(self, limit: int) -> list[Note]:
        with self._lock:
            rows = self._db().execute(
                "SELECT * FROM notes WHERE summarized_by IS NULL AND deleted_at IS NULL "
                "ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
            return [_note_from_row(r) for r in rows]

    def count_unconsolidated(self) -> int:
        with self._lock:
            row = self._db().execute(
                "SELECT COUNT(*) FROM notes WHERE summarized_by IS NULL AND deleted_at IS NULL"
            ).fetchone()
            return row[0]

    def count_stale(self) -> int:
        with self._lock:
            row = self._db().execute(
                "SELECT COUNT(*) FROM summaries WHERE stale = 1"
            ).fetchone()
            return row[0]

    def count_notes(self) -> int:
        with self._lock:
            row = self._db().execute(
                "SELECT COUNT(*) FROM notes WHERE deleted_at IS NULL"
            ).fetchone()
            return row[0]

    def stale_summaries(self) -> list[Summary]:
        with self._lock:
            rows = self._db().execute(
                "SELECT * FROM summaries WHERE stale = 1 ORDER BY level ASC, created_at ASC"
            ).fetchall()
            return [_summary_from_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Summary writes
    # ------------------------------------------------------------------

    def insert_summary(
        self,
        parent_id: str | None,
        level: int,
        text: str,
        covers_from: str,
        covers_to: str,
    ) -> Summary:
        with self._lock:
            summary_id = "sum_" + secrets.token_hex(8)
            now = self._now()
            with self._db() as db:
                db.execute(
                    "INSERT INTO summaries (id, parent_id, level, text, covers_from, covers_to, "
                    "stale, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (summary_id, parent_id, level, text, covers_from, covers_to, now, now),
                )
                db.execute(
                    "INSERT INTO memory_fts (id, entry_type, text) VALUES (?, 'summary', ?)",
                    (summary_id, text),
                )
            return Summary(summary_id, parent_id, level, text, covers_from, covers_to, False, now, now)

    def update_summary(
        self,
        summary_id: str,
        text: str,
        covers_from: str,
        covers_to: str,
        *,
        level: int | None = None,
    ) -> None:
        with self._lock:
            now = self._now()
            with self._db() as db:
                if level is not None:
                    db.execute(
                        "UPDATE summaries SET text=?, covers_from=?, covers_to=?, stale=0, "
                        "updated_at=?, level=? WHERE id=?",
                        (text, covers_from, covers_to, now, level, summary_id),
                    )
                else:
                    db.execute(
                        "UPDATE summaries SET text=?, covers_from=?, covers_to=?, stale=0, "
                        "updated_at=? WHERE id=?",
                        (text, covers_from, covers_to, now, summary_id),
                    )
                db.execute("DELETE FROM memory_fts WHERE id = ?", (summary_id,))
                db.execute(
                    "INSERT INTO memory_fts (id, entry_type, text) VALUES (?, 'summary', ?)",
                    (summary_id, text),
                )

    def set_summary_parent(self, summary_id: str, parent_id: str) -> None:
        with self._lock:
            now = self._now()
            with self._db() as db:
                db.execute(
                    "UPDATE summaries SET parent_id = ?, updated_at = ? WHERE id = ?",
                    (parent_id, now, summary_id),
                )

    def delete_summary(self, summary_id: str) -> None:
        with self._lock:
            row = self._db().execute(
                "SELECT parent_id FROM summaries WHERE id = ?", (summary_id,)
            ).fetchone()
            parent_id = row["parent_id"] if row is not None else None
            with self._db() as db:
                db.execute("DELETE FROM summaries WHERE id = ?", (summary_id,))
                db.execute("DELETE FROM memory_fts WHERE id = ?", (summary_id,))
                if parent_id is not None:
                    db.execute("UPDATE summaries SET stale = 1 WHERE id = ?", (parent_id,))

    def mark_summarized(self, note_ids: list[str], summary_id: str) -> None:
        if not note_ids:
            return
        with self._lock, self._db() as db:
            db.executemany(
                "UPDATE notes SET summarized_by = ? WHERE id = ?",
                [(summary_id, note_id) for note_id in note_ids],
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


def _summary_from_row(row: sqlite3.Row) -> Summary:
    return Summary(
        id=row["id"],
        parent_id=row["parent_id"],
        level=row["level"],
        text=row["text"],
        covers_from=row["covers_from"],
        covers_to=row["covers_to"],
        stale=bool(row["stale"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _recency_cutoff(now_iso: str) -> str:
    now = datetime.strptime(now_iso, "%Y-%m-%dT%H:%M:%S.%f+00:00").replace(tzinfo=timezone.utc)
    return (now - timedelta(days=RECENCY_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
