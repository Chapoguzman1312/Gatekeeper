"""Applying access decisions to the actual Telegram group.

Everything that can add or remove a human being goes through here, so there is
exactly one place where report-only mode and admin protection are enforced.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any, Dict, Optional

from .config import Config
from .db import Database, now
from .entitlements import Access, Entitlement, evaluate
from .stripe_client import StripeClient
from .stripe_core import payment_link_for
from .telegram import TelegramClient, TelegramError

log = logging.getLogger(__name__)

JOB_DRIP = "drip"
JOB_GRACE_EXPIRY = "grace_expiry"


class AccessService:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        telegram: TelegramClient,
        stripe: StripeClient,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.tg = telegram
        self.stripe = stripe

    # -- checkout handoff ---------------------------------------------------

    async def start_checkout(
        self, user_id: int, username: Optional[str], first_name: Optional[str]
    ) -> str:
        """Mint a token and return the personalised payment link."""
        token = secrets.token_urlsafe(24)
        await self.db.create_pending_link(token, user_id, username, first_name)
        await self.db.record("checkout_started", user_id, f"token={token[:8]}...")
        return payment_link_for(self.cfg.stripe_payment_link, token)

    # -- granting -----------------------------------------------------------

    async def grant(
        self,
        user_id: int,
        entitlement: Entitlement,
        customer_id: Optional[str] = None,
        subscription_id: Optional[str] = None,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
    ) -> Optional[str]:
        """Record entitlement and send a single-use invite link.

        Returns the invite link, or None if we could not create or deliver one.
        """
        fields: Dict[str, Any] = {
            "status": entitlement.access.value,
            "entitled_until": entitlement.entitled_until,
            "last_synced_at": now(),
        }
        if customer_id:
            fields["stripe_customer_id"] = customer_id
        if subscription_id:
            fields["stripe_subscription_id"] = subscription_id
        if username:
            fields["username"] = username
        if first_name:
            fields["first_name"] = first_name
        await self.db.upsert_member(user_id, **fields)

        already_in = await self.tg.is_in_chat(self.cfg.telegram_chat_id, user_id)
        if already_in:
            await self.db.upsert_member(user_id, in_chat=1)
            await self.tg.send_message(
                user_id, "You're all set - your access is active and you're already in."
            )
            await self.db.record("grant_noop_already_member", user_id)
            return None

        try:
            invite = await self.tg.create_single_use_invite(
                self.cfg.telegram_chat_id, name=f"u{user_id}"
            )
        except TelegramError as exc:
            log.error("Could not create invite for %s: %s", user_id, exc)
            await self.db.record("grant_failed", user_id, str(exc))
            await self.alert_admins(
                f"Could not create an invite link for user {user_id}: {exc.description}\n"
                f"Check that the bot is an admin of the group with "
                f"'Invite users via link' enabled."
            )
            return None

        sent = await self.tg.send_message(
            user_id,
            "Payment confirmed - welcome in.\n\n"
            f"{invite}\n\n"
            "This link works once and expires in 24 hours. If it stops working, "
            "send me /link and I'll issue a fresh one.",
        )
        if sent is None:
            # They paid but have never opened a chat with the bot. Keep the
            # entitlement and let the owner know so nobody is left stranded.
            await self.db.record("grant_undeliverable", user_id, invite)
            await self.alert_admins(
                f"User {user_id} paid but has not started a chat with the bot, "
                f"so I could not send their invite. Their link: {invite}"
            )
            return invite

        await self.db.record("granted", user_id, entitlement.reason)
        await self._schedule_drip(user_id)
        return invite

    async def _schedule_drip(self, user_id: int) -> None:
        await self.db.cancel_jobs_for(user_id, JOB_DRIP)
        base = now()
        for hours, message in self.cfg.onboarding_drip:
            await self.db.schedule_job(
                JOB_DRIP,
                run_at=base + hours * 3600,
                subject_id=user_id,
                payload={"message": message},
            )

    # -- revoking -----------------------------------------------------------

    async def revoke(self, user_id: int, reason: str) -> bool:
        """Remove a member from the group. Returns True if they were removed.

        In report mode this records what would have happened and removes nobody.
        """
        if user_id in self.cfg.admin_user_ids:
            await self.db.record("revoke_skipped_admin", user_id, reason)
            return False

        if await self.tg.is_protected(self.cfg.telegram_chat_id, user_id):
            await self.db.record("revoke_skipped_chat_admin", user_id, reason)
            return False

        present = await self.tg.is_in_chat(self.cfg.telegram_chat_id, user_id)
        if present is False:
            await self.db.upsert_member(
                user_id, in_chat=0, status=Access.REVOKED.value
            )
            await self.db.record("revoke_noop_not_member", user_id, reason)
            return False

        if not self.cfg.enforcing:
            await self.db.record("would_remove", user_id, reason, simulated=True)
            log.info("[report mode] would remove %s (%s)", user_id, reason)
            return False

        try:
            await self.tg.remove_member(self.cfg.telegram_chat_id, user_id)
        except TelegramError as exc:
            log.error("Could not remove %s: %s", user_id, exc)
            await self.db.record("remove_failed", user_id, str(exc))
            await self.alert_admins(
                f"Could not remove user {user_id}: {exc.description}\n"
                f"The bot probably lacks the 'Ban users' permission in the group."
            )
            return False

        await self.db.upsert_member(
            user_id,
            in_chat=0,
            status=Access.REVOKED.value,
            removed_at=now(),
        )
        await self.db.cancel_jobs_for(user_id)
        await self.db.record("removed", user_id, reason)
        await self.tg.send_message(
            user_id,
            "Your subscription has ended, so I've removed you from the group.\n\n"
            "No hard feelings - send me /start whenever you want to come back and "
            "I'll get you a fresh link.",
        )
        return True

    async def warn_grace(self, user_id: int, deadline: int) -> None:
        from datetime import datetime, timezone

        when = datetime.fromtimestamp(deadline, tz=timezone.utc).strftime("%d %B")
        await self.tg.send_message(
            user_id,
            "Heads up - your last payment didn't go through.\n\n"
            f"You'll keep access until {when} while it retries. Updating your card "
            "in the billing portal is usually all it takes.",
        )
        await self.db.record("grace_warning_sent", user_id)

    # -- syncing ------------------------------------------------------------

    async def sync_member(self, member: Dict[str, Any]) -> Entitlement:
        """Re-read this member's real state from Stripe and persist it."""
        user_id = member["telegram_user_id"]
        subscription: Optional[Dict[str, Any]] = None

        try:
            if member.get("stripe_subscription_id"):
                subscription = await self.stripe.get_subscription(
                    member["stripe_subscription_id"]
                )
            elif member.get("stripe_customer_id"):
                subscription = await self.stripe.best_subscription_for_customer(
                    member["stripe_customer_id"]
                )
        except Exception as exc:  # network, permissions, deleted objects
            log.warning("Stripe sync failed for %s: %s", user_id, exc)
            return Entitlement(Access.UNKNOWN, member.get("entitled_until"), str(exc))

        entitlement = evaluate(subscription, self.cfg.grace_period_days)

        fields: Dict[str, Any] = {
            "status": entitlement.access.value,
            "entitled_until": entitlement.entitled_until,
            "last_synced_at": now(),
        }
        if subscription:
            fields["stripe_subscription_id"] = subscription.get("id")
            customer = subscription.get("customer")
            if isinstance(customer, str):
                fields["stripe_customer_id"] = customer
        await self.db.upsert_member(user_id, **fields)
        return entitlement

    async def apply(self, user_id: int, entitlement: Entitlement) -> str:
        """Act on a decision. Returns a short word describing what was done."""
        if entitlement.access is Access.UNKNOWN:
            return "skipped"

        if entitlement.requires_removal:
            removed = await self.revoke(user_id, entitlement.reason)
            return "removed" if removed else "flagged"

        if entitlement.access is Access.GRACE and entitlement.entitled_until:
            await self.db.cancel_jobs_for(user_id, JOB_GRACE_EXPIRY)
            await self.db.schedule_job(
                JOB_GRACE_EXPIRY,
                run_at=entitlement.entitled_until,
                subject_id=user_id,
                payload={"reason": entitlement.reason},
            )
            return "grace"

        return "kept"

    # -- admin notifications ------------------------------------------------

    async def alert_admins(self, text: str) -> None:
        for admin_id in self.cfg.admin_user_ids:
            await self.tg.send_message(admin_id, f"[gatekeeper] {text}")
