"""The Stripe HTTP client.

Only the read calls this service needs. Signature verification and the meaning
of subscription statuses live in stripe_core, which has no dependencies.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import aiohttp

from .stripe_core import (  # re-exported for convenience
    ACTIVE_STATUSES,
    DEAD_STATUSES,
    GRACE_STATUSES,
    SignatureError,
    payment_link_for,
    subscription_end,
    verify_signature,
)

__all__ = [
    "ACTIVE_STATUSES",
    "DEAD_STATUSES",
    "GRACE_STATUSES",
    "SignatureError",
    "StripeClient",
    "StripeError",
    "payment_link_for",
    "subscription_end",
    "verify_signature",
]

log = logging.getLogger(__name__)

API_ROOT = "https://api.stripe.com/v1"


class StripeError(RuntimeError):
    pass


class StripeClient:
    def __init__(self, api_key: str, session: aiohttp.ClientSession) -> None:
        self._api_key = api_key
        self._session = session

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        url = f"{API_ROOT}{path}"
        async with self._session.get(
            url, headers=headers, params=params, timeout=30
        ) as resp:
            body = await resp.json()
            if resp.status >= 400:
                message = body.get("error", {}).get("message", "unknown error")
                raise StripeError(f"GET {path} -> {resp.status}: {message}")
            return body

    async def get_subscription(self, subscription_id: str) -> Dict[str, Any]:
        return await self._get(f"/subscriptions/{quote(subscription_id, safe='')}")

    async def get_customer(self, customer_id: str) -> Dict[str, Any]:
        return await self._get(f"/customers/{quote(customer_id, safe='')}")

    async def list_subscriptions_for_customer(
        self, customer_id: str
    ) -> List[Dict[str, Any]]:
        body = await self._get(
            "/subscriptions",
            params={"customer": customer_id, "status": "all", "limit": 20},
        )
        return body.get("data", [])

    async def best_subscription_for_customer(
        self, customer_id: str
    ) -> Optional[Dict[str, Any]]:
        """The subscription that decides access.

        A customer can hold several. Access is granted if *any* of them is
        live, so prefer an active one, then one in grace, then the most recent.
        """
        subscriptions = await self.list_subscriptions_for_customer(customer_id)
        if not subscriptions:
            return None
        for group in (ACTIVE_STATUSES, GRACE_STATUSES):
            for subscription in subscriptions:
                if subscription.get("status") in group:
                    return subscription
        return max(subscriptions, key=lambda s: s.get("created", 0))
