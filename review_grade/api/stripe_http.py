"""Thin Stripe REST client. Form-encoded bodies; HTTP is injectable for tests."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

# Stripe API version pinned for stable shapes in TEST mode.
STRIPE_API_VERSION = "2024-06-20"
DEFAULT_BASE_URL = "https://api.stripe.com"

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class StripeError(Exception):
    def __init__(self, status: int, body: object) -> None:
        self.status = status
        self.body = body
        super().__init__(f"Stripe HTTP {status}: {body!r}")


def _default_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


def _encode_form(fields: Mapping[str, Any]) -> bytes:
    pairs: list[tuple[str, str]] = []

    def walk(prefix: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, Mapping):
            for key, nested in value.items():
                walk(f"{prefix}[{key}]" if prefix else str(key), nested)
            return
        if isinstance(value, (list, tuple)):
            for index, nested in enumerate(value):
                walk(f"{prefix}[{index}]", nested)
            return
        if isinstance(value, bool):
            pairs.append((prefix, "true" if value else "false"))
            return
        pairs.append((prefix, str(value)))

    for key, value in fields.items():
        walk(str(key), value)
    return urllib.parse.urlencode(pairs).encode("utf-8")


class StripeClient:
    """Calls Stripe's REST API with Basic auth. No SDK dependency."""

    def __init__(
        self,
        secret_key: str,
        *,
        transport: Transport | None = None,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        if not secret_key:
            raise ValueError("secret_key must be non-empty")
        self._secret_key = secret_key
        self._transport = transport or _default_transport
        self._base_url = base_url.rstrip("/")

    def request(self, method: str, path: str, fields: Mapping[str, Any] | None = None) -> dict:
        if not path.startswith("/"):
            raise ValueError("path must start with /")
        url = self._base_url + path
        token = base64.b64encode(f"{self._secret_key}:".encode("utf-8")).decode("ascii")
        headers = {
            "Authorization": f"Basic {token}",
            "Stripe-Version": STRIPE_API_VERSION,
            "Accept": "application/json",
        }
        body: bytes | None = None
        if fields is not None:
            body = _encode_form(fields)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        status, raw = self._transport(method.upper(), url, headers, body)
        try:
            parsed: object = json.loads(raw.decode("utf-8") if raw else "null")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StripeError(status, raw) from exc
        if status >= 400 or not isinstance(parsed, dict):
            raise StripeError(status, parsed)
        return parsed

    def create_customer(self, *, email: str, metadata: Mapping[str, str]) -> dict:
        return self.request(
            "POST",
            "/v1/customers",
            {"email": email, "metadata": dict(metadata)},
        )

    def create_checkout_setup_session(
        self,
        *,
        customer_id: str,
        success_url: str,
        cancel_url: str,
        metadata: Mapping[str, str],
        client_reference_id: str,
    ) -> dict:
        return self.request(
            "POST",
            "/v1/checkout/sessions",
            {
                "mode": "setup",
                "customer": customer_id,
                "currency": "usd",
                "success_url": success_url,
                "cancel_url": cancel_url,
                "client_reference_id": client_reference_id,
                "metadata": dict(metadata),
                "payment_method_types": ["card"],
            },
        )

    def retrieve_setup_intent(self, setup_intent_id: str) -> dict:
        return self.request("GET", f"/v1/setup_intents/{setup_intent_id}")

    def create_subscription(
        self,
        *,
        customer_id: str,
        price_id: str,
        payment_method_id: str,
        trial_end: int,
        metadata: Mapping[str, str],
    ) -> dict:
        return self.request(
            "POST",
            "/v1/subscriptions",
            {
                "customer": customer_id,
                "items": [{"price": price_id}],
                "default_payment_method": payment_method_id,
                "trial_end": trial_end,
                "metadata": dict(metadata),
            },
        )
