"""Internal model/token cost for each review.

Customer-facing run.cost_usd stays the projected line price. This module
records what the provider call actually cost us: every attempt (including
retries and failed runs we absorb), whether the customer was charged.

Unknown usage or unpriced rates store NULL tokens/cost with explicit flags.
A real zero token count or zero dollar cost stays distinguishable from unknown.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from review_grade.api.model_pricing import (
    MODEL_PRICING,
    ModelRates,
    effective_mtok_rates,
    rates_for,
)

if TYPE_CHECKING:
    from review_grade.api.store import Store

logger = logging.getLogger("review_grade.api.review_cost")

_TOKENS_PER_MTOK = 1_000_000


@dataclass(frozen=True)
class AttemptUsage:
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    usage_known: bool = False


def unknown_usage(model: str) -> AttemptUsage:
    """Failure before a usage envelope, non-JSON reply, or missing usage."""
    return AttemptUsage(
        model=model,
        input_tokens=None,
        output_tokens=None,
        usage_known=False,
    )


def known_usage(model: str, *, input_tokens: int, output_tokens: int) -> AttemptUsage:
    """Provider reported token counts (including a real zero)."""
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts must be non-negative")
    return AttemptUsage(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        usage_known=True,
    )


@dataclass(frozen=True)
class AttemptCost:
    model: str
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    usage_known: bool
    priced: bool


@dataclass(frozen=True)
class ReviewCostRecord:
    """One finished review's absorbed provider cost."""

    run_id: str
    account: int
    attempts: tuple[AttemptCost, ...]
    total_cost_usd: float | None
    charged: bool


def attempt_cost_usd(
    *,
    input_tokens: int,
    output_tokens: int,
    input_usd_per_mtok: float,
    output_usd_per_mtok: float,
) -> float:
    """Dollar cost for one provider call from token counts and per-mtok rates."""
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts must be non-negative")
    return (
        input_tokens * input_usd_per_mtok + output_tokens * output_usd_per_mtok
    ) / _TOKENS_PER_MTOK


def build_attempt(
    usage: AttemptUsage,
    *,
    pricing: dict[str, ModelRates] | None = None,
) -> AttemptCost:
    """Price one attempt. Unknown model/rates or unknown usage → NULL cost."""
    if not usage.usage_known:
        return AttemptCost(
            model=usage.model,
            input_tokens=None,
            output_tokens=None,
            cost_usd=None,
            usage_known=False,
            priced=False,
        )
    rates = rates_for(usage.model, pricing)
    if rates is None:
        logger.warning("model_pricing missing for %s; attempt unpriced", usage.model)
        return AttemptCost(
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=None,
            usage_known=True,
            priced=False,
        )
    assert usage.input_tokens is not None and usage.output_tokens is not None
    effective = effective_mtok_rates(rates, input_tokens=usage.input_tokens)
    if effective is None:
        logger.warning(
            "model_pricing placeholder for %s (%s); attempt unpriced",
            usage.model,
            rates["source"],
        )
        return AttemptCost(
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=None,
            usage_known=True,
            priced=False,
        )
    input_rate, output_rate = effective
    return AttemptCost(
        model=usage.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=attempt_cost_usd(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            input_usd_per_mtok=input_rate,
            output_usd_per_mtok=output_rate,
        ),
        usage_known=True,
        priced=True,
    )


def total_attempt_cost(
    attempts: list[AttemptCost] | tuple[AttemptCost, ...],
) -> float | None:
    """Sum every priced attempt. NULL if any attempt is unknown or unpriced."""
    if any(not attempt.priced or not attempt.usage_known for attempt in attempts):
        return None
    total = 0.0
    for attempt in attempts:
        assert attempt.cost_usd is not None
        total = total + attempt.cost_usd
    return total


def build_review_cost(
    *,
    run_id: str,
    account: int,
    usages: list[AttemptUsage],
    charged: bool,
    pricing: dict[str, ModelRates] | None = None,
) -> ReviewCostRecord:
    """Build the per-review cost record from every attempt usage."""
    attempts = tuple(build_attempt(usage, pricing=pricing) for usage in usages)
    return ReviewCostRecord(
        run_id=run_id,
        account=account,
        attempts=attempts,
        total_cost_usd=total_attempt_cost(attempts),
        charged=charged,
    )


def log_review_cost(record: ReviewCostRecord) -> None:
    """Emit one log line. No user-facing surface."""
    attempt_bits = [
        {
            "model": attempt.model,
            "input_tokens": attempt.input_tokens,
            "output_tokens": attempt.output_tokens,
            "cost_usd": attempt.cost_usd,
            "usage_known": attempt.usage_known,
            "priced": attempt.priced,
        }
        for attempt in record.attempts
    ]
    logger.info(
        "review_cost run_id=%s account=%s attempts=%s total_cost_usd=%s charged=%s",
        record.run_id,
        record.account,
        attempt_bits,
        record.total_cost_usd,
        record.charged,
    )


def record_review_cost(
    store: Store,
    *,
    run_id: str,
    account: int,
    usages: list[AttemptUsage],
    charged: bool,
    pricing: dict[str, ModelRates] | None = None,
) -> ReviewCostRecord:
    """Persist and log cost for one review. Failed runs pass charged=False."""
    table = MODEL_PRICING if pricing is None else pricing
    record = build_review_cost(
        run_id=run_id,
        account=account,
        usages=usages,
        charged=charged,
        pricing=table,
    )
    store.save_review_cost(record)
    log_review_cost(record)
    return record
