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
    def test_prefers_current_period_end(self):
        self.assertEqual(
            subscription_end({"current_period_end": 100, "cancel_at": 200}), 100
        )

    def test_falls_back_to_cancel_at(self):
        self.assertEqual(subscription_end({"cancel_at": 200}), 200)

    def test_returns_none_when_nothing_is_set(self):
        self.assertIsNone(subscription_end({"status": "active"}))

    def test_ignores_null_and_zero_values(self):
        self.assertIsNone(subscription_end({"current_period_end": None, "cancel_at": 0}))


if __name__ == "__main__":
    unittest.main()
