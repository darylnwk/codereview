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

| Offer | Price | What they get |
| --- | --- | --- |
| Pack of 10 runs | $19 | Ten reviews, web or API, same balance |
| Single run | $3 | One review |
| Free trial | 2 runs | Enough to see a review, not enough to live in CI |

Cost of a run is one reviewer call. Gemini 3.8 Flash or Grok 4 Fast. The rubric is cached. The diff is not. A typical run should land at a few cents to about $0.15. Refuse any run projected over $0.30. At $1.90 effective per pack run, margin stays wide.

They pay because CI or their agent gets a structured review back, with a run id and a credit receipt. A chat will also review a diff. It will not sit in the pipeline.

What does not sell:

- Unlimited monthly chat.
- A recall number on their diff.
- A GitHub App that comments for them. The Action is enough.

Expansion only after 5 of 10 trial users run a second review:

- Team pack: 50 runs for $79, shared balance.

Refund rule: if the reviewer fails, do not decrement the credit.

## User interface

Three screens. No practice list. No dashboard. No review box.

### 1. Review

- Header: credits remaining, "Buy 10 for $19", link to Keys.
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
- Stripe Checkout for the $19 pack. Success returns to Review with 10 credits.
- Account row: email, credits, Stripe customer id.

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
GET  /v1/runs/:id       { status, findings, partial, cost_usd }
GET  /v1/me             { credits }
```

`status` is `queued`, `done`, or `failed`. Poll every 2 seconds. A run still queued after 3 minutes becomes `failed` and does not spend a credit. The credit decrements only when status flips to `done`.

Size:

- Under 8,000 changed lines: review.
- 8,000 to 20,000: `413` with the file list. They resend with `files`. No credit on the 413.
- Over 20,000, or generated files only: `413`. No credit.

20,000 changed lines is a dump, not a review. A normal pull request is under 200 lines. A thousand is already large.

Rate limit: 10 accepts per minute per key. No batch endpoint. Many pulls are many `POST /v1/reviews` calls, polled separately.

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
- Cap $0.30 projected. Log tokens, cache read, cache write, and dollars on every run.

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

Re-run this when the rubric or the model changes. If recall drops under 70%, stop selling.

## Pipeline

1. Benchmark subset and kill-gate number. Blocks Stripe.
2. Reviewer worker, async run row, size check, cache logging.
3. Three screens against the same review function.
4. Keys, Stripe pack, Action snippet.
5. Ten users. Five of ten run a second review unprompted: add the team pack. Else stop.

## Tables

- `users(email, credits, stripe_customer_id)`
- `api_keys(user_id, prefix, hash, name, last_used_at, revoked_at)`
- `runs(user_id, diff_hash, findings_json, status, partial, cost_usd, cache_read, cache_write, source, created_at)`

## What not to do

- Do not ask the user for a review.
- Do not call it a practice arena.
- Do not show seeded pull requests to users.
- Do not return the answer key.
- Do not put a recall fraction on a user diff.
- Do not fine-tune.
- Do not install a GitHub App in v1.
- Do not sell unlimited chat.
