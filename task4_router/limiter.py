"""A token-aware sliding-window rate limiter backed by on-disk SQLite.

Two things make this harder than it looks.

**It has to be a sliding window, not a per-minute bucket.** A bucket that resets
on the clock boundary lets a tenant spend the full limit at 11:59:59 and the full
limit again at 12:00:00 -- double the intended rate, at the worst possible moment.
So usage is stored as `(tenant_key, ts, tokens)` rows and every check sums the
last 60 seconds.

**The check and the insert are one read-modify-write.** Two concurrent requests
that each read 49,000 will both decide there is room for 5,000. Measured, with
50 concurrent 5,000-token requests against a 50,000 limit
(`tests/test_task4_router.py::test_without_a_transaction_the_check_and_insert_race`):

    no transaction at all   admits 65,000-85,000   -- the race, plainly
    BEGIN DEFERRED          admits 50,000, but raises "database is locked"
    BEGIN IMMEDIATE         admits exactly 50,000

The middle row is the interesting one. A deferred transaction that reads and then
writes cannot be upgraded once another writer has committed under it -- SQLite
refuses rather than corrupting the snapshot, so the *correctness* is safe either
way and what you lose is availability: `busy_timeout` cannot help, because there
is nothing to wait for. `BEGIN IMMEDIATE` takes the write lock before the read,
so contenders queue on `busy_timeout` and succeed instead of failing. WAL keeps
readers from being blocked by that writer.

And the amount to charge is not known when the decision has to be made: output
tokens do not exist until the response does. Hence reserve-then-reconcile.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from common.logging import get_logger

logger = get_logger(__name__)

DEFAULT_LIMIT = 50_000
DEFAULT_WINDOW_SECONDS = 60.0
DEFAULT_BUSY_TIMEOUT = 5.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_key  TEXT    NOT NULL,
    ts          REAL    NOT NULL,
    tokens      INTEGER NOT NULL
);
-- Every query is "this tenant, since this timestamp"; the composite index makes
-- both the sum and the eviction sweep an index range scan rather than a table scan.
CREATE INDEX IF NOT EXISTS idx_usage_tenant_ts ON usage (tenant_key, ts);
"""


class RateLimitExceeded(Exception):
    """The tenant's window is full. Carries what the caller needs to retry."""

    def __init__(self, tenant_key: str, used: int, requested: int, limit: int, retry_after: float) -> None:
        self.tenant_key = tenant_key
        self.used = used
        self.requested = requested
        self.limit = limit
        self.retry_after = retry_after
        super().__init__(f"{tenant_key} has used {used}/{limit} tokens; {requested} more would exceed it")


@dataclass(frozen=True)
class Reservation:
    """A claim on part of the window, to be settled or withdrawn."""

    row_id: int
    tenant_key: str
    reserved: int


Clock = Callable[[], float]


class TokenRateLimiter:
    """Sliding-window token accounting. Safe across concurrent async callers.

    `clock` is injectable so the window can be tested by advancing time rather
    than by sleeping through it -- a sliding-window test that sleeps 60 seconds
    does not get run, and a test that does not get run does not catch anything.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        limit: int = DEFAULT_LIMIT,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        clock: Clock = time.time,
        busy_timeout: float = DEFAULT_BUSY_TIMEOUT,
    ) -> None:
        self.database_path = str(database_path)
        self.limit = limit
        self.window_seconds = window_seconds
        self.clock = clock
        self.busy_timeout = busy_timeout
        self._initialise()

    # -- connection handling -------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout,
            # Autocommit: transaction boundaries are ours, because the whole
            # point is to control exactly where BEGIN IMMEDIATE goes. sqlite3's
            # implicit transaction management would start a deferred one instead.
            isolation_level=None,
        )
        # WAL: a writer holding the lock no longer blocks readers. Persisted in
        # the database file, but re-asserted here because a fresh file needs it.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(f"PRAGMA busy_timeout={int(self.busy_timeout * 1000)}")
        # NORMAL rather than FULL: this is a rate-limit ledger, and losing the
        # last few milliseconds of it to a power cut is not worth an fsync per
        # request. The failure mode is a tenant getting slightly more quota.
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _initialise(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(SCHEMA)
        finally:
            connection.close()

    # -- the public, async surface -------------------------------------------

    async def reserve(self, tenant_key: str, estimated_tokens: int) -> Reservation:
        """Claim `estimated_tokens` of the window, or raise `RateLimitExceeded`.

        SQLite calls go to a worker thread: they are blocking, and blocking the
        event loop in a gateway is how one slow query becomes a global stall.
        """
        return await asyncio.to_thread(self._reserve_sync, tenant_key, estimated_tokens)

    async def reconcile(self, reservation: Reservation, actual_tokens: int) -> None:
        """Settle a reservation against what the request actually cost."""
        await asyncio.to_thread(self._reconcile_sync, reservation, actual_tokens)

    async def release(self, reservation: Reservation) -> None:
        """Withdraw a reservation entirely, for a request that never happened."""
        await asyncio.to_thread(self._release_sync, reservation)

    async def used(self, tenant_key: str) -> int:
        return await asyncio.to_thread(self._used_sync, tenant_key)

    async def rows(self) -> int:
        return await asyncio.to_thread(self._rows_sync)

    # -- the blocking implementations ----------------------------------------

    def _reserve_sync(self, tenant_key: str, estimated_tokens: int) -> Reservation:
        connection = self._connect()
        try:
            # IMMEDIATE, not DEFERRED: the write lock is taken before the read
            # that justifies the write. A deferred transaction would take it at
            # the INSERT, by which point another writer may have committed under
            # our snapshot -- SQLite then refuses the upgrade with SQLITE_BUSY
            # and the request fails. See the module docstring for the numbers.
            connection.execute("BEGIN IMMEDIATE")
            try:
                now = self.clock()
                cutoff = now - self.window_seconds
                self._evict(connection, cutoff)

                used = self._sum(connection, tenant_key, cutoff)
                if used + estimated_tokens > self.limit:
                    connection.execute("ROLLBACK")
                    raise RateLimitExceeded(
                        tenant_key=tenant_key,
                        used=used,
                        requested=estimated_tokens,
                        limit=self.limit,
                        retry_after=self._retry_after(connection, tenant_key, cutoff, now),
                    )

                cursor = connection.execute(
                    "INSERT INTO usage (tenant_key, ts, tokens) VALUES (?, ?, ?)",
                    (tenant_key, now, estimated_tokens),
                )
                connection.execute("COMMIT")
                row_id = cursor.lastrowid
                assert row_id is not None
                return Reservation(row_id=row_id, tenant_key=tenant_key, reserved=estimated_tokens)
            except RateLimitExceeded:
                raise
            except Exception:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()

    def _reconcile_sync(self, reservation: Reservation, actual_tokens: int) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE usage SET tokens = ? WHERE id = ?",
                (actual_tokens, reservation.row_id),
            )
            connection.execute("COMMIT")
        finally:
            connection.close()
        if actual_tokens != reservation.reserved:
            logger.info(
                "reconciled %s: reserved %d, actual %d",
                reservation.tenant_key,
                reservation.reserved,
                actual_tokens,
            )

    def _release_sync(self, reservation: Reservation) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM usage WHERE id = ?", (reservation.row_id,))
            connection.execute("COMMIT")
        finally:
            connection.close()

    def _used_sync(self, tenant_key: str) -> int:
        connection = self._connect()
        try:
            return self._sum(connection, tenant_key, self.clock() - self.window_seconds)
        finally:
            connection.close()

    def _rows_sync(self) -> int:
        connection = self._connect()
        try:
            row = connection.execute("SELECT COUNT(*) FROM usage").fetchone()
            return int(row[0])
        finally:
            connection.close()

    # -- helpers -------------------------------------------------------------

    def _evict(self, connection: sqlite3.Connection, cutoff: float) -> None:
        """Drop rows that have fallen out of the window, for every tenant.

        Eviction is global rather than per-tenant so an idle tenant's rows do not
        accumulate forever waiting for a request that never comes.
        """
        connection.execute("DELETE FROM usage WHERE ts < ?", (cutoff,))

    def _sum(self, connection: sqlite3.Connection, tenant_key: str, cutoff: float) -> int:
        row = connection.execute(
            "SELECT COALESCE(SUM(tokens), 0) FROM usage WHERE tenant_key = ? AND ts >= ?",
            (tenant_key, cutoff),
        ).fetchone()
        return int(row[0])

    def _retry_after(
        self,
        connection: sqlite3.Connection,
        tenant_key: str,
        cutoff: float,
        now: float,
    ) -> float:
        """When the oldest row leaves the window, freeing its tokens."""
        row = connection.execute(
            "SELECT MIN(ts) FROM usage WHERE tenant_key = ? AND ts >= ?",
            (tenant_key, cutoff),
        ).fetchone()
        if row is None or row[0] is None:
            return 0.0
        return max(0.0, float(row[0]) + self.window_seconds - now)
