"""Card on file via Checkout setup mode, day-of-month trial, and webhooks.

Why Checkout setup mode (not a bare SetupIntent): the plan and account copy
already name Stripe Checkout, and setup mode collects a card without charging
and without a new Elements surface. A SetupIntent alone would need a custom
card form (a new user-facing surface).

Trial starts when the card is added. ``trial_ends_at`` is the same day next
month, clamped (31 Jan → 28/29 Feb). That day becomes ``billing_anchor_day``.
The monthly overage cap stays at the default $0; failed runs never charge.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from review_grade.api.plans import get_plan, trial_ends_at
from review_grade.api.store import Store
from review_grade.api.stripe_http import StripeClient

# Env var names (values never set by this package; see docs/stripe-env.md).
ENV_SECRET_KEY = "STRIPE_SECRET_KEY"
ENV_WEBHOOK_SECRET = "STRIPE_WEBHOOK_SECRET"
ENV_PRICE_DEVELOPER = "STRIPE_PRICE_DEVELOPER"
ENV_PRICE_FLEET = "STRIPE_PRICE_FLEET"
ENV_SUCCESS_URL = "STRIPE_CHECKOUT_SUCCESS_URL"
ENV_CANCEL_URL = "STRIPE_CHECKOUT_CANCEL_URL"

_SIGNATURE_TOLERANCE_SECONDS = 300


class SignatureError(Exception):
    """Stripe-Signature header failed verification."""


@dataclass(frozen=True)
class StripeEnv:
    secret_key: str
    webhook_secret: str
    price_developer: str
    price_fleet: str
    success_url: str
    cancel_url: str

    def price_for(self, plan_id: str) -> str:
        get_plan(plan_id)  # validate
        if plan_id == "fleet":
            return self.price_fleet
        return self.price_developer


def load_stripe_env(environ: dict[str, str] | None = None) -> StripeEnv | None:
    """Read Stripe env names. Returns None if any required name is missing.

    Does not create, request, or write secrets.
    """
    env = os.environ if environ is None else environ
    names = (
        ENV_SECRET_KEY,
        ENV_WEBHOOK_SECRET,
        ENV_PRICE_DEVELOPER,
        ENV_PRICE_FLEET,
        ENV_SUCCESS_URL,
        ENV_CANCEL_URL,
    )
    values = {name: env.get(name, "").strip() for name in names}
    if any(not values[name] for name in names):
        return None
    return StripeEnv(
        secret_key=values[ENV_SECRET_KEY],
        webhook_secret=values[ENV_WEBHOOK_SECRET],
        price_developer=values[ENV_PRICE_DEVELOPER],
        price_fleet=values[ENV_PRICE_FLEET],
        success_url=values[ENV_SUCCESS_URL],
        cancel_url=values[ENV_CANCEL_URL],
    )


def verify_stripe_signature(
    payload: bytes,
    header: str | None,
    secret: str,
    *,
    tolerance: int = _SIGNATURE_TOLERANCE_SECONDS,
    now: int | None = None,
) -> None:
    """Raise SignatureError unless ``Stripe-Signature`` matches ``secret``."""
    if not secret:
        raise SignatureError("missing webhook secret")
    if header is None or header.strip() == "":
        raise SignatureError("missing Stripe-Signature header")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SignatureError("payload is not utf-8") from exc
    timestamp = None
    signatures: list[str] = []
    for item in header.split(","):
        key, sep, value = item.strip().partition("=")
        if sep != "=":
            continue
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError as exc:
                raise SignatureError("invalid timestamp") from exc
        elif key == "v1":
            signatures.append(value)
    if timestamp is None or not signatures:
        raise SignatureError("timestamp or v1 signature missing")
    signed = f"{timestamp}.{text}".encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise SignatureError("no matching v1 signature")
    clock = int(time.time()) if now is None else now
    if abs(clock - timestamp) > tolerance:
        raise SignatureError("timestamp outside tolerance")


def sign_payload(payload: bytes | str, secret: str, *, timestamp: int | None = None) -> str:
    """Build a ``Stripe-Signature`` header for tests."""
    if isinstance(payload, bytes):
        text = payload.decode("utf-8")
    else:
        text = payload
    ts = int(time.time()) if timestamp is None else timestamp
    signed = f"{ts}.{text}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def apply_card_on_file(
    store: Store,
    user_id: int,
    card_on_file_at: datetime,
    *,
    stripe_customer_id: str,
    stripe_subscription_id: str | None = None,
) -> dict[str, object]:
    """Start trial, set billing anchor from card-add day, keep the $0 default cap.

    Replaces the day-1 billing_anchor_day TODO: the anchor is the UTC day the
    card was added. Cap is left at DEFAULT_MONTHLY_CAP_CENTS ($0).
    """
    return store.record_card_on_file(
        user_id,
        card_on_file_at,
        stripe_customer_id=stripe_customer_id,
        stripe_subscription_id=stripe_subscription_id,
    )


def create_card_setup_checkout(
    store: Store,
    client: StripeClient,
    user_id: int,
    *,
    plan_id: str,
    success_url: str,
    cancel_url: str,
) -> dict[str, str]:
    """Create a Checkout Session in setup mode for the user's card.

    Returns ``{"id": session_id, "url": checkout_url}``.
    """
    get_plan(plan_id)
    email = store.user_email(user_id)
    if email is None:
        raise ValueError(f"unknown user_id {user_id}")
    customer_id = store.stripe_customer_id(user_id)
    if customer_id is None:
        customer = client.create_customer(
            email=email,
            metadata={"user_id": str(user_id)},
        )
        customer_id = str(customer["id"])
        store.set_stripe_customer_id(user_id, customer_id)
    session = client.create_checkout_setup_session(
        customer_id=customer_id,
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=str(user_id),
        metadata={"user_id": str(user_id), "plan": plan_id},
    )
    url = session.get("url")
    session_id = session.get("id")
    if not isinstance(url, str) or not isinstance(session_id, str):
        raise ValueError("Checkout Session response missing id or url")
    return {"id": session_id, "url": url}


def handle_stripe_webhook(
    store: Store,
    client: StripeClient,
    *,
    payload: bytes,
    signature_header: str | None,
    webhook_secret: str,
    price_for_plan: dict[str, str],
    now: datetime | None = None,
    signature_now: int | None = None,
) -> tuple[int, dict]:
    """Verify, then apply the event once by Stripe event id.

    - Bad signature → 400
    - Replay of a known event id → 200, no effect
    - Unknown event type → 200, no effect (still recorded for idempotency)
    - ``checkout.session.completed`` in setup mode → card on file + trial
    """
    try:
        verify_stripe_signature(
            payload,
            signature_header,
            webhook_secret,
            now=signature_now,
        )
    except SignatureError:
        return 400, {"error": "invalid_signature"}

    try:
        event = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, {"error": "invalid_payload"}
    if not isinstance(event, dict):
        return 400, {"error": "invalid_payload"}

    event_id = event.get("id")
    event_type = event.get("type")
    if not isinstance(event_id, str) or event_id == "":
        return 400, {"error": "invalid_payload"}
    if not isinstance(event_type, str):
        event_type = ""

    claimed = store.claim_stripe_event(event_id, event_type)
    if not claimed:
        return 200, {"received": True, "duplicate": True}

    if event_type != "checkout.session.completed":
        return 200, {"received": True}

    data_object = _event_object(event)
    if data_object is None:
        return 200, {"received": True}
    if data_object.get("mode") != "setup":
        return 200, {"received": True}

    try:
        _apply_setup_session(
            store,
            client,
            data_object,
            price_for_plan=price_for_plan,
            now=now,
        )
    except (KeyError, TypeError, ValueError):
        # Event is claimed; do not re-apply on replay. Surface as received.
        return 200, {"received": True, "applied": False}
    return 200, {"received": True, "applied": True}


def _event_object(event: dict) -> dict | None:
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    obj = data.get("object")
    if not isinstance(obj, dict):
        return None
    return obj


def _apply_setup_session(
    store: Store,
    client: StripeClient,
    session: dict,
    *,
    price_for_plan: dict[str, str],
    now: datetime | None,
) -> None:
    metadata = session.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("session metadata missing")
    raw_user = metadata.get("user_id") or session.get("client_reference_id")
    if raw_user is None:
        raise ValueError("user_id missing on session")
    user_id = int(raw_user)
    plan_id = str(metadata.get("plan") or "developer")
    get_plan(plan_id)
    price_id = price_for_plan.get(plan_id)
    if not price_id:
        raise ValueError(f"no Stripe price for plan {plan_id!r}")

    customer_id = session.get("customer")
    if not isinstance(customer_id, str) or customer_id == "":
        raise ValueError("customer missing on session")

    setup_intent_ref = session.get("setup_intent")
    if isinstance(setup_intent_ref, dict):
        setup_intent_id = setup_intent_ref.get("id")
    else:
        setup_intent_id = setup_intent_ref
    if not isinstance(setup_intent_id, str) or setup_intent_id == "":
        raise ValueError("setup_intent missing on session")

    setup_intent = client.retrieve_setup_intent(setup_intent_id)
    payment_method = setup_intent.get("payment_method")
    if isinstance(payment_method, dict):
        payment_method_id = payment_method.get("id")
    else:
        payment_method_id = payment_method
    if not isinstance(payment_method_id, str) or payment_method_id == "":
        raise ValueError("payment_method missing on SetupIntent")

    card_at = now if now is not None else datetime.now(UTC)
    ends = trial_ends_at(card_at)
    trial_end_unix = int(ends.timestamp())

    subscription = client.create_subscription(
        customer_id=customer_id,
        price_id=price_id,
        payment_method_id=payment_method_id,
        trial_end=trial_end_unix,
        metadata={"user_id": str(user_id), "plan": plan_id},
    )
    subscription_id = str(subscription["id"])
    apply_card_on_file(
        store,
        user_id,
        card_at,
        stripe_customer_id=customer_id,
        stripe_subscription_id=subscription_id,
    )
