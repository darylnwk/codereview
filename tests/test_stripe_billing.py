"""Stripe TEST-mode card on file, trial, anchor, cap, and webhooks.

HTTP to Stripe is mocked. Behavioural cases must fail if the production code
that implements them is removed.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from urllib.parse import parse_qs

from review_grade.api.http import bind, receive_stripe_webhook
from review_grade.api.plans import (
    DEFAULT_MONTHLY_CAP_CENTS,
    DEVELOPER,
    charge_for_review_cents,
    trial_ends_at,
)
from review_grade.api.store import Store
from review_grade.api.stripe_billing import (
    ENV_CANCEL_URL,
    ENV_PRICE_DEVELOPER,
    ENV_PRICE_FLEET,
    ENV_SECRET_KEY,
    ENV_SUCCESS_URL,
    ENV_WEBHOOK_SECRET,
    StripeEnv,
    apply_card_on_file,
    create_card_setup_checkout,
    handle_stripe_webhook,
    load_stripe_env,
    sign_payload,
    verify_stripe_signature,
)
from review_grade.api.stripe_http import StripeClient, StripeError


_WEBHOOK_SECRET = "whsec_test_secret_for_unit_tests_only"
_PRICES = {"developer": "price_dev_test", "fleet": "price_fleet_test"}


class FakeStripeTransport:
    """Records form posts and returns canned Stripe API shapes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, list[str]]]] = []
        self.customers = 0
        self.sessions = 0
        self.subscriptions = 0
        self.setup_intents: dict[str, dict] = {
            "seti_test": {
                "id": "seti_test",
                "object": "setup_intent",
                "payment_method": "pm_test_card",
                "status": "succeeded",
            }
        }

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        fields: dict[str, list[str]] = {}
        if body:
            fields = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        self.calls.append((method, url, fields))
        path = url.split("api.stripe.com", 1)[-1]
        if method == "POST" and path == "/v1/customers":
            self.customers += 1
            return 200, json.dumps(
                {
                    "id": f"cus_test_{self.customers}",
                    "object": "customer",
                    "email": fields.get("email", [""])[0],
                }
            ).encode()
        if method == "POST" and path == "/v1/checkout/sessions":
            self.sessions += 1
            assert fields.get("mode") == ["setup"], fields
            return 200, json.dumps(
                {
                    "id": f"cs_test_{self.sessions}",
                    "object": "checkout.session",
                    "mode": "setup",
                    "url": f"https://checkout.stripe.com/c/pay/cs_test_{self.sessions}",
                    "customer": fields.get("customer", [None])[0],
                }
            ).encode()
        if method == "GET" and path.startswith("/v1/setup_intents/"):
            intent_id = path.rsplit("/", 1)[-1]
            payload = self.setup_intents.get(intent_id)
            if payload is None:
                return 404, json.dumps({"error": {"message": "missing"}}).encode()
            return 200, json.dumps(payload).encode()
        if method == "POST" and path == "/v1/subscriptions":
            self.subscriptions += 1
            trial_end = int(fields["trial_end"][0])
            return 200, json.dumps(
                {
                    "id": f"sub_test_{self.subscriptions}",
                    "object": "subscription",
                    "status": "trialing",
                    "trial_end": trial_end,
                    "customer": fields.get("customer", [None])[0],
                }
            ).encode()
        return 500, json.dumps({"error": {"message": f"unexpected {method} {path}"}}).encode()


def _env() -> StripeEnv:
    return StripeEnv(
        secret_key="sk_test_dummy",
        webhook_secret=_WEBHOOK_SECRET,
        price_developer=_PRICES["developer"],
        price_fleet=_PRICES["fleet"],
        success_url="https://example.test/success?session_id={CHECKOUT_SESSION_ID}",
        cancel_url="https://example.test/cancel",
    )


def _client(transport: FakeStripeTransport) -> StripeClient:
    return StripeClient("sk_test_dummy", transport=transport)


def _checkout_completed_event(
    *,
    event_id: str,
    user_id: int,
    plan: str = "developer",
    customer: str = "cus_test_1",
    setup_intent: str = "seti_test",
) -> bytes:
    body = {
        "id": event_id,
        "object": "event",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_1",
                "object": "checkout.session",
                "mode": "setup",
                "customer": customer,
                "setup_intent": setup_intent,
                "client_reference_id": str(user_id),
                "metadata": {"user_id": str(user_id), "plan": plan},
            }
        },
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


class TrialClampTests(unittest.TestCase):
    def test_month_end_clamp_includes_leap_years(self) -> None:
        # Non-leap February.
        self.assertEqual(
            trial_ends_at(datetime(2026, 1, 31, 15, 0, tzinfo=UTC)),
            datetime(2026, 2, 28, 0, 0, tzinfo=UTC),
        )
        # Leap February (mutant that always uses 28 would survive without this).
        self.assertEqual(
            trial_ends_at(datetime(2024, 1, 31, 10, 0, tzinfo=UTC)),
            datetime(2024, 2, 29, 0, 0, tzinfo=UTC),
        )
        self.assertEqual(
            trial_ends_at(datetime(2026, 3, 31, 8, 0, tzinfo=UTC)),
            datetime(2026, 4, 30, 0, 0, tzinfo=UTC),
        )


class CardOnFileAnchorAndCapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite")
        self.store = Store(self.tmp.name)
        self.user_id = self.store.add_user("card@example.com", 0)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.close()

    def test_card_add_sets_billing_anchor_replacing_day_one(self) -> None:
        before = self.store.billing(self.user_id)
        self.assertEqual(before.billing_anchor_day, 1)
        card_at = datetime(2026, 10, 21, 15, 30, tzinfo=UTC)
        result = apply_card_on_file(
            self.store,
            self.user_id,
            card_at,
            stripe_customer_id="cus_anchor",
        )
        self.assertEqual(result["billing_anchor_day"], 21)
        after = self.store.billing(self.user_id, now=card_at)
        self.assertEqual(after.billing_anchor_day, 21)
        self.assertEqual(
            after.trial_ends_at,
            "2026-11-21T00:00:00.000000Z",
        )
        # Jan 31 card → anchor 31 (not left at day 1).
        other = self.store.add_user("jan31@example.com", 0)
        apply_card_on_file(
            self.store,
            other,
            datetime(2026, 1, 31, 12, 0, tzinfo=UTC),
            stripe_customer_id="cus_jan31",
        )
        self.assertEqual(self.store.billing(other).billing_anchor_day, 31)

    def test_spending_cap_stays_zero_by_default_when_card_added(self) -> None:
        self.assertEqual(DEFAULT_MONTHLY_CAP_CENTS, 0)
        self.assertEqual(self.store.billing(self.user_id).monthly_cap_cents, 0)
        apply_card_on_file(
            self.store,
            self.user_id,
            datetime(2026, 10, 21, 12, 0, tzinfo=UTC),
            stripe_customer_id="cus_cap",
        )
        after = self.store.billing(self.user_id)
        self.assertEqual(after.monthly_cap_cents, 0)
        self.assertEqual(after.monthly_cap_cents, DEFAULT_MONTHLY_CAP_CENTS)

    def test_failed_review_run_is_never_charged(self) -> None:
        # Past included reviews, a failed run still bills 0 cents.
        self.assertEqual(
            charge_for_review_cents(DEVELOPER, 300, status="failed"),
            0,
        )
        self.assertEqual(
            charge_for_review_cents(DEVELOPER, 300, status="done"),
            DEVELOPER.overage_cents,
        )


class CheckoutSetupModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite")
        self.store = Store(self.tmp.name)
        self.user_id = self.store.add_user("setup@example.com", 0)
        self.transport = FakeStripeTransport()
        self.client = _client(self.transport)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.close()

    def test_create_checkout_uses_setup_mode(self) -> None:
        env = _env()
        session = create_card_setup_checkout(
            self.store,
            self.client,
            self.user_id,
            plan_id="developer",
            success_url=env.success_url,
            cancel_url=env.cancel_url,
        )
        self.assertTrue(session["url"].startswith("https://checkout.stripe.com/"))
        modes = [fields.get("mode") for _m, url, fields in self.transport.calls if "checkout/sessions" in url]
        self.assertEqual(modes, [["setup"]])
        self.assertEqual(self.store.stripe_customer_id(self.user_id), "cus_test_1")


class WebhookSignatureAndIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite")
        self.store = Store(self.tmp.name)
        self.user_id = self.store.add_user("hook@example.com", 0)
        self.transport = FakeStripeTransport()
        self.client = _client(self.transport)
        self.card_at = datetime(2026, 1, 31, 16, 0, tzinfo=UTC)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.close()

    def _post(self, payload: bytes, header: str | None) -> tuple[int, dict]:
        return handle_stripe_webhook(
            self.store,
            self.client,
            payload=payload,
            signature_header=header,
            webhook_secret=_WEBHOOK_SECRET,
            price_for_plan=_PRICES,
            now=self.card_at,
            signature_now=1_700_000_000,
        )

    def test_bad_signature_returns_400(self) -> None:
        payload = _checkout_completed_event(event_id="evt_bad", user_id=self.user_id)
        status, body = self._post(payload, "t=1700000000,v1=deadbeef")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_signature")
        self.assertIsNone(self.store.billing(self.user_id).card_on_file_at)
        self.assertEqual(self.transport.subscriptions, 0)

    def test_replay_is_idempotent_by_event_id(self) -> None:
        payload = _checkout_completed_event(event_id="evt_once", user_id=self.user_id)
        header = sign_payload(payload, _WEBHOOK_SECRET, timestamp=1_700_000_000)
        first, body1 = self._post(payload, header)
        self.assertEqual(first, 200)
        self.assertTrue(body1.get("applied"))
        billing = self.store.billing(self.user_id, now=self.card_at)
        self.assertEqual(billing.billing_anchor_day, 31)
        self.assertEqual(billing.monthly_cap_cents, 0)
        self.assertEqual(
            billing.trial_ends_at,
            "2026-02-28T00:00:00.000000Z",
        )
        self.assertEqual(self.transport.subscriptions, 1)
        # Replay: same event id, no second subscription / no state change.
        second, body2 = self._post(payload, header)
        self.assertEqual(second, 200)
        self.assertTrue(body2.get("duplicate"))
        self.assertEqual(self.transport.subscriptions, 1)
        again = self.store.billing(self.user_id, now=self.card_at)
        self.assertEqual(again.trial_ends_at, billing.trial_ends_at)
        self.assertEqual(again.stripe_subscription_id, "sub_test_1")

    def test_unknown_event_type_returns_200_with_no_effect(self) -> None:
        body = {
            "id": "evt_unknown",
            "object": "event",
            "type": "invoice.paid",
            "data": {"object": {"id": "in_x"}},
        }
        payload = json.dumps(body).encode("utf-8")
        header = sign_payload(payload, _WEBHOOK_SECRET, timestamp=1_700_000_000)
        status, response = self._post(payload, header)
        self.assertEqual(status, 200)
        self.assertEqual(response, {"received": True})
        self.assertIsNone(self.store.billing(self.user_id).card_on_file_at)
        self.assertEqual(self.transport.subscriptions, 0)
        # Replay of unknown type still 200, still no effect.
        status2, response2 = self._post(payload, header)
        self.assertEqual(status2, 200)
        self.assertTrue(response2.get("duplicate"))

    def test_leap_year_trial_end_passed_to_stripe_subscription(self) -> None:
        leap_user = self.store.add_user("leap@example.com", 0)
        leap_at = datetime(2024, 1, 31, 9, 0, tzinfo=UTC)
        payload = _checkout_completed_event(event_id="evt_leap", user_id=leap_user)
        header = sign_payload(payload, _WEBHOOK_SECRET, timestamp=1_700_000_000)
        status, body = handle_stripe_webhook(
            self.store,
            self.client,
            payload=payload,
            signature_header=header,
            webhook_secret=_WEBHOOK_SECRET,
            price_for_plan=_PRICES,
            now=leap_at,
            signature_now=1_700_000_000,
        )
        self.assertEqual(status, 200)
        self.assertTrue(body.get("applied"))
        expected_end = trial_ends_at(leap_at)
        self.assertEqual(expected_end, datetime(2024, 2, 29, 0, 0, tzinfo=UTC))
        sub_calls = [c for c in self.transport.calls if c[1].endswith("/v1/subscriptions")]
        self.assertEqual(len(sub_calls), 1)
        trial_end = int(sub_calls[0][2]["trial_end"][0])
        self.assertEqual(trial_end, int(expected_end.timestamp()))
        self.assertEqual(self.store.billing(leap_user).billing_anchor_day, 31)


class HttpWebhookRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite")
        self.store = Store(self.tmp.name)
        self.user_id = self.store.add_user("route@example.com", 0)
        self.transport = FakeStripeTransport()
        self.client = _client(self.transport)
        self.env = _env()

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.close()

    def test_receive_stripe_webhook_bad_signature_400(self) -> None:
        payload = _checkout_completed_event(event_id="evt_http", user_id=self.user_id)
        status, body = receive_stripe_webhook(
            self.store,
            self.client,
            self.env,
            payload,
            "t=1,v1=nope",
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_signature")

    def test_bind_exposes_webhook_on_server(self) -> None:
        server = bind(
            self.tmp.name,
            0,
            stripe_client=self.client,
            stripe_env=self.env,
        )
        try:
            self.assertIs(server.stripe_client, self.client)
            self.assertIs(server.stripe_env, self.env)
        finally:
            server.server_close()
            server.store.close()


class SignatureHelperTests(unittest.TestCase):
    def test_verify_accepts_signed_payload(self) -> None:
        payload = b'{"id":"evt_x","type":"ping"}'
        header = sign_payload(payload, _WEBHOOK_SECRET, timestamp=1_700_000_000)
        verify_stripe_signature(
            payload,
            header,
            _WEBHOOK_SECRET,
            now=1_700_000_000,
        )

    def test_verify_rejects_tampered_payload(self) -> None:
        payload = b'{"id":"evt_x","type":"ping"}'
        header = sign_payload(payload, _WEBHOOK_SECRET, timestamp=1_700_000_000)
        with self.assertRaises(Exception):
            verify_stripe_signature(
                b'{"id":"evt_x","type":"pong"}',
                header,
                _WEBHOOK_SECRET,
                now=1_700_000_000,
            )


class EnvLoaderTests(unittest.TestCase):
    def test_load_stripe_env_requires_all_names(self) -> None:
        self.assertIsNone(load_stripe_env({}))
        partial = {ENV_SECRET_KEY: "sk_test_x"}
        self.assertIsNone(load_stripe_env(partial))
        full = {
            ENV_SECRET_KEY: "sk_test_x",
            ENV_WEBHOOK_SECRET: "whsec_x",
            ENV_PRICE_DEVELOPER: "price_d",
            ENV_PRICE_FLEET: "price_f",
            ENV_SUCCESS_URL: "https://ok/success",
            ENV_CANCEL_URL: "https://ok/cancel",
        }
        loaded = load_stripe_env(full)
        assert loaded is not None
        self.assertEqual(loaded.price_for("developer"), "price_d")
        self.assertEqual(loaded.price_for("fleet"), "price_f")


class StripeHttpFormTests(unittest.TestCase):
    def test_client_raises_on_error_status(self) -> None:
        def boom(
            method: str, url: str, headers: dict[str, str], body: bytes | None
        ) -> tuple[int, bytes]:
            return 401, b'{"error":{"message":"no"}}'

        client = StripeClient("sk_test_x", transport=boom)
        with self.assertRaises(StripeError):
            client.request("GET", "/v1/customers/cus_x")


if __name__ == "__main__":
    unittest.main()
