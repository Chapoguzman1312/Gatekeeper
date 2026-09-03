"""HTTP surface: Stripe events in, Telegram updates in, health check out.

One aiohttp application serves all three, because a free-tier host gives you
one web process and this needs to fit inside it.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from aiohttp import web

from .access import AccessService
from .config import Config
from .db import Database
from .entitlements import evaluate
from .handlers import UpdateHandler
from .stripe_client import StripeClient
from .stripe_core import SignatureError, verify_signature

log = logging.getLogger(__name__)

# Stripe events that can change somebody's access.
RELEVANT_EVENTS = {
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "customer.subscription.paused",
    "customer.subscription.resumed",
    "invoice.paid",
    "invoice.payment_succeeded",
    "invoice.payment_failed",
}


class WebhookRoutes:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        stripe: StripeClient,
        access: AccessService,
        handler: UpdateHandler,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.stripe = stripe
        self.access = access
        self.handler = handler

    def register(self, app: web.Application) -> None:
        app.router.add_get("/", self.health)
        app.router.add_get("/health", self.health)
        app.router.add_post("/stripe", self.stripe_webhook)
        app.router.add_post(self.cfg.telegram_webhook_path, self.telegram_webhook)

    # -- health -------------------------------------------------------------

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"ok": True, "mode": self.cfg.enforcement_mode, "service": "gatekeeper"}
        )

    # -- telegram -----------------------------------------------------------

    async def telegram_webhook(self, request: web.Request) -> web.Response:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != self.cfg.telegram_webhook_secret:
            log.warning("Rejected a Telegram webhook with a bad secret token")
            return web.Response(status=403, text="forbidden")

        try:
            update = await request.json()
        except json.JSONDecodeError:
            return web.Response(status=400, text="bad json")

        # Answer immediately; Telegram retries anything slow.
        await self.handler.handle(update)
        return web.Response(text="ok")

    # -- stripe -------------------------------------------------------------

    async def stripe_webhook(self, request: web.Request) -> web.Response:
        payload = await request.read()
        signature = request.headers.get("Stripe-Signature", "")

        try:
            verify_signature(payload, signature, self.cfg.stripe_webhook_secret)
        except SignatureError as exc:
            log.warning("Rejected Stripe webhook: %s", exc)
            return web.Response(status=400, text=str(exc))

        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return web.Response(status=400, text="bad json")

        event_id = event.get("id", "")
        event_type = event.get("type", "")

        if event_type not in RELEVANT_EVENTS:
            return web.Response(text="ignored")

        # Stripe redelivers on any non-2xx, and sometimes just because. Doing
        # the work twice must be harmless, so we simply refuse to do it twice.
        first_time = await self.db.mark_event_seen(event_id, event_type)
        if not first_time:
            log.info("Ignoring redelivered event %s", event_id)
            return web.Response(text="duplicate")

        try:
            await self._dispatch(event_type, event)
        except Exception:
            log.exception("Failed handling Stripe event %s (%s)", event_id, event_type)
            # 500 makes Stripe retry, which is what we want for a transient
            # failure - but the event id is already recorded, so clear it so
            # the retry is not treated as a duplicate.
            await self.db.conn.execute(
                "DELETE FROM seen_events WHERE stripe_event_id = ?", (event_id,)
            )
            await self.db.conn.commit()
            return web.Response(status=500, text="retry please")

        return web.Response(text="ok")

    async def _dispatch(self, event_type: str, event: Dict[str, Any]) -> None:
        obj = event.get("data", {}).get("object", {})

        if event_type == "checkout.session.completed":
            await self._on_checkout_completed(obj)
        elif event_type.startswith("customer.subscription."):
            await self._on_subscription_event(obj)
        elif event_type.startswith("invoice."):
            await self._on_invoice_event(obj)

    async def _on_checkout_completed(self, session: Dict[str, Any]) -> None:
        """A payment landed. Match it to the Telegram user who started it."""
        token = session.get("client_reference_id")
        customer_id = _as_id(session.get("customer"))
        subscription_id = _as_id(session.get("subscription"))

        if not token:
            await self._orphan(session, "checkout had no client_reference_id")
            return

        pending = await self.db.consume_pending_link(token)
        if pending is None:
            await self._orphan(session, f"unknown token {token[:8]}...")
            return

        user_id = pending["telegram_user_id"]

        subscription: Optional[Dict[str, Any]] = None
        if subscription_id:
            try:
                subscription = await self.stripe.get_subscription(subscription_id)
            except Exception as exc:
                log.warning("Could not read subscription %s: %s", subscription_id, exc)
        elif customer_id:
            subscription = await self.stripe.best_subscription_for_customer(customer_id)

        if subscription is None:
            # A one-off payment rather than a subscription: honour it for a
            # month and let the sweep sort out the rest.
            from .db import now
            from .entitlements import Access, Entitlement

            entitlement = Entitlement(
                Access.ENTITLED, now() + 31 * 24 * 3600, "one-off payment"
            )
        else:
            entitlement = evaluate(subscription, self.cfg.grace_period_days)

        await self.access.grant(
            user_id,
            entitlement,
            customer_id=customer_id,
            subscription_id=subscription_id,
            username=pending.get("username"),
            first_name=pending.get("first_name"),
        )

    async def _on_subscription_event(self, subscription: Dict[str, Any]) -> None:
        subscription_id = subscription.get("id")
        customer_id = _as_id(subscription.get("customer"))

        member = None
        if subscription_id:
            member = await self.db.get_member_by_subscription(subscription_id)
        if member is None and customer_id:
            member = await self.db.get_member_by_customer(customer_id)

        if member is None:
            log.info(
                "Subscription event for %s with no member on file", subscription_id
            )
            return

        from .db import now as _now

        entitlement = evaluate(subscription, self.cfg.grace_period_days)
        user_id = member["telegram_user_id"]

        await self.db.upsert_member(
            user_id,
            status=entitlement.access.value,
            entitled_until=entitlement.entitled_until,
            stripe_subscription_id=subscription_id,
            stripe_customer_id=customer_id or member.get("stripe_customer_id"),
            last_synced_at=_now(),
        )

        outcome = await self.access.apply(user_id, entitlement)
        log.info(
            "Subscription %s -> %s for user %s (%s)",
            subscription.get("status"),
            outcome,
            user_id,
            entitlement.reason,
        )

        if entitlement.access.value == "grace" and entitlement.entitled_until:
            await self.access.warn_grace(user_id, entitlement.entitled_until)

    async def _on_invoice_event(self, invoice: Dict[str, Any]) -> None:
        subscription_id = _as_id(invoice.get("subscription"))
        customer_id = _as_id(invoice.get("customer"))

        if not subscription_id and not customer_id:
            return

        subscription = None
        try:
            if subscription_id:
                subscription = await self.stripe.get_subscription(subscription_id)
            elif customer_id:
                subscription = await self.stripe.best_subscription_for_customer(
                    customer_id
                )
        except Exception as exc:
            log.warning("Could not resolve subscription for invoice: %s", exc)
            return

        if subscription:
            await self._on_subscription_event(subscription)

    async def _orphan(self, session: Dict[str, Any], reason: str) -> None:
        """A payment we cannot match to a Telegram user. Never silently drop it."""
        email = (session.get("customer_details") or {}).get("email") or "unknown email"
        customer_id = _as_id(session.get("customer")) or "unknown customer"
        log.warning("Unmatched payment (%s): %s / %s", reason, email, customer_id)
        await self.db.record("unmatched_payment", None, f"{reason}; {email}")
        await self.access.alert_admins(
            f"A payment came in that I could not match to a Telegram user "
            f"({reason}).\n\n"
            f"Customer: {customer_id}\nEmail: {email}\n\n"
            f"Ask them to send /start to the bot, then use "
            f"/grant <their user id> to let them in."
        )


def _as_id(value: Any) -> Optional[str]:
    """Stripe fields are either an id string or an expanded object."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        identifier = value.get("id")
        return identifier if isinstance(identifier, str) else None
    return None
