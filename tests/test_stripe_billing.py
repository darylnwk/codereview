"""Stripe TEST-mode card on file, trial, anchor, cap, and webhooks.

HTTP to Stripe is mocked. Behavioural cases must fail if the production code
that implements them is removed.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs

import review_grade.api.store as store_mod
from review_grade.api.__main__ import main as api_main
from review_grade.api.http import bind, receive_stripe_webhook
from review_grade.api.plans import (
    DEFAULT_MONTHLY_CAP_CENTS,
    DEVELOPER,
    FLEET,
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
    StripePriceMismatch,
    apply_card_on_file,
    create_card_setup_checkout,
    handle_stripe_webhook,
    load_stripe_env,
    sign_payload,
    validate_configured_prices,
    verify_stripe_signature,
)
from review_grade.api.stripe_http import LiveModeRefused, StripeClient, StripeError, refuse_live_secret_key


_WEBHOOK_SECRET = "whsec_test_secret_for_unit_tests_only"
_PRICES = {"developer": "price_dev_test", "fleet": "price_fleet_test"}


def _stripe_event_row(db_path: str, event_id: str) -> sqlite3.Row | None:
    """Read ``stripe_events`` directly so dedupe cannot be faked in Store memory."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT id, type, processed_at FROM stripe_events WHERE id = ?",
            (event_id,),
        ).fetchone()
    finally:
        conn.close()


def _clear_in_process_event_claims() -> None:
    """Clear module/class-level sets so only sqlite-backed claim survives.

    A mutant that replaces ``claim_stripe_event`` with a module-level ``set``
    would still look like a duplicate after Store reopen unless this runs.
    """
    for obj in (store_mod, getattr(store_mod, "Store", None)):
        if obj is None:
            continue
        for _name, value in list(vars(obj).items()):
            if isinstance(value, set):
                value.clear()


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
            },
            "seti_no_pm": {
                "id": "seti_no_pm",
                "object": "setup_intent",
                "payment_method": None,
                "status": "requires_payment_method",
            },
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

    def test_card_at_0030_utc_plus_8_anchors_previous_utc_day(self) -> None:
        """00:30 in UTC+8 is still the previous calendar day in UTC.

        Mutant that uses local ``day`` without converting to UTC would anchor 21.
        """
        plus8 = timezone(timedelta(hours=8))
        card_at = datetime(2026, 10, 21, 0, 30, tzinfo=plus8)
        self.assertEqual(card_at.astimezone(UTC), datetime(2026, 10, 20, 16, 30, tzinfo=UTC))
        result = apply_card_on_file(
            self.store,
            self.user_id,
            card_at,
            stripe_customer_id="cus_utc8",
        )
        self.assertEqual(result["billing_anchor_day"], 20)
        self.assertEqual(self.store.billing(self.user_id).billing_anchor_day, 20)
        self.assertEqual(
            result["trial_ends_at"],
            "2026-11-20T00:00:00.000000Z",
        )

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

    def test_stale_signature_timestamp_returns_400(self) -> None:
        """A timestamp older than the 300s tolerance must be refused.

        Mutant that drops the tolerance check would accept this event.
        """
        payload = _checkout_completed_event(event_id="evt_stale", user_id=self.user_id)
        stale_ts = 1_700_000_000 - 301
        header = sign_payload(payload, _WEBHOOK_SECRET, timestamp=stale_ts)
        status, body = handle_stripe_webhook(
            self.store,
            self.client,
            payload=payload,
            signature_header=header,
            webhook_secret=_WEBHOOK_SECRET,
            price_for_plan=_PRICES,
            now=self.card_at,
            signature_now=1_700_000_000,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_signature")
        self.assertIsNone(self.store.billing(self.user_id).card_on_file_at)
        self.assertEqual(self.transport.subscriptions, 0)

    def test_setup_intent_without_payment_method_starts_no_trial(self) -> None:
        """Missing payment_method must not record a card or create a subscription."""
        payload = _checkout_completed_event(
            event_id="evt_no_pm",
            user_id=self.user_id,
            setup_intent="seti_no_pm",
        )
        header = sign_payload(payload, _WEBHOOK_SECRET, timestamp=1_700_000_000)
        status, body = self._post(payload, header)
        self.assertEqual(status, 200)
        self.assertFalse(body.get("applied", False))
        billing = self.store.billing(self.user_id)
        self.assertIsNone(billing.card_on_file_at)
        self.assertIsNone(billing.trial_ends_at)
        self.assertEqual(billing.billing_anchor_day, 1)
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

    def test_replay_after_reopen_store_is_duplicate(self) -> None:
        """Persisted event ids must survive close/reopen with in-process state cleared.

        (a) Deliver a signed event, assert the ``stripe_events`` row in sqlite,
        open a NEW Store on the same file after clearing module-level claim
        sets, replay, and assert duplicate with no second subscription or card
        update. A mutant that only dedupes in a module-level ``set`` fails once
        that set is cleared.
        """
        event_id = "evt_persist"
        payload = _checkout_completed_event(event_id=event_id, user_id=self.user_id)
        header = sign_payload(payload, _WEBHOOK_SECRET, timestamp=1_700_000_000)
        first, body1 = self._post(payload, header)
        self.assertEqual(first, 200)
        self.assertTrue(body1.get("applied"))
        self.assertEqual(self.transport.subscriptions, 1)
        row = _stripe_event_row(self.tmp.name, event_id)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["id"], event_id)
        self.assertEqual(row["type"], "checkout.session.completed")
        self.assertTrue(str(row["processed_at"]))
        before = self.store.billing(self.user_id, now=self.card_at)
        self.assertEqual(before.stripe_subscription_id, "sub_test_1")
        self.assertEqual(before.billing_anchor_day, 31)
        self.assertIsNotNone(before.card_on_file_at)
        db_path = self.tmp.name
        self.store.close()
        _clear_in_process_event_claims()
        reopened = Store(db_path)
        try:
            second, body2 = handle_stripe_webhook(
                reopened,
                self.client,
                payload=payload,
                signature_header=header,
                webhook_secret=_WEBHOOK_SECRET,
                price_for_plan=_PRICES,
                now=self.card_at,
                signature_now=1_700_000_000,
            )
            self.assertEqual(second, 200)
            self.assertTrue(body2.get("duplicate"))
            self.assertEqual(self.transport.subscriptions, 1)
            after = reopened.billing(self.user_id, now=self.card_at)
            self.assertEqual(after.stripe_subscription_id, before.stripe_subscription_id)
            self.assertEqual(after.card_on_file_at, before.card_on_file_at)
            self.assertEqual(after.trial_ends_at, before.trial_ends_at)
            self.assertEqual(after.billing_anchor_day, before.billing_anchor_day)
            self.assertEqual(_stripe_event_row(db_path, event_id)["id"], event_id)
        finally:
            reopened.close()
            # tearDown also closes self.store; reopen a handle so tearDown is safe.
            self.store = Store(db_path)

    def test_preinserted_stripe_event_id_is_duplicate(self) -> None:
        """(b) A row already in ``stripe_events`` must short-circuit the webhook.

        Pre-insert the event id without calling ``claim_stripe_event``, clear
        in-process sets, then deliver the signed event. Memory-only dedupe
        would apply a subscription; sqlite claim must report duplicate.
        """
        event_id = "evt_preinserted"
        db_path = self.tmp.name
        with self.store.transaction():
            self.store._conn.execute(
                """
                INSERT INTO stripe_events (id, type, processed_at)
                VALUES (?, ?, ?)
                """,
                (event_id, "checkout.session.completed", "2026-01-01T00:00:00.000000Z"),
            )
        self.assertIsNotNone(_stripe_event_row(db_path, event_id))
        _clear_in_process_event_claims()
        payload = _checkout_completed_event(event_id=event_id, user_id=self.user_id)
        header = sign_payload(payload, _WEBHOOK_SECRET, timestamp=1_700_000_000)
        status, body = self._post(payload, header)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("duplicate"))
        self.assertEqual(self.transport.subscriptions, 0)
        billing = self.store.billing(self.user_id, now=self.card_at)
        self.assertIsNone(billing.card_on_file_at)
        self.assertIsNone(billing.trial_ends_at)
        self.assertIsNone(billing.stripe_subscription_id)
        self.assertEqual(billing.billing_anchor_day, 1)

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


def _full_stripe_env(secret_key: str) -> dict[str, str]:
    return {
        ENV_SECRET_KEY: secret_key,
        ENV_WEBHOOK_SECRET: "whsec_x",
        ENV_PRICE_DEVELOPER: "price_d",
        ENV_PRICE_FLEET: "price_f",
        ENV_SUCCESS_URL: "https://ok/success",
        ENV_CANCEL_URL: "https://ok/cancel",
    }


def _matching_price(plan_id: str, price_id: str) -> dict:
    plan = DEVELOPER if plan_id == "developer" else FLEET
    return {
        "id": price_id,
        "object": "price",
        "unit_amount": plan.monthly_price_cents,
        "currency": "usd",
        "recurring": {"interval": "month", "interval_count": 1},
    }


class EnvLoaderTests(unittest.TestCase):
    def test_load_stripe_env_requires_all_names(self) -> None:
        self.assertIsNone(load_stripe_env({}))
        partial = {ENV_SECRET_KEY: "sk_test_x"}
        self.assertIsNone(load_stripe_env(partial))
        loaded = load_stripe_env(_full_stripe_env("sk_test_x"))
        assert loaded is not None
        self.assertEqual(loaded.price_for("developer"), "price_d")
        self.assertEqual(loaded.price_for("fleet"), "price_f")

    def test_allowed_secret_key_prefixes_load(self) -> None:
        for prefix in ("sk_test_", "rk_test_"):
            with self.subTest(prefix=prefix):
                key = prefix + ("a" * 24)
                loaded = load_stripe_env(_full_stripe_env(key))
                assert loaded is not None
                self.assertEqual(loaded.secret_key, key)
                # Construction must succeed for allowed TEST prefixes.
                StripeClient(key, transport=lambda *a: (200, b"{}"))

    def test_refused_secret_key_prefixes(self) -> None:
        refused = (
            ("sk_live_", "sk_live_" + ("x" * 24)),
            ("rk_live_", "rk_live_" + ("y" * 24)),
            ("pk_test_", "pk_test_" + ("z" * 24)),
            ("pk_live_", "pk_live_" + ("w" * 24)),
            ("empty", ""),
            ("garbage", "not-a-stripe-key"),
            ("sk_test_without_trailing_underscore", "sk_test"),
            # Allowed prefix must be at the start, not merely contained.
            ("allowed_prefix_after_live", "sk_live_sk_test_x"),
            ("allowed_prefix_not_at_start", "xsk_test_abc"),
            ("leading_space_before_rk_test", " rk_test_x"),
        )
        for label, key in refused:
            with self.subTest(label=label):
                with self.assertRaises(LiveModeRefused) as guard_ctx:
                    refuse_live_secret_key(key)
                guard_message = str(guard_ctx.exception)
                self.assertIn("sk_test_", guard_message)
                self.assertIn("rk_test_", guard_message)
                self.assertIn("TEST mode", guard_message)
                with self.assertRaises(LiveModeRefused):
                    StripeClient(key)
                if key:
                    # load_stripe_env strips; leading-space keys become allowed after strip.
                    stripped = key.strip()
                    if stripped.startswith(("sk_test_", "rk_test_")):
                        loaded = load_stripe_env(_full_stripe_env(key))
                        assert loaded is not None
                        self.assertEqual(loaded.secret_key, stripped)
                    else:
                        with self.assertRaises(LiveModeRefused):
                            load_stripe_env(_full_stripe_env(key))
                else:
                    # Empty STRIPE_SECRET_KEY is treated as unset (incomplete env).
                    self.assertIsNone(load_stripe_env(_full_stripe_env("")))

    def test_refused_key_value_never_appears_in_error(self) -> None:
        unique = "sk_live_NEVER_ECHO_THIS_UNIQUE_SUFFIX_9f3a"
        with self.assertRaises(LiveModeRefused) as ctx:
            load_stripe_env(_full_stripe_env(unique))
        message = str(ctx.exception)
        self.assertNotIn(unique, message)
        self.assertNotIn("NEVER_ECHO", message)
        with self.assertRaises(LiveModeRefused) as client_ctx:
            StripeClient(unique)
        self.assertNotIn(unique, str(client_ctx.exception))


class ConfiguredPriceValidationTests(unittest.TestCase):
    def _env(self) -> StripeEnv:
        loaded = load_stripe_env(_full_stripe_env("sk_test_price_check"))
        assert loaded is not None
        return loaded

    def test_matching_prices_pass(self) -> None:
        env = self._env()
        catalog = {
            env.price_developer: _matching_price("developer", env.price_developer),
            env.price_fleet: _matching_price("fleet", env.price_fleet),
        }

        def fetch(price_id: str) -> dict:
            return catalog[price_id]

        validate_configured_prices(env, fetch)

    def test_wrong_amount_fails(self) -> None:
        env = self._env()

        def fetch(price_id: str) -> dict:
            price = _matching_price(
                "developer" if price_id == env.price_developer else "fleet",
                price_id,
            )
            if price_id == env.price_developer:
                price["unit_amount"] = DEVELOPER.monthly_price_cents + 100
            return price

        with self.assertRaises(StripePriceMismatch) as ctx:
            validate_configured_prices(env, fetch)
        message = str(ctx.exception)
        self.assertIn("unit_amount", message)
        self.assertIn(str(DEVELOPER.monthly_price_cents), message)

    def test_wrong_currency_fails(self) -> None:
        env = self._env()

        def fetch(price_id: str) -> dict:
            price = _matching_price(
                "developer" if price_id == env.price_developer else "fleet",
                price_id,
            )
            if price_id == env.price_fleet:
                price["currency"] = "eur"
            return price

        with self.assertRaises(StripePriceMismatch) as ctx:
            validate_configured_prices(env, fetch)
        self.assertIn("currency", str(ctx.exception))
        self.assertIn("eur", str(ctx.exception))

    def test_wrong_interval_fails(self) -> None:
        env = self._env()

        def fetch(price_id: str) -> dict:
            price = _matching_price(
                "developer" if price_id == env.price_developer else "fleet",
                price_id,
            )
            if price_id == env.price_developer:
                price["recurring"] = {"interval": "year", "interval_count": 1}
            return price

        with self.assertRaises(StripePriceMismatch) as ctx:
            validate_configured_prices(env, fetch)
        self.assertIn("interval", str(ctx.exception))
        self.assertIn("year", str(ctx.exception))

    def test_missing_price_fails(self) -> None:
        env = self._env()
        secret_body = {"error": {"message": "No such price SECRET_BODY_TOKEN"}}

        def fetch(price_id: str) -> dict:
            if price_id == env.price_fleet:
                raise StripeError(404, secret_body)
            return _matching_price("developer", price_id)

        with self.assertRaises(StripePriceMismatch) as ctx:
            validate_configured_prices(env, fetch)
        message = str(ctx.exception)
        self.assertIn("could not be fetched", message)
        self.assertIn(env.price_fleet, message)
        self.assertIn("HTTP 404", message)
        self.assertNotIn("SECRET_BODY_TOKEN", message)
        self.assertNotIn("No such price", message)


class MainStartupValidationTests(unittest.TestCase):
    """Startup path through ``review_grade.api.__main__.main`` (no real server)."""

    _ARGV = ("--db", "/tmp/stripe-startup-test.sqlite", "--port", "0", "--no-worker")

    def _fake_httpd(self) -> SimpleNamespace:
        store = SimpleNamespace(close=lambda: None)
        return SimpleNamespace(
            price_cap=False,
            store=store,
            serve_forever=lambda: None,
            server_close=lambda: None,
        )

    def test_mismatched_price_raises_before_bind(self) -> None:
        env = _full_stripe_env("sk_test_startup_mismatch")

        def bad_retrieve(price_id: str) -> dict:
            price = _matching_price(
                "developer" if price_id == env[ENV_PRICE_DEVELOPER] else "fleet",
                price_id,
            )
            price["unit_amount"] = 1
            return price

        bind_mock = mock.Mock()
        client = mock.Mock()
        client.retrieve_price.side_effect = bad_retrieve
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch("review_grade.api.__main__.StripeClient", return_value=client):
                with mock.patch("review_grade.api.__main__.bind", bind_mock):
                    with self.assertRaises(StripePriceMismatch):
                        api_main(list(self._ARGV))
        bind_mock.assert_not_called()

    def test_matching_price_reaches_bind(self) -> None:
        env = _full_stripe_env("sk_test_startup_match")
        httpd = self._fake_httpd()
        bind_mock = mock.Mock(return_value=httpd)

        def good_retrieve(price_id: str) -> dict:
            plan = (
                "developer" if price_id == env[ENV_PRICE_DEVELOPER] else "fleet"
            )
            return _matching_price(plan, price_id)

        client = mock.Mock()
        client.retrieve_price.side_effect = good_retrieve
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch("review_grade.api.__main__.StripeClient", return_value=client):
                with mock.patch("review_grade.api.__main__.bind", bind_mock):
                    api_main(list(self._ARGV))
        bind_mock.assert_called_once()
        self.assertTrue(httpd.price_cap)

    def test_refused_key_absent_from_exception_and_stderr(self) -> None:
        unique = "sk_live_MAIN_PATH_NEVER_ECHO_7c2e"
        env = _full_stripe_env(unique)
        bind_mock = mock.Mock()
        stderr = io.StringIO()
        log_capture = io.StringIO()
        handler = logging.StreamHandler(log_capture)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                with mock.patch("review_grade.api.__main__.bind", bind_mock):
                    with mock.patch("sys.stderr", stderr):
                        with self.assertRaises(LiveModeRefused) as ctx:
                            api_main(list(self._ARGV))
        finally:
            root.removeHandler(handler)
        message = str(ctx.exception)
        self.assertNotIn(unique, message)
        self.assertNotIn("NEVER_ECHO", message)
        self.assertNotIn(unique, stderr.getvalue())
        self.assertNotIn(unique, log_capture.getvalue())
        bind_mock.assert_not_called()


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
