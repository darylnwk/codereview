"""Plan prices, included reviews, overage, and the user-set monthly cap.

Decided by Daryl, 21 Oct 2026. There is no per-email free-review trial and no
annual pricing. Trial is one free month on every paid tier after a Stripe card
is on file (same day-of-month, clamped at month end); that date is the billing
anchor, not a separate plan id.

Money math uses integer cents. Dollar helpers divide by 100 for display/tests.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timezone

# Failed runs never bill. Status values match the runs table.
_FAILED = "failed"
_DONE = "done"

# Decision (Daryl, 21 Oct 2026): default monthly overage cap is $0 on every
# tier — no default overage. After included reviews, accept returns cap_reached
# until the user sets a positive monthly cap.
DEFAULT_MONTHLY_CAP_USD = 0
DEFAULT_MONTHLY_CAP_CENTS = DEFAULT_MONTHLY_CAP_USD * 100

# Verbatim product copy (curly apostrophe U+2019).
CAP_REACHED_ZERO = (
    "You’ve used all {included} reviews in your plan this month. "
    "To keep reviewing, set a monthly spending cap for extra reviews."
)
CAP_REACHED_SET = (
    "You’ve reached your {cap} monthly spending cap for extra reviews. "
    "Raise your cap to keep reviewing, or wait until {reset_date}."
)


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

ANNUAL_PRICING = False


def get_plan(plan_id: str) -> Plan:
    try:
        return PLANS[plan_id]
    except KeyError as exc:
        raise ValueError(f"unknown plan: {plan_id!r}") from exc


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _clamp_day(year: int, month: int, day: int) -> int:
    return min(day, calendar.monthrange(year, month)[1])


def _period_boundary(year: int, month: int, anchor_day: int) -> datetime:
    day = _clamp_day(year, month, anchor_day)
    return datetime(year, month, day, 0, 0, 0, 0, tzinfo=timezone.utc)


def add_one_month(moment: datetime, *, anchor_day: int | None = None) -> datetime:
    """Same day-of-month one month later, clamped to the target month's last day."""
    moment = _as_utc(moment)
    day = moment.day if anchor_day is None else anchor_day
    if not 1 <= day <= 31:
        raise ValueError("anchor_day must be in 1..31")
    if moment.month == 12:
        year, month = moment.year + 1, 1
    else:
        year, month = moment.year, moment.month + 1
    return moment.replace(year=year, month=month, day=_clamp_day(year, month, day))


def trial_ends_at(card_on_file_at: datetime) -> datetime:
    """First instant after the free trial.

    One month from the day the card is added, same day-of-month, clamped at
    month end (21 Oct → 21 Nov; 31 Jan → 28/29 Feb; 31 Mar → 30 Apr). When
    wiring Stripe, set ``trial_end`` to this instant rather than a fixed day
    count. Billing periods use the same anchor day.
    """
    return add_one_month(_as_utc(card_on_file_at))


def billing_period_start(
    moment: datetime | None = None,
    *,
    anchor_day: int = 1,
) -> datetime:
    """Start of the billing period containing ``moment``.

    Period boundaries fall on ``anchor_day`` each month (clamped when the month
    is shorter). Default anchor day 1 matches users who have not set a card yet.
    """
    if not 1 <= anchor_day <= 31:
        raise ValueError("anchor_day must be in 1..31")
    now = _as_utc(datetime.now(timezone.utc) if moment is None else moment)
    this = _period_boundary(now.year, now.month, anchor_day)
    if now >= this:
        return this
    if now.month == 1:
        return _period_boundary(now.year - 1, 12, anchor_day)
    return _period_boundary(now.year, now.month - 1, anchor_day)


def next_billing_period_start(
    moment: datetime | None = None,
    *,
    anchor_day: int = 1,
) -> datetime:
    """First instant of the period after the one containing ``moment``."""
    start = billing_period_start(moment, anchor_day=anchor_day)
    return add_one_month(start, anchor_day=anchor_day)


def format_cap_label(cap_cents: int) -> str:
    """Display form for cap copy, e.g. ``$12.99`` or ``$1``."""
    if cap_cents < 0:
        raise ValueError("cap_cents must be >= 0")
    if cap_cents % 100 == 0:
        return f"${cap_cents // 100}"
    return f"${cap_cents / 100:.2f}"


def format_reset_date(moment: datetime) -> str:
    """Human reset date for cap copy, e.g. ``21 Nov 2026``."""
    moment = _as_utc(moment)
    return f"{moment.day} {moment.strftime('%b')} {moment.year}"


def usd_to_cents(cap_usd: float) -> int:
    if isinstance(cap_usd, bool) or not isinstance(cap_usd, (int, float)):
        raise TypeError("cap_usd must be a number")
    if cap_usd < 0:
        raise ValueError("cap_usd must be >= 0")
    return int(round(cap_usd * 100))


# Back-compat alias used by older tests / comments.
_cap_cents = usd_to_cents


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


def within_monthly_cap(plan: Plan, finished: int, cap_cents: int) -> bool:
    """True when overage for `finished` billable reviews is at or under the cap."""
    if isinstance(cap_cents, bool) or not isinstance(cap_cents, int):
        raise TypeError("cap_cents must be an int")
    if cap_cents < 0:
        raise ValueError("cap_cents must be >= 0")
    return overage_spend_cents(plan, finished) <= cap_cents


def allow_next_review(plan: Plan, finished: int, cap_cents: int) -> bool:
    """True when one more billable review would stay within the user cap."""
    if finished < 0:
        raise ValueError("finished must be >= 0")
    return within_monthly_cap(plan, finished + 1, cap_cents)


def reviews_left_this_period(plan: Plan, period_reviews: int) -> int:
    """Included reviews still unused in the period (not counting overage headroom)."""
    if period_reviews < 0:
        raise ValueError("period_reviews must be >= 0")
    return max(0, plan.included_reviews - period_reviews)


def has_legacy_trial_plan() -> bool:
    """True if a removed per-email free-review trial plan were still defined."""
    return "trial" in PLANS or any(p.included_reviews == 20 for p in PLANS.values())
