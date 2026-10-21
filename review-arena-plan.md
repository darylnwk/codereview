# Review grade — agentic build plan

A review of a pull request the user already has. They send the diff. The product returns the review. The same call runs in the browser, over the API, in CI, and from their own agent. Users pay per run. The hidden benchmark is internal. It is not a product, and it is not shown to users.

## What it is

Someone has a pull request. They send the diff. The model reviews it and returns findings: file, line, what breaks, and a one-line fix. One credit.

They do not write a review. They do not send review text. The review is the thing they are buying.

This is not recall. There is no planted-bug count on a user diff. The response must not say "4 of 6".

The benchmark of seeded pull requests stays on the server. You run the reviewer against diffs whose bugs you already know. If recall drops, you stop selling the call. Users never list those pull requests, and a user key cannot fetch them.

The chat is not the product. The browser is one client. The API is the product.

## How it makes money

The unit sold is a review, not access to a model.

Pricing decided by Daryl, 21 Oct 2026. No annual pricing. The old per-email free-review trial is removed.

| Offer | Price | What they get |
| --- | --- | --- |
| Trial | 1 month free | On **all** tiers. Starts when the user adds a card through Stripe. Ends one month later on the same day-of-month, clamped at month end (21 Oct → 21 Nov; 31 Jan → 28/29 Feb). That day is the billing-period anchor. Billing starts after that month unless cancelled |
| Developer | $25 / month | 250 reviews included, then $0.15 per extra review. User-set monthly cap on extras (default **$0** — no overage until they set a cap) |
| Fleet | $199 / month | 2,500 reviews included, then $0.06 per extra review. User-set monthly cap on extras (default **$0**) |

**Default overage cap $0** on all tiers — decided by Daryl, 21 Oct 2026. After included reviews, accept returns `cap_reached` until the user sets a positive monthly cap.

Unused included reviews end with the month. There is no single-review product and no pack of ten. Failed runs are free.

Cost of a run is one reviewer call. Gemini 3.8 Flash or Grok 4 Fast. The rubric is cached. The diff is not. A typical run should land at a few cents. Refuse any run projected over $0.15. Developer stays profitable if a typical run stays under $0.10.

Note (not a ship blocker): the 12 Oct discussion aimed for Fleet per-run cost under ~$0.04 once real `cache_read` / `cache_write` logging exists. Fleet stays in the offer; fill cache columns later to measure.

They pay because CI or their agent gets a structured review back, with a run id. A chat will also review a diff. It will not sit in the pipeline.

What does not sell:

- Unlimited monthly chat.
- A recall number on their diff.
- A GitHub App that comments for them. The Action is enough.
- Annual plans.

Expansion only after 5 of 10 trial users run a second review:

- Shared balance for a team, at the same tier prices.

Access is gated by the per-period included/overage cap, not the legacy `credits` column (kept on `users` but not decremented on finish).

## User interface

Three screens. No practice list. No dashboard. No review box.

### 1. Review

- Header: reviews left this period, plan line (Developer or Fleet), overage rate, link to Keys.
- One input: the diff (paste or file).
- Size line under the diff: changed lines, and the largest files.
- Submit disabled until the diff is non-empty, or until it is over the hard cap.
- Over 8,000 changed lines and under 20,000: Submit stays disabled until they uncheck files. The kept diff is the one that is reviewed.
- Over 20,000 changed lines, or only generated files (`package-lock`, snapshots, vendored): refuse. No credit.
- Footer note: "This spends 1 credit."

### 2. Result

Shown when the run is `done`. The page polls. The request does not wait on the model.

- Status while queued: "Reviewing…"
- Findings: file, line, what breaks, one-line fix.
- `partial: true` only if they asked to review a subset. Name the files that were not read.
- Actions: review another, copy JSON.

### 3. Keys

- Create key. Name it (`ci`, `agent`). Secret shown once. Prefix `ra_live_`. Stored as a hash. Revocable.
- List: name, prefix, last used, revoke.
- Copy-paste blocks for curl, the GitHub Action, and a short agent loop.
- The key spends the same balance as the website. No per-key balance in v1.

### Auth and money

- Magic link. Email in, link in, session cookie.
- Stripe Checkout in **setup** mode (not a bare SetupIntent): card on file starts the one-month free trial on the chosen tier (Developer or Fleet). Hosted Checkout fits the existing Checkout copy and needs no new card form. On `checkout.session.completed`, set Stripe Subscription `trial_end` to the same day-of-month one month later, clamped (not `trial_period_days=30`), and set local `billing_anchor_day` from the card-add UTC day (replacing the day-1 default). After the trial month, the tier price bills unless cancelled. Included reviews reset each period; overage bills at the tier rate until the user-set monthly cap (default $0). Cap is stored as integer cents and is not changed when the card is added.
- Webhooks: `POST /v1/stripe/webhook` verifies `Stripe-Signature`; handling is idempotent by Stripe event id. Bad signature → 400. Replay → 200, no effect. Unknown event type → 200, no effect.
- Account row: email, credits (legacy), plan, monthly_cap_cents, billing_anchor_day, card_on_file_at, trial_ends_at, Stripe customer id, Stripe subscription id.

### What the UI will not have in v1

- A box for the user to write a review.
- A list of seeded pull requests.
- A chat with the model.
- Leaderboards.
- Team admin.
- GitHub login or a GitHub App.

## API

Auth: `Authorization: Bearer ra_live_...`

A website submit and an API submit write the same `runs` row. `source` is `web`, `api`, `ci`, or `agent`.

```
POST /v1/reviews        body: { diff, files?, source? }
                        202 { run_id } after key, credit, and size checks
GET  /v1/runs/:id       { status, findings, partial, omitted, cost_usd }
GET  /v1/me             { reviews_left }
```

`status` is `queued`, `done`, or `failed`. Poll every 2 seconds. A run still queued after 3 minutes becomes `failed` and does not count toward the period. Accept reserves a period slot in the same transaction as the queued insert (cap check). Finish flips to `done` without touching `credits`. A second GET of a done run returns the same findings. If the process dies before finish, the run stays queued, and the restarted worker finishes once. `reviews_left` is included reviews still unused in the current period.

When the body includes `files`, a done run sets `partial` true and `omitted` to the paths that were not selected. A done review of the full diff sets `partial` false and `omitted` to an empty list. A queued GET keeps `partial` false and `omitted` [] until the run is done.

Send an `Idempotency-Key` header to repeat the same 202 response for the same diff. That same idempotency key with a different diff returns 409.

A queued GET has `findings` null. An empty list is reserved for a finished review that found nothing.

Size:

- Under 8,000 changed lines: review.
- 8,000 to 20,000: `413` with the file list. They resend with `files`. No credit on the 413.
- Over 20,000, or generated files only: `413`. No credit.
- Projected price over $0.15: `413` with `too_expensive`. The message is "The review would cost more than $0.15." The price is $0.00002 per changed line. 7,501 changed lines is over the cap. A 20-line diff is $0.0004 and is still accepted. No run is inserted and no credit is spent.

20,000 changed lines is a dump, not a review. A normal pull request is under 200 lines. A thousand is already large.

Rate limit: 10 accepts per minute per key. No batch endpoint. Many pulls are many `POST /v1/reviews` calls, polled separately.

A limit of 10 accepts per minute counts 202 responses only.

### Agent

Their agent is a client. You do not host it.

1. Agent has the diff.
2. `POST /v1/reviews` with the diff.
3. Poll `GET /v1/runs/:id`.
4. Read findings. A retry is another credit.

```bash
curl -s -H "Authorization: Bearer $REVIEW_GRADE_KEY" \
  https://api.reviewgrade.dev/v1/reviews \
  -d '{"diff":"...","source":"agent"}'
```

### CI

A GitHub Action, not an App.

Inputs: `api-key` (secret), `diff`. The Action posts, polls, and writes findings to the job summary. It does not fail the job on findings in v1. It fails the job on `401`, `402`, `413`, or reviewer `failed`.

### What the API will not do in v1

- Take a review as input.
- List or fetch the benchmark.
- Return an answer key.
- Return a recall fraction.
- Host their agent.
- Comment on GitHub. They can do that from the Action if they want.

## Async

The review does not finish inside the request. `POST` returns `202`. The worker reviews. The caller polls. The website uses the same run.

## Prompt cache

Cache the rubric, not the diff. The diff is different every call.

Prompt order: rubric and JSON shape, then the diff. The rubric bytes must be identical across calls.

Anthropic: mark the rubric with a cache breakpoint. Default TTL is 5 minutes, refreshed on a hit. A 1-hour TTL costs 2× the cache write. There is no call to ask if the cache is warm. After the response, `cache_read_input_tokens` > 0 is a hit. `cache_creation_input_tokens` > 0 and read at 0 is a write, which means first call, expiry, or a changed prefix. Log both on the run. A CI job that reviews once an hour will miss a 5-minute cache. Do not hold a cache overnight. The rubric is the small part of the bill. The size cap matters more.

## Models

- The product writes the review. The user does not.
- Reviewer: Gemini 3.8 Flash or Grok 4 Fast. Input: rubric and diff. Output: findings JSON.
- A strong model is only for the internal benchmark, to see if the reviewer still catches known bugs.
- Cap $0.15 projected. Log dollars on every done run. `review_diff` does not return token counts, so `cache_read` and `cache_write` stay null on a fixture run and on the default reviewer.

## Internal benchmark

Not a user feature. No route serves it.

1. Load `Qodo/PR-Review-Bench` from Hugging Face.
2. Keep Python and TypeScript, 3 to 8 defects, drop style and docs.
3. Emit 20 pull requests, about 80 defects, to `bench/subset.json`. The key stays in that file.
4. Run the reviewer on each diff. Do not give it the key.
5. A separate cheap matcher scores findings against the key. Recall = caught / planted.

Kill gate, before Stripe:

- Under 70% after two prompt passes: stop. Do not take money.
- 85% or better: ship.
- 70–85%: one more pass, then a human decides.

**Status (21 Oct 2026):** Kill gate (recall test) passed **locally**, run by Daryl. Not CI-verified. Re-run this when the rubric or the model changes. If recall drops under 70%, stop selling.

## Pipeline

1. Benchmark subset and kill-gate number. Locally passed 21 Oct 2026 (Daryl); still blocks Stripe until CI-verified if you want that bar.
2. Reviewer worker, async run row, size check, cache logging.
3. Three screens against the same review function.
4. API keys + Stripe card-on-file day-of-month trial on Developer and Fleet, Action snippet.
5. Ten users. Five of ten run a second review unprompted: add the shared team balance. Else stop.

## Tables

- `users(email, credits, stripe_customer_id, plan, monthly_cap_cents, billing_anchor_day, card_on_file_at, trial_ends_at, stripe_subscription_id)` — `credits` is legacy; default `monthly_cap_cents` is 0; legacy `monthly_cap_usd` if present is migrated ×100 and kept; `billing_anchor_day` starts at 1 and is set from the card-add day
- `api_keys(user_id, prefix, hash, name, last_used_at, revoked_at)`
- `runs(user_id, diff_hash, findings_json, status, partial, cost_usd, cache_read, cache_write, source, created_at)`
- `stripe_events(id, type, processed_at)` — webhook idempotency by Stripe event id

Included reviews and the overage cap are counted per billing period from `runs`: `queued` + `done` since period start. Failed runs never count. Period boundaries follow `billing_anchor_day` (card-on-file day, clamped). Accept checks the count and inserts the queued row in one transaction so concurrent accepts cannot overrun the cap.

`cache_read` and `cache_write` are columns on `runs`. They stay null. `review_diff` does not return token counts, and a fixture run does not invent them. `omitted` lists paths left out when the request included `files`. A queued GET leaves it empty.

## What not to do

- Do not ask the user for a review.
- Do not call it a practice arena.
- Do not show seeded pull requests to users.
- Do not return the answer key.
- Do not put a recall fraction on a user diff.
- Do not fine-tune.
- Do not install a GitHub App in v1.
- Do not sell unlimited chat.
