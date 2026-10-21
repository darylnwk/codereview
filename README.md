# Review grade

Code review your agent can call. Send a diff; get findings (file, line, what breaks, fix). One credit per finished review. Failed runs are free.

This repo is the agentic build: the review API, the internal recall benchmark, and a static pre-release site. Nothing is on sale yet.

## Pricing (planned)

- **Trial:** 1 month free on all tiers after a Stripe card is on file; billing starts after the month unless cancelled.
- **Developer:** $25/month, 250 reviews included, then $0.15 per extra review (user-set monthly cap).
- **Fleet:** $199/month, 2,500 reviews included, then $0.06 per extra review (user-set monthly cap).
- No annual pricing. The old per-email free-review trial is gone.
- Included/overage usage is counted per billing period from finished and accepted runs (failed never count). Period start is the calendar month UTC for now; later it will follow Stripe's billing anchor.
- Default monthly overage cap is $50 (carried over from main; same constant for Developer and Fleet until decided otherwise).

## How to run

Python 3.12+. No third-party runtime deps for the API.

```bash
# Seed a local key with credits, then serve
python3 -m review_grade.api.seed --db /tmp/review.sqlite --credits 250
python3 -m review_grade.api --db /tmp/review.sqlite --port 8080 --fixture finding
```

Static site (no build step):

```bash
cd site && python3 -m http.server 8000
```

Open `http://127.0.0.1:8000/`. Account pages use mock data only.

## How to test

```bash
python3 -m unittest discover -s tests -v
```

Lint (optional):

```bash
python3 -m ruff check review_grade tests
```
