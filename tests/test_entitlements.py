"""The access rule. Removing a paying member is the expensive mistake, so the
bias in these tests is toward proving we never do that.

Standard library only - run with: python -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.entitlements import (  # noqa: E402
    Access,
    evaluate,
    evaluate_member,
    needs_stripe_refresh,
)

NOW = 1_700_000_000
DAY = 24 * 3600


def sub(status, period_end=None, **extra):
    payload = {"status": status, "id": "sub_1"}
    if period_end is not None:
        payload["current_period_end"] = period_end
    payload.update(extra)
    return payload


class LiveSubscriptions(unittest.TestCase):
    def test_active_subscription_is_entitled(self):
        result = evaluate(sub("active", NOW + 20 * DAY), now=NOW)
        self.assertIs(result.access, Access.ENTITLED)
        self.assertEqual(result.entitled_until, NOW + 20 * DAY)
        self.assertTrue(result.allows_entry)
        self.assertFalse(result.requires_removal)

    def test_trialing_subscription_is_entitled(self):
        result = evaluate(sub("trialing", NOW + 7 * DAY), now=NOW)
        self.assertIs(result.access, Access.ENTITLED)


class FailingPayments(unittest.TestCase):
    def test_past_due_stays_in_during_grace(self):
        result = evaluate(sub("past_due", NOW - DAY), grace_period_days=3, now=NOW)
        self.assertIs(result.access, Access.GRACE)
        self.assertTrue(result.allows_entry)
        self.assertFalse(result.requires_removal)

    def test_past_due_is_revoked_once_grace_expires(self):
        result = evaluate(sub("past_due", NOW - 5 * DAY), grace_period_days=3, now=NOW)
        self.assertIs(result.access, Access.REVOKED)
        self.assertTrue(result.requires_removal)

    def test_grace_runs_from_period_end_not_from_now(self):
        """A card that failed three weeks ago must not earn a fresh window
        every time the sweep happens to run."""
        result = evaluate(sub("past_due", NOW - 21 * DAY), grace_period_days=3, now=NOW)
        self.assertIs(result.access, Access.REVOKED)

    def test_unpaid_is_treated_like_past_due(self):
        result = evaluate(sub("unpaid", NOW - DAY), grace_period_days=3, now=NOW)
        self.assertIs(result.access, Access.GRACE)

    def test_zero_grace_revokes_immediately(self):
        result = evaluate(sub("past_due", NOW - 60), grace_period_days=0, now=NOW)
        self.assertIs(result.access, Access.REVOKED)

    def test_longer_grace_keeps_them_in(self):
        result = evaluate(sub("past_due", NOW - 5 * DAY), grace_period_days=14, now=NOW)
        self.assertIs(result.access, Access.GRACE)


class Cancellations(unittest.TestCase):
    def test_cancelled_but_paid_through_keeps_access(self):
        """They cancelled on the 3rd but paid to the 28th. They keep the time
        they bought - taking it away is how you earn a chargeback."""
        result = evaluate(sub("canceled", NOW + 10 * DAY), now=NOW)
        self.assertIs(result.access, Access.ENTITLED)
        self.assertEqual(result.entitled_until, NOW + 10 * DAY)

    def test_cancelled_and_expired_is_revoked(self):
        result = evaluate(sub("canceled", NOW - DAY), now=NOW)
        self.assertIs(result.access, Access.REVOKED)

    def test_paused_subscription_is_revoked(self):
        result = evaluate(sub("paused", NOW - DAY), now=NOW)
        self.assertIs(result.access, Access.REVOKED)

    def test_incomplete_never_grants_access(self):
        result = evaluate(sub("incomplete"), now=NOW)
        self.assertIs(result.access, Access.REVOKED)

    def test_no_subscription_is_revoked(self):
        self.assertIs(evaluate(None, now=NOW).access, Access.REVOKED)


class SafetyValve(unittest.TestCase):
    def test_unrecognised_status_does_nothing(self):
        """A status Stripe adds in 2029 must never cause a removal."""
        result = evaluate(sub("some_new_status", NOW + DAY), now=NOW)
        self.assertIs(result.access, Access.UNKNOWN)
        self.assertFalse(result.requires_removal)
        self.assertFalse(result.allows_entry)


class CachedEvaluation(unittest.TestCase):
    def test_cached_entitlement_still_valid(self):
        member = {"status": "entitled", "entitled_until": NOW + DAY}
        self.assertIs(evaluate_member(member, now=NOW).access, Access.ENTITLED)

    def test_expired_cache_asks_for_a_resync_rather_than_removing(self):
        member = {"status": "entitled", "entitled_until": NOW - DAY}
        result = evaluate_member(member, now=NOW)
        self.assertIs(result.access, Access.UNKNOWN)
        self.assertFalse(result.requires_removal)

    def test_expired_grace_in_cache_is_a_removal(self):
        member = {"status": "grace", "entitled_until": NOW - DAY}
        self.assertIs(evaluate_member(member, now=NOW).access, Access.REVOKED)

    def test_member_never_synced_is_unknown(self):
        member = {"status": "unknown", "entitled_until": None}
        self.assertIs(evaluate_member(member, now=NOW).access, Access.UNKNOWN)

    def test_member_with_no_expiry_is_unknown(self):
        member = {"status": "entitled", "entitled_until": None}
        self.assertIs(evaluate_member(member, now=NOW).access, Access.UNKNOWN)

    def test_revoked_stays_revoked(self):
        member = {"status": "revoked", "entitled_until": NOW - DAY}
        self.assertIs(evaluate_member(member, now=NOW).access, Access.REVOKED)


class RefreshPolicy(unittest.TestCase):
    def test_never_synced_needs_refresh(self):
        self.assertTrue(needs_stripe_refresh({"last_synced_at": None}, now=NOW))

    def test_recently_synced_does_not_need_refresh(self):
        self.assertFalse(needs_stripe_refresh({"last_synced_at": NOW - 600}, now=NOW))

    def test_stale_sync_needs_refresh(self):
        self.assertTrue(needs_stripe_refresh({"last_synced_at": NOW - 2 * DAY}, now=NOW))


if __name__ == "__main__":
    unittest.main()
