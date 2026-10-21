"""Plan prices, included reviews, overage, and the user-set monthly cap.

Decided by Daryl, 21 Oct 2026. There is no per-email free-review trial and no
annual pricing. Trial is one free month on every paid tier after a Stripe card
is on file; that start date is billing, not a separate plan id.

Money math uses integer cents. Dollar helpers divide by 100 for display/tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# Failed runs never bill. Status values match the runs table.
_FAILED = "failed"
_DONE = "done"

# Carried over from main (account mock / product default). Not new pricing.
# Also applied to Fleet until a separate default is decided.
DEFAULT_MONTHLY_CAP_USD = 50
DEFAULT_MONTHLY_CAP_CENTS = DEFAULT_MONTHLY_CAP_USD * 100


@dataclass(frozen=True)
class Plan:
    id: str
    monthly_price_cents: int
    included_reviews: int
    overage_cents: int

    @property
    def monthly_price_usd(self) -> float:
        return self.monthly_price_cents / 100

    @property
    def overage_usd(self) -> float:
        return self.overage_cents / 100


DEVELOPER = Plan(
    id="developer",
    monthly_price_cents=2500,
    included_reviews=250,
    overage_cents=15,
)
FLEET = Plan(
    id="fleet",
    monthly_price_cents=19900,
    included_reviews=2500,
    overage_cents=6,
)

PLANS: dict[str, Plan] = {
    DEVELOPER.id: DEVELOPER,
    FLEET.id: FLEET,
}

# Trial is not a plan row. Card-on-file starts one free month on any tier.
TRIAL_DAYS = 30
ANNUAL_PRICING = False


def get_plan(plan_id: str) -> Plan:
    try:
        return PLANS[plan_id]
    except KeyError as exc:
        raise ValueError(f"unknown plan: {plan_id!r}") from exc


def billing_period_start(moment: datetime | None = None) -> datetime:
    """Start of the current billing period (calendar month UTC for now).

    Later this should follow Stripe's subscription billing anchor.
    """
    now = datetime.now(timezone.utc) if moment is None else moment
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def overage_reviews(plan: Plan, finished: int) -> int:
    if finished < 0:
        raise ValueError("finished must be >= 0")
    return max(0, finished - plan.included_reviews)


def overage_spend_cents(plan: Plan, finished: int) -> int:
    return overage_reviews(plan, finished) * plan.overage_cents


def overage_spend_usd(plan: Plan, finished: int) -> float:
    return overage_spend_cents(plan, finished) / 100


def monthly_spend_cents(plan: Plan, finished: int) -> int:
    """Month fee plus overage in cents. Trial waiver is applied by billing, not here."""
    return plan.monthly_price_cents + overage_spend_cents(plan, finished)


def monthly_spend_usd(plan: Plan, finished: int) -> float:
    return monthly_spend_cents(plan, finished) / 100


def charge_for_review_cents(plan: Plan, finished_before: int, *, status: str) -> int:
    """Cent charge for one settled review. Failed runs are free."""
    if status == _FAILED:
        return 0
    if status != _DONE:
        raise ValueError(f"status must be done or failed, got {status!r}")
    if finished_before < 0:
        raise ValueError("finished_before must be >= 0")
    if finished_before < plan.included_reviews:
        return 0
    return plan.overage_cents


def charge_for_review(plan: Plan, finished_before: int, *, status: str) -> float:
    return charge_for_review_cents(plan, finished_before, status=status) / 100


def _cap_cents(cap_usd: float) -> int:
    if isinstance(cap_usd, bool) or not isinstance(cap_usd, (int, float)):
        raise TypeError("cap_usd must be a number")
    if cap_usd < 0:
        raise ValueError("cap_usd must be >= 0")
    return int(cap_usd) * 100


def within_monthly_cap(plan: Plan, finished: int, cap_usd: float) -> bool:
    """True when overage for `finished` billable reviews is at or under the cap."""
    return overage_spend_cents(plan, finished) <= _cap_cents(cap_usd)


def allow_next_review(plan: Plan, finished: int, cap_usd: float) -> bool:
    """True when one more billable review would stay within the user cap."""
    if finished < 0:
        raise ValueError("finished must be >= 0")
    return within_monthly_cap(plan, finished + 1, cap_usd)


def has_legacy_trial_plan() -> bool:
    """True if a removed per-email free-review trial plan were still defined."""
    return "trial" in PLANS or any(p.included_reviews == 20 for p in PLANS.values())
