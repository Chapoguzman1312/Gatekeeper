"""Background loops: the reconciliation sweep and the job queue.

Both run as asyncio tasks inside the web process, which is the right shape for
a free-tier host where you get one process and no cron.
"""

from __future__ import annotations

import asyncio
import logging

from .access import JOB_DRIP, JOB_GRACE_EXPIRY, AccessService
from .config import Config
from .db import Database
from .entitlements import evaluate_member
from .handlers import UpdateHandler
from .telegram import TelegramClient

log = logging.getLogger(__name__)

JOB_POLL_SECONDS = 30
MAX_JOB_ATTEMPTS = 5


class Scheduler:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        telegram: TelegramClient,
        access: AccessService,
        handler: UpdateHandler,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.tg = telegram
        self.access = access
        self.handler = handler
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._sweep_loop(), name="sweep"),
            asyncio.create_task(self._job_loop(), name="jobs"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []

    # -- reconciliation -----------------------------------------------------

    async def _sweep_loop(self) -> None:
        interval = self.cfg.reconcile_interval_minutes * 60
        # A short delay so the first sweep does not race startup.
        await asyncio.sleep(20)
        while True:
            try:
                summary = await self.handler.run_sweep()
                log.info("Sweep: %s", summary.replace("\n", " "))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Sweep failed; will try again next interval")
            await asyncio.sleep(interval)

    # -- job queue ----------------------------------------------------------

    async def _job_loop(self) -> None:
        while True:
            try:
                jobs = await self.db.due_jobs()
                for job in jobs:
                    await self._run_job(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Job loop iteration failed")
            await asyncio.sleep(JOB_POLL_SECONDS)

    async def _run_job(self, job: dict) -> None:
        job_id = job["id"]
        kind = job["kind"]
        user_id = job["subject_id"]

        if job["attempts"] >= MAX_JOB_ATTEMPTS:
            log.error("Giving up on job %s (%s) after %s attempts", job_id, kind, job["attempts"])
            await self.db.complete_job(job_id)
            return

        try:
            if kind == JOB_DRIP:
                await self._run_drip(user_id, job["payload"].get("message", ""))
            elif kind == JOB_GRACE_EXPIRY:
                await self._run_grace_expiry(user_id, job["payload"].get("reason", ""))
            else:
                log.warning("Unknown job kind %r, discarding", kind)
            await self.db.complete_job(job_id)
        except Exception as exc:
            log.warning("Job %s (%s) failed: %s", job_id, kind, exc)
            await self.db.fail_job(job_id, str(exc))

    async def _run_drip(self, user_id: int, message: str) -> None:
        if not message:
            return
        member = await self.db.get_member(user_id)
        # Don't keep onboarding someone who has left or lapsed.
        if member is None or not member.get("in_chat"):
            return
        entitlement = evaluate_member(member, self.cfg.grace_period_days)
        if not entitlement.allows_entry:
            return
        await self.tg.send_message(user_id, message)
        await self.db.record("drip_sent", user_id, message[:60])

    async def _run_grace_expiry(self, user_id: int, reason: str) -> None:
        """The grace window we scheduled has run out. Re-check, then act."""
        member = await self.db.get_member(user_id)
        if member is None:
            return
        # Always re-read Stripe here: the card may well have gone through in
        # the meantime, and removing someone who has paid is the one mistake
        # that loses the client.
        entitlement = await self.access.sync_member(member)
        outcome = await self.access.apply(user_id, entitlement)
        log.info("Grace expiry for %s -> %s (%s)", user_id, outcome, entitlement.reason)
