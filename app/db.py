"""SQLite storage.

Small enough to read in one sitting, which is the point: the owner of a paid
community is trusting this with their revenue, and a schema you can explain in
a sentence is easier to trust than an ORM.

Tables
------
members        one row per Telegram user we have ever seen pay
pending_links  short-lived tokens that connect a Stripe checkout to a user
seen_events    Stripe event ids, so a redelivered webhook is a no-op
jobs           scheduled work: onboarding drip, grace-period expiry
audit          append-only log of everything that changed a member's access
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    telegram_user_id      INTEGER PRIMARY KEY,
    username              TEXT,
    first_name            TEXT,
    stripe_customer_id    TEXT,
    stripe_subscription_id TEXT,
    status                TEXT    NOT NULL DEFAULT 'unknown',
    entitled_until        INTEGER,
    in_chat               INTEGER NOT NULL DEFAULT 0,
    joined_at             INTEGER,
    removed_at            INTEGER,
    last_synced_at        INTEGER,
    created_at            INTEGER NOT NULL,
    updated_at            INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_members_customer
    ON members (stripe_customer_id);
CREATE INDEX IF NOT EXISTS idx_members_subscription
    ON members (stripe_subscription_id);
CREATE INDEX IF NOT EXISTS idx_members_status
    ON members (status);

CREATE TABLE IF NOT EXISTS pending_links (
    token            TEXT PRIMARY KEY,
    telegram_user_id INTEGER NOT NULL,
    username         TEXT,
    first_name       TEXT,
    created_at       INTEGER NOT NULL,
    consumed_at      INTEGER
);

CREATE TABLE IF NOT EXISTS seen_events (
    stripe_event_id TEXT PRIMARY KEY,
    event_type      TEXT NOT NULL,
    received_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT    NOT NULL,
    subject_id   INTEGER,
    payload      TEXT,
    run_at       INTEGER NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    done_at      INTEGER,
    last_error   TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_pending
    ON jobs (done_at, run_at);

CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    action     TEXT    NOT NULL,
    subject_id INTEGER,
    detail     TEXT,
    simulated  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit (ts);
"""


def now() -> int:
    return int(time.time())


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    # -- lifecycle ----------------------------------------------------------

    async def connect(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() has not been called")
        return self._conn

    # -- members ------------------------------------------------------------

    async def upsert_member(self, telegram_user_id: int, **fields: Any) -> None:
        ts = now()
        existing = await self.get_member(telegram_user_id)
        if existing is None:
            columns = ["telegram_user_id", "created_at", "updated_at", *fields.keys()]
            values = [telegram_user_id, ts, ts, *fields.values()]
            placeholders = ", ".join("?" for _ in columns)
            await self.conn.execute(
                f"INSERT INTO members ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )
        elif fields:
            assignments = ", ".join(f"{key} = ?" for key in fields)
            await self.conn.execute(
                f"UPDATE members SET {assignments}, updated_at = ? "
                f"WHERE telegram_user_id = ?",
                [*fields.values(), ts, telegram_user_id],
            )
        else:
            await self.conn.execute(
                "UPDATE members SET updated_at = ? WHERE telegram_user_id = ?",
                (ts, telegram_user_id),
            )
        await self.conn.commit()

    async def get_member(self, telegram_user_id: int) -> Optional[Dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM members WHERE telegram_user_id = ?", (telegram_user_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_member_by_customer(
        self, stripe_customer_id: str
    ) -> Optional[Dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM members WHERE stripe_customer_id = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (stripe_customer_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_member_by_subscription(
        self, stripe_subscription_id: str
    ) -> Optional[Dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM members WHERE stripe_subscription_id = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (stripe_subscription_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def all_members(self) -> List[Dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM members ORDER BY created_at ASC"
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def members_in_chat(self) -> List[Dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM members WHERE in_chat = 1 ORDER BY created_at ASC"
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def count_by_status(self) -> Dict[str, int]:
        cursor = await self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM members GROUP BY status"
        )
        return {row["status"]: row["n"] for row in await cursor.fetchall()}

    # -- pending links ------------------------------------------------------

    async def create_pending_link(
        self,
        token: str,
        telegram_user_id: int,
        username: Optional[str],
        first_name: Optional[str],
    ) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO pending_links "
            "(token, telegram_user_id, username, first_name, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token, telegram_user_id, username, first_name, now()),
        )
        await self.conn.commit()

    async def consume_pending_link(self, token: str) -> Optional[Dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM pending_links WHERE token = ?", (token,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        await self.conn.execute(
            "UPDATE pending_links SET consumed_at = ? WHERE token = ?",
            (now(), token),
        )
        await self.conn.commit()
        return dict(row)

    async def purge_stale_links(self, older_than_seconds: int = 7 * 24 * 3600) -> int:
        cursor = await self.conn.execute(
            "DELETE FROM pending_links WHERE consumed_at IS NULL AND created_at < ?",
            (now() - older_than_seconds,),
        )
        await self.conn.commit()
        return cursor.rowcount or 0

    # -- idempotency --------------------------------------------------------

    async def mark_event_seen(self, event_id: str, event_type: str) -> bool:
        """Return True the first time an event id is seen, False on redelivery."""
        try:
            await self.conn.execute(
                "INSERT INTO seen_events (stripe_event_id, event_type, received_at) "
                "VALUES (?, ?, ?)",
                (event_id, event_type, now()),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def purge_old_events(self, older_than_seconds: int = 30 * 24 * 3600) -> int:
        cursor = await self.conn.execute(
            "DELETE FROM seen_events WHERE received_at < ?",
            (now() - older_than_seconds,),
        )
        await self.conn.commit()
        return cursor.rowcount or 0

    # -- jobs ---------------------------------------------------------------

    async def schedule_job(
        self,
        kind: str,
        run_at: int,
        subject_id: Optional[int] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        await self.conn.execute(
            "INSERT INTO jobs (kind, subject_id, payload, run_at) VALUES (?, ?, ?, ?)",
            (kind, subject_id, json.dumps(payload or {}), run_at),
        )
        await self.conn.commit()

    async def due_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM jobs WHERE done_at IS NULL AND run_at <= ? "
            "ORDER BY run_at ASC LIMIT ?",
            (now(), limit),
        )
        jobs = []
        for row in await cursor.fetchall():
            job = dict(row)
            job["payload"] = json.loads(job["payload"] or "{}")
            jobs.append(job)
        return jobs

    async def complete_job(self, job_id: int) -> None:
        await self.conn.execute(
            "UPDATE jobs SET done_at = ? WHERE id = ?", (now(), job_id)
        )
        await self.conn.commit()

    async def fail_job(self, job_id: int, error: str, retry_in: int = 300) -> None:
        await self.conn.execute(
            "UPDATE jobs SET attempts = attempts + 1, last_error = ?, run_at = ? "
            "WHERE id = ?",
            (error[:500], now() + retry_in, job_id),
        )
        await self.conn.commit()

    async def cancel_jobs_for(self, subject_id: int, kind: Optional[str] = None) -> int:
        if kind:
            cursor = await self.conn.execute(
                "UPDATE jobs SET done_at = ? "
                "WHERE subject_id = ? AND kind = ? AND done_at IS NULL",
                (now(), subject_id, kind),
            )
        else:
            cursor = await self.conn.execute(
                "UPDATE jobs SET done_at = ? WHERE subject_id = ? AND done_at IS NULL",
                (now(), subject_id),
            )
        await self.conn.commit()
        return cursor.rowcount or 0

    # -- audit --------------------------------------------------------------

    async def record(
        self,
        action: str,
        subject_id: Optional[int] = None,
        detail: str = "",
        simulated: bool = False,
    ) -> None:
        await self.conn.execute(
            "INSERT INTO audit (ts, action, subject_id, detail, simulated) "
            "VALUES (?, ?, ?, ?, ?)",
            (now(), action, subject_id, detail, 1 if simulated else 0),
        )
        await self.conn.commit()

    async def recent_audit(self, limit: int = 20) -> List[Dict[str, Any]]:
        cursor = await self.conn.execute(
            "SELECT * FROM audit ORDER BY ts DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def count_actions_since(self, action: str, since: int) -> int:
        cursor = await self.conn.execute(
            "SELECT COUNT(*) AS n FROM audit WHERE action = ? AND ts >= ?",
            (action, since),
        )
        row = await cursor.fetchone()
        return row["n"] if row else 0
