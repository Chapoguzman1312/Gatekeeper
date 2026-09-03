"""Stripe domain logic with no I/O and no dependencies.

Deliberately separate from stripe_client.py so the two things most worth
testing - signature verification and the meaning of a subscription status -
can be exercised with nothing installed but Python itself.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

DEFAULT_TOLERANCE_SECONDS = 300

# Statuses that mean the customer is currently paying us.
ACTIVE_STATUSES = {"active", "trialing"}
# Payment has failed but Stripe is still retrying - this is what the grace
# period exists for.
GRACE_STATUSES = {"past_due", "unpaid"}
# Over. Nothing more is coming.
DEAD_STATUSES = {"canceled", "incomplete_expired", "paused"}


class SignatureError(ValueError):
    """The webhook payload did not come from Stripe, or arrived too late."""


def verify_signature(
    payload: bytes,
    signature_header: str,
    secret: str,
    tolerance: int = DEFAULT_TOLERANCE_SECONDS,
    now: Optional[int] = None,
) -> None:
    """Raise SignatureError unless the payload is a genuine, fresh Stripe event.

    The header looks like: t=1690000000,v1=abc...,v1=def...
    The signed payload is "{timestamp}.{raw body}", HMAC-SHA256 with the
    endpoint's signing secret. More than one v1 appears while a secret is
    being rotated, so any match is enough.
    """
    if not signature_header:
        raise SignatureError("missing Stripe-Signature header")

    timestamp: Optional[str] = None
    candidates: List[str] = []
    for part in signature_header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            candidates.append(value)

    if timestamp is None:
        raise SignatureError("Stripe-Signature header has no timestamp")
    if not candidates:
        raise SignatureError("Stripe-Signature header has no v1 signature")

    try:
        sent_at = int(timestamp)
    except ValueError as exc:
        raise SignatureError("Stripe-Signature timestamp is not a number") from exc

    current = int(time.time()) if now is None else now
    if tolerance and abs(current - sent_at) > tolerance:
        raise SignatureError(
            f"event timestamp is outside the {tolerance}s tolerance - "
            f"replayed, or this server's clock is wrong"
        )

    signed = f"{timestamp}.".encode() + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()

    if not any(hmac.compare_digest(expected, candidate) for candidate in candidates):
        raise SignatureError("no signature in the header matched")


def payment_link_for(base_link: str, token: str) -> str:
    """Attach our tracking token to the owner's Stripe Payment Link.

    Stripe passes client_reference_id straight through to the completed
    checkout session, which is how a payment gets matched back to the Telegram
    user who started it.
    """
    separator = "&" if "?" in base_link else "?"
    return f"{base_link}{separator}client_reference_id={quote(token, safe='')}"


def subscription_end(subscription: Dict[str, Any]) -> Optional[int]:
    """When the paid period runs out."""
    for field in ("current_period_end", "cancel_at", "ended_at"):
        value = subscription.get(field)
        if isinstance(value, int) and value > 0:
            return value
    return None
