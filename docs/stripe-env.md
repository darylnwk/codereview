# Stripe TEST-mode environment

Names only. Do not put secret values in this repo or in agent chat.

## Environment variable names

| Name | Purpose |
| --- | --- |
| `STRIPE_SECRET_KEY` | Stripe secret key (`sk_test_…`) for REST calls |
| `STRIPE_WEBHOOK_SECRET` | Signing secret for `POST /v1/stripe/webhook` (`whsec_…`) |
| `STRIPE_PRICE_DEVELOPER` | Price id for the Developer plan (`price_…`) |
| `STRIPE_PRICE_FLEET` | Price id for the Fleet plan (`price_…`) |
| `STRIPE_CHECKOUT_SUCCESS_URL` | Checkout `success_url` (may include `{CHECKOUT_SESSION_ID}`) |
| `STRIPE_CHECKOUT_CANCEL_URL` | Checkout `cancel_url` |

The API reads these names when present. It does not create or write them.

## Stripe Dashboard objects (TEST mode)

### Products / prices

Create two recurring monthly prices (or one product with two prices):

- Developer — $25 / month → copy id into `STRIPE_PRICE_DEVELOPER`
- Fleet — $199 / month → copy id into `STRIPE_PRICE_FLEET`

### Webhook endpoint

- URL: `https://<api-host>/v1/stripe/webhook`
- Mode: Test
- Events to send:
  - `checkout.session.completed` (required for card-on-file + trial start)
- Copy the endpoint signing secret into `STRIPE_WEBHOOK_SECRET`

Other event types are accepted with HTTP 200 and ignored (idempotent by event id).

### Checkout

Card collection uses Checkout **setup** mode (not payment). No Dashboard Checkout UI object is required beyond enabling card payment methods for Checkout in TEST mode.
