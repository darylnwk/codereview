"""Plan prices, included reviews, overage, and the user-set monthly cap.

Decided by Daryl, 21 Oct 2026. There is no 20-review trial and no annual pricing.
Trial is one free month on every paid tier after a Stripe card is on file; that
start date is billing, not a separate plan id.
"""

from __future__ import annotations

from dataclasses import dataclass

# Failed runs never bill. Status values match the runs table.
_FAILED = "failed"
_DONE = "done"


@dataclass(frozen=True)
class Plan:
    id: str
    monthly_price_usd: int
    included_reviews: int
    overage_usd: float


DEVELOPER = Plan(
    id="developer",
    monthly_price_usd=25,
    included_reviews=250,
    overage_usd=0.15,
)
FLEET = Plan(
    id="fleet",
    monthly_price_usd=199,
    included_reviews=2500,
    overage_usd=0.06,
)

PLANS: dict[str, Plan] = {
    DEVELOPER.id: DEVELOPER,
    FLEET.id: FLEET,
}

# Default dollar cap on overage when the user has not set one.
DEFAULT_MONTHLY_CAP_USD = 50

# Trial is not a plan row. Card-on-file starts one free month on any tier.
TRIAL_DAYS = 30
ANNUAL_PRICING = False


def get_plan(plan_id: str) -> Plan:
    try:
        return PLANS[plan_id]
    except KeyError as exc:
        raise ValueError(f"unknown plan: {plan_id!r}") from exc


def overage_reviews(plan: Plan, finished: int) -> int:
    if finished < 0:
        raise ValueError("finished must be >= 0")
    return max(0, finished - plan.included_reviews)


def overage_spend_usd(plan: Plan, finished: int) -> float:
    return overage_reviews(plan, finished) * plan.overage_usd


def monthly_spend_usd(plan: Plan, finished: int) -> float:
    """Month fee plus overage. Trial waiver is applied by billing, not here."""
    return plan.monthly_price_usd + overage_spend_usd(plan, finished)


def charge_for_review(plan: Plan, finished_before: int, *, status: str) -> float:
    """Dollar charge for one settled review. Failed runs are free."""
    if status == _FAILED:
        return 0.0
    if status != _DONE:
        raise ValueError(f"status must be done or failed, got {status!r}")
    if finished_before < 0:
        raise ValueError("finished_before must be >= 0")
    if finished_before < plan.included_reviews:
        return 0.0
    return plan.overage_usd


def within_monthly_cap(plan: Plan, finished: int, cap_usd: float) -> bool:
    """True when overage for `finished` done reviews is at or under the cap."""
    if cap_usd < 0:
        raise ValueError("cap_usd must be >= 0")
    return overage_spend_usd(plan, finished) <= float(cap_usd)


def allow_next_review(plan: Plan, finished: int, cap_usd: float) -> bool:
    """True when finishing one more review would stay within the user cap."""
    if finished < 0:
        raise ValueError("finished must be >= 0")
    return within_monthly_cap(plan, finished + 1, cap_usd)


def has_legacy_trial_plan() -> bool:
    """True if a removed per-email free-review trial plan were still defined."""
    return "trial" in PLANS or any(p.included_reviews == 20 for p in PLANS.values())
