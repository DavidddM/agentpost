"""SQLite storage for agentpost.

Every call opens a short-lived connection, so the server and the admin command
can share one file. The README describes the tables and THREAD_ROWS.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
SCHEMA_VERSION = 2
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
LAST_SEEN_STEP = timedelta(seconds=60)
WAL_SWITCH_SECONDS = 10

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS devs (
        name         TEXT PRIMARY KEY,
        token_hash   TEXT NOT NULL UNIQUE,
        created_at   TEXT NOT NULL,
        revoked_at   TEXT,
        last_seen_at TEXT,
        baseline_id  INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS threads (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        title      TEXT NOT NULL,
        private    INTEGER NOT NULL DEFAULT 0,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS thread_members (
        thread_id INTEGER NOT NULL REFERENCES threads (id),
        dev       TEXT NOT NULL,
        PRIMARY KEY (thread_id, dev)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id  INTEGER NOT NULL REFERENCES threads (id),
        sender     TEXT NOT NULL,
        body       TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS messages_thread ON messages (thread_id, id)",
    """
    CREATE TABLE IF NOT EXISTS thread_reads (
        thread_id    INTEGER NOT NULL REFERENCES threads (id),
        dev          TEXT NOT NULL,
        last_read_id INTEGER NOT NULL,
        PRIMARY KEY (thread_id, dev)
    )
    """,
)

THREAD_ROWS = """
SELECT t.id, t.title, t.private, t.created_by, t.created_at,
       (SELECT COUNT(*) FROM messages m WHERE m.thread_id = t.id) AS total,
       MAX(COALESCE(r.last_read_id, 0), :baseline) AS read_id,
       (SELECT COUNT(*) FROM messages m
         WHERE m.thread_id = t.id AND m.sender != :dev
           AND m.id > MAX(COALESCE(r.last_read_id, 0), :baseline)) AS new,
       lm.id AS last_id, lm.sender AS last_from, lm.created_at AS last_at,
       lm.body AS last_body
  FROM threads t
  JOIN messages lm
    ON lm.id = (SELECT MAX(id) FROM messages WHERE thread_id = t.id)
  LEFT JOIN thread_reads r ON r.thread_id = t.id AND r.dev = :dev
 WHERE (t.private = 0
        OR EXISTS (SELECT 1 FROM thread_members tm
                    WHERE tm.thread_id = t.id AND tm.dev = :dev))
"""


class DevError(ValueError):
    """A problem with a dev name the caller can fix."""


def db_path() -> Path:
    return Path(os.environ.get("AGENTPOST_DB", "/data/agentpost.sqlite"))


def now() -> str:
    return datetime.now(timezone.utc).strftime(TIME_FORMAT)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        with conn:
            yield conn
    finally:
        conn.close()


def unusable(exc: Exception) -> str:
    """One line telling the server owner the database file cannot be used."""
    return f"cannot use the database at {db_path()}: {exc}"


def _switch_to_wal(conn: sqlite3.Connection) -> None:
    """Put the file in WAL mode, which every later connection inherits.

    SQLite does not wait for a lock during this switch, so a busy file is
    retried here until WAL_SWITCH_SECONDS have passed.
    """
    deadline = time.monotonic() + WAL_SWITCH_SECONDS
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            busy = exc.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
            if not busy or time.monotonic() >= deadline:
                raise
        time.sleep(0.05)


def init() -> None:
    """Create the tables, or exit with one line if the file cannot be used.

    One transaction takes the write lock first, so processes starting together
    wait for each other and an interrupted start leaves nothing half-made.
    """
    try:
        with connect() as conn:
            _switch_to_wal(conn)
            conn.execute("BEGIN IMMEDIATE")
            _create_tables(conn)
    except (sqlite3.Error, OSError) as exc:
        raise SystemExit(unusable(exc)) from None


def _create_tables(conn: sqlite3.Connection) -> None:
    """Create the schema, refusing a file written by another schema version."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    populated = conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    if populated and version != SCHEMA_VERSION:
        raise SystemExit(
            f"{db_path()} was created by a different agentpost version "
            f"(schema {version}, this build needs {SCHEMA_VERSION}). "
            "Move the file away to start fresh."
        )
    for statement in SCHEMA:
        conn.execute(statement)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _check_name(name: str) -> str:
    name = name.strip().lower()
    if not NAME_RE.match(name):
        raise DevError(
            "name must be 1-32 chars: lowercase letters, digits, '-' or '_', "
            "starting with a letter or digit"
        )
    return name


def add_dev(name: str) -> str:
    """Register a dev and return their token. Only the token's hash is stored."""
    name = _check_name(name)
    token = secrets.token_urlsafe(32)
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO devs (name, token_hash, created_at, baseline_id) "
                "VALUES (?, ?, ?, (SELECT COALESCE(MAX(id), 0) FROM messages))",
                (name, hash_token(token), now()),
            )
    except sqlite3.IntegrityError:
        raise DevError(
            f"dev '{name}' already exists (use rotate to issue a new token)"
        ) from None
    return token


def rotate_dev(name: str) -> str:
    """Issue a new token. The old one stops working; a revoked dev is active again."""
    name = _check_name(name)
    token = secrets.token_urlsafe(32)
    with connect() as conn:
        cur = conn.execute(
            "UPDATE devs SET token_hash = ?, revoked_at = NULL WHERE name = ?",
            (hash_token(token), name),
        )
        if cur.rowcount == 0:
            raise DevError(f"no dev named '{name}'")
    return token


def revoke_dev(name: str) -> None:
    name = _check_name(name)
    with connect() as conn:
        cur = conn.execute(
            "UPDATE devs SET revoked_at = ? WHERE name = ? AND revoked_at IS NULL",
            (now(), name),
        )
        if cur.rowcount == 0:
            raise DevError(f"no active dev named '{name}'")


def list_devs(include_revoked: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT name, created_at, revoked_at, last_seen_at FROM devs"
    if not include_revoked:
        sql += " WHERE revoked_at IS NULL"
    with connect() as conn:
        return conn.execute(sql + " ORDER BY name").fetchall()


def dev_for_token(token: str) -> str | None:
    """Return the active dev that owns this token.

    last_seen_at is refreshed at most once a minute and never waited for, so an
    ordinary request only reads and cannot be held up by another writer.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT name, last_seen_at FROM devs "
            "WHERE token_hash = ? AND revoked_at IS NULL",
            (hash_token(token),),
        ).fetchone()
        if row is None:
            return None
        current = datetime.now(timezone.utc)
        stale_before = (current - LAST_SEEN_STEP).strftime(TIME_FORMAT)
        if row["last_seen_at"] is None or row["last_seen_at"] < stale_before:
            conn.execute("PRAGMA busy_timeout=0")
            try:
                conn.execute(
                    "UPDATE devs SET last_seen_at = ? WHERE name = ?",
                    (current.strftime(TIME_FORMAT), row["name"]),
                )
            except sqlite3.OperationalError:
                pass
        return row["name"]


def unknown_devs(names: list[str]) -> list[str]:
    """The names that are not active devs."""
    with connect() as conn:
        rows = conn.execute("SELECT name FROM devs WHERE revoked_at IS NULL")
        active = {r["name"] for r in rows}
    return [n for n in names if n not in active]


def _baseline(conn: sqlite3.Connection, dev: str) -> int:
    row = conn.execute("SELECT baseline_id FROM devs WHERE name = ?", (dev,))
    return row.fetchone()["baseline_id"]


def _visible_thread(
    conn: sqlite3.Connection, dev: str, thread_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        THREAD_ROWS + " AND t.id = :id",
        {"dev": dev, "baseline": _baseline(conn, dev), "id": thread_id},
    ).fetchone()


def _with_members(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict]:
    threads = []
    for row in rows:
        members = conn.execute(
            "SELECT dev FROM thread_members WHERE thread_id = ? ORDER BY dev",
            (row["id"],),
        )
        threads.append({**dict(row), "members": [m["dev"] for m in members]})
    return threads


def start_thread(
    dev: str, title: str, body: str, members: list[str]
) -> tuple[int, int, str]:
    """Create a thread with its first message.

    Any `members` besides the creator make the thread private to that group.
    Returns (thread_id, message_id, created_at).
    """
    created = now()
    with connect() as conn:
        thread_id = conn.execute(
            "INSERT INTO threads (title, private, created_by, created_at) "
            "VALUES (?, ?, ?, ?)",
            (title, 1 if members else 0, dev, created),
        ).lastrowid
        if members:
            conn.executemany(
                "INSERT INTO thread_members (thread_id, dev) VALUES (?, ?)",
                [(thread_id, m) for m in sorted({dev, *members})],
            )
        message_id = conn.execute(
            "INSERT INTO messages (thread_id, sender, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (thread_id, dev, body, created),
        ).lastrowid
        return int(thread_id), int(message_id), created


def reply(dev: str, thread_id: int, body: str) -> tuple[int, str] | None:
    """Add a message to a thread the dev can see.

    Returns (message_id, created_at), or None if there is no such thread.
    """
    created = now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if _visible_thread(conn, dev, thread_id) is None:
            return None
        message_id = conn.execute(
            "INSERT INTO messages (thread_id, sender, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (thread_id, dev, body, created),
        ).lastrowid
        return int(message_id), created


def list_threads(
    dev: str, new_only: bool, limit: int, offset: int = 0
) -> tuple[list[dict], bool]:
    """Threads the dev may see, most recently active first.

    Returns (threads, has_more).
    """
    unread = "WHERE new > 0" if new_only else ""
    sql = (
        f"SELECT * FROM ({THREAD_ROWS}) {unread} "
        "ORDER BY last_id DESC LIMIT :limit OFFSET :offset"
    )
    with connect() as conn:
        params = {
            "dev": dev,
            "baseline": _baseline(conn, dev),
            "limit": limit + 1,
            "offset": offset,
        }
        rows = conn.execute(sql, params).fetchall()
        return _with_members(conn, rows[:limit]), len(rows) > limit


def fetch_messages(
    dev: str, thread_id: int, since_id: int, limit: int
) -> tuple[dict, list[sqlite3.Row], bool] | None:
    """A thread the dev can see and its messages with id > since_id, oldest first.

    Returns (thread, messages, has_more), or None if there is no such thread.
    Does not change what counts as read; see mark_read.
    """
    with connect() as conn:
        row = _visible_thread(conn, dev, thread_id)
        if row is None:
            return None
        rows = conn.execute(
            "SELECT id, sender, body, created_at FROM messages "
            "WHERE thread_id = ? AND id > ? ORDER BY id LIMIT ?",
            (thread_id, since_id, limit + 1),
        ).fetchall()
        return _with_members(conn, [row])[0], rows[:limit], len(rows) > limit


def mark_read(dev: str, thread_id: int, since_id: int, last_id: int) -> bool:
    """Record that the dev was sent the thread's messages in (since_id, last_id].

    The position only moves forward, and never past unread messages from other
    devs that since_id skipped. Returns False, leaving the messages unread, if
    another writer holds the database for more than a moment.
    """
    with connect() as conn:
        conn.execute("PRAGMA busy_timeout=500")
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return False
        row = _visible_thread(conn, dev, thread_id)
        if row is None or last_id <= row["read_id"]:
            return True
        skipped = conn.execute(
            "SELECT 1 FROM messages WHERE thread_id = ? AND sender != ? "
            "AND id > ? AND id <= ? LIMIT 1",
            (thread_id, dev, row["read_id"], since_id),
        ).fetchone()
        if skipped:
            return True
        conn.execute(
            "INSERT INTO thread_reads (thread_id, dev, last_read_id) "
            "VALUES (?, ?, ?) ON CONFLICT (thread_id, dev) "
            "DO UPDATE SET last_read_id = excluded.last_read_id",
            (thread_id, dev, last_id),
        )
        return True
