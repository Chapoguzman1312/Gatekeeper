"""The rule that decides who is allowed in the group.

Pure functions, no I/O. This is the part that is most expensive to get wrong -
removing a paying member is much worse than briefly tolerating a lapsed one -
so it lives on its own and is unit tested directly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from .stripe_core import (
    ACTIVE_STATUSES,
    DEAD_STATUSES,
    GRACE_STATUSES,
    subscription_end,
)


class Access(str, Enum):
    ENTITLED = "entitled"
    """Paid up. Let them in, keep them in."""

    GRACE = "grace"
    """Payment failed and Stripe is still retrying. Warn, do not remove yet."""

    REVOKED = "revoked"
    """Over. Remove them."""

    UNKNOWN = "unknown"
    """We could not determine anything. Do nothing at all."""


@dataclass(frozen=True)
class Entitlement:
    access: Access
    entitled_until: Optional[int]
    reason: str

    @property
    def allows_entry(self) -> bool:
        return self.access in (Access.ENTITLED, Access.GRACE)

    @property
    def requires_removal(self) -> bool:
        return self.access is Access.REVOKED


def evaluate(
    subscription: Optional[Dict[str, Any]],
    grace_period_days: int = 3,
    now: Optional[int] = None,
) -> Entitlement:
    """Turn a Stripe subscription object into an access decision."""
    current = int(time.time()) if now is None else now
    grace_seconds = max(0, grace_period_days) * 24 * 3600

    if subscription is None:
        return Entitlement(Access.REVOKED, None, "no subscription on file")

    status = subscription.get("status")
    period_end = subscription_end(subscription)

    if status in ACTIVE_STATUSES:
        return Entitlement(Access.ENTITLED, period_end, f"subscription {status}")

    if status in GRACE_STATUSES:
        # Grace runs from the end of the period they actually paid for, not
        # from now, so a customer whose card failed three weeks ago does not
        # earn a fresh grace window every time the sweep runs.
        deadline = (period_end or current) + grace_seconds
        if current < deadline:
            return Entitlement(
                Access.GRACE, deadline, f"payment {status}, in grace until deadline"
            )
        return Entitlement(Access.REVOKED, deadline, f"payment {status}, grace expired")

    if status in DEAD_STATUSES:
        # Cancelled but already paid through a future date: they keep the time
        # they bought.
        if period_end and period_end > current:
            return Entitlement(
                Access.ENTITLED, period_end, f"{status}, paid through period end"
            )
        return Entitlement(Access.REVOKED, period_end, f"subscription {status}")

    if status == "incomplete":
        # First payment never landed. Not a member yet; nothing to remove.
        return Entitlement(Access.REVOKED, None, "initial payment incomplete")

    return Entitlement(Access.UNKNOWN, period_end, f"unrecognised status {status!r}")


def evaluate_member(
    member: Dict[str, Any],
    grace_period_days: int = 3,
    now: Optional[int] = None,
) -> Entitlement:
    """Decide from a stored member row, without calling Stripe.

    Used by the sweep for members whose cached state is recent enough to trust.
    """
    current = int(time.time()) if now is None else now
    status = member.get("status") or "unknown"
    entitled_until = member.get("entitled_until")

    if status == Access.UNKNOWN.value:
        return Entitlement(Access.UNKNOWN, entitled_until, "never synced with Stripe")

    if status == Access.REVOKED.value:
        return Entitlement(Access.REVOKED, entitled_until, "revoked at last sync")

    if entitled_until is None:
        return Entitlement(Access.UNKNOWN, None, "no expiry recorded")

    if current < entitled_until:
        return Entitlement(Access(status), entitled_until, f"{status} until expiry")

    # The cached window has run out. If they were merely in grace, that grace
    # has now expired; if they were entitled, the renewal has not been seen and
    # the sweep should re-check against Stripe rather than assume.
    if status == Access.GRACE.value:
        return Entitlement(Access.REVOKED, entitled_until, "grace period expired")
    return Entitlement(Access.UNKNOWN, entitled_until, "entitlement expired, needs sync")


def needs_stripe_refresh(
    member: Dict[str, Any], max_age_seconds: int = 12 * 3600, now: Optional[int] = None
) -> bool:
    """True when the cached Stripe state is too old to act on."""
    current = int(time.time()) if now is None else now
    last = member.get("last_synced_at")
    if not last:
        return True
    return (current - last) > max_age_seconds
