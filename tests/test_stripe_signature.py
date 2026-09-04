"""Signature verification is the only thing standing between a stranger on the
internet and free access to a paid group. It gets tested properly.

Standard library only - run with: python -m unittest discover tests
"""

import hashlib
import hmac
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.stripe_core import (  # noqa: E402
    SignatureError,
    payment_link_for,
    subscription_end,
    verify_signature,
)

SECRET = "whsec_test_secret"
PAYLOAD = b'{"id":"evt_1","type":"checkout.session.completed"}'
NOW = 1_700_000_000


def sign(payload: bytes, timestamp: int, secret: str = SECRET) -> str:
    signed = f"{timestamp}.".encode() + payload
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


class VerifySignature(unittest.TestCase):
    def test_accepts_a_valid_signature(self):
        verify_signature(PAYLOAD, sign(PAYLOAD, NOW), SECRET, now=NOW)

    def test_accepts_when_one_of_several_signatures_matches(self):
        """Stripe sends multiple v1 values while a secret is being rotated."""
        header = sign(PAYLOAD, NOW) + ",v1=" + "0" * 64
        verify_signature(PAYLOAD, header, SECRET, now=NOW)

    def test_rejects_a_forged_signature(self):
        header = sign(PAYLOAD, NOW, secret="not_the_secret")
        with self.assertRaises(SignatureError):
            verify_signature(PAYLOAD, header, SECRET, now=NOW)

    def test_rejects_a_tampered_payload(self):
        header = sign(PAYLOAD, NOW)
        tampered = PAYLOAD.replace(b"evt_1", b"evt_2")
        with self.assertRaises(SignatureError):
            verify_signature(tampered, header, SECRET, now=NOW)

    def test_rejects_a_replayed_event(self):
        with self.assertRaisesRegex(SignatureError, "tolerance"):
            verify_signature(PAYLOAD, sign(PAYLOAD, NOW), SECRET, now=NOW + 3600)

    def test_accepts_a_slightly_late_event(self):
        verify_signature(PAYLOAD, sign(PAYLOAD, NOW), SECRET, now=NOW + 120)

    def test_rejects_missing_header(self):
        with self.assertRaisesRegex(SignatureError, "missing"):
            verify_signature(PAYLOAD, "", SECRET)

    def test_rejects_header_without_timestamp(self):
        with self.assertRaisesRegex(SignatureError, "timestamp"):
            verify_signature(PAYLOAD, "v1=abc", SECRET)

    def test_rejects_header_without_signature(self):
        with self.assertRaisesRegex(SignatureError, "v1"):
            verify_signature(PAYLOAD, "t=1700000000", SECRET)

    def test_rejects_non_numeric_timestamp(self):
        with self.assertRaisesRegex(SignatureError, "not a number"):
            verify_signature(PAYLOAD, "t=yesterday,v1=abc", SECRET)


class PaymentLink(unittest.TestCase):
    def test_appends_reference_to_a_clean_link(self):
        self.assertEqual(
            payment_link_for("https://buy.stripe.com/abc", "tok123"),
            "https://buy.stripe.com/abc?client_reference_id=tok123",
        )

    def test_appends_reference_to_a_link_that_already_has_a_query(self):
        self.assertEqual(
            payment_link_for("https://buy.stripe.com/abc?prefilled=1", "tok123"),
            "https://buy.stripe.com/abc?prefilled=1&client_reference_id=tok123",
        )

    def test_escapes_tokens_containing_url_characters(self):
        link = payment_link_for("https://buy.stripe.com/abc", "a+b/c=d")
        self.assertIn("client_reference_id=a%2Bb%2Fc%3Dd", link)


class SubscriptionEnd(unittest.TestCase):
    """Stripe moved current_period_end onto subscription items in the
    2025-03-31.basil API version. Reading only the old location silently
    produced members with no expiry, which made the whole sweep a no-op -
    so both shapes are covered here."""

    def test_reads_period_end_from_subscription_items(self):
        """The current API shape."""
        sub = {
            "status": "active",
            "items": {"data": [{"id": "si_1", "current_period_end": 1_700_000_000}]},
        }
        self.assertEqual(subscription_end(sub), 1_700_000_000)

    def test_reads_period_end_from_the_top_level(self):
        """The pre-basil API shape."""
        self.assertEqual(subscription_end({"current_period_end": 100}), 100)

    def test_items_win_over_the_top_level(self):
        sub = {
            "current_period_end": 100,
            "items": {"data": [{"current_period_end": 500}]},
        }
        self.assertEqual(subscription_end(sub), 500)

    def test_several_items_use_the_latest_end(self):
        sub = {
            "items": {
                "data": [
                    {"current_period_end": 300},
                    {"current_period_end": 900},
                    {"current_period_end": 600},
                ]
            }
        }
        self.assertEqual(subscription_end(sub), 900)

    def test_prefers_period_end_over_cancel_at(self):
        self.assertEqual(
            subscription_end({"current_period_end": 100, "cancel_at": 200}), 100
        )

    def test_falls_back_to_cancel_at(self):
        self.assertEqual(subscription_end({"cancel_at": 200}), 200)

    def test_falls_back_to_ended_at(self):
        self.assertEqual(subscription_end({"ended_at": 250}), 250)

    def test_returns_none_when_nothing_is_set(self):
        self.assertIsNone(subscription_end({"status": "active"}))

    def test_tolerates_empty_items(self):
        self.assertIsNone(subscription_end({"items": {"data": []}}))

    def test_tolerates_items_without_period_end(self):
        sub = {"items": {"data": [{"id": "si_1"}]}, "cancel_at": 400}
        self.assertEqual(subscription_end(sub), 400)

    def test_ignores_null_and_zero_values(self):
        self.assertIsNone(subscription_end({"current_period_end": None, "cancel_at": 0}))


if __name__ == "__main__":
    unittest.main()
