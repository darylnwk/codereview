"""Internal model/token cost for each review.

Customer-facing run.cost_usd stays the projected line price. This module
records what the provider call actually cost us: every attempt (including
retries and failed runs we absorb), whether the customer was charged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from review_grade.api.model_pricing import MODEL_PRICING, ModelRates, rates_for

if TYPE_CHECKING:
    from review_grade.api.store import Store

logger = logging.getLogger("review_grade.api.review_cost")

_TOKENS_PER_MTOK = 1_000_000


@dataclass(frozen=True)
class AttemptUsage:
    model: str
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class AttemptCost:
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


@dataclass(frozen=True)
class ReviewCostRecord:
    """One finished review's absorbed provider cost."""

    run_id: str
    account: int
    attempts: tuple[AttemptCost, ...]
    total_cost_usd: float
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
    """Price one attempt from the pricing map.

    TODO placeholder rates (None) yield cost_usd 0.0 so the attempt is still
    recorded. Callers that need real dollars must fill MODEL_PRICING first.
    """
    rates = rates_for(usage.model, pricing)
    if rates is None:
        raise KeyError(f"no pricing entry for model {usage.model!r}")
    input_rate = rates["input_usd_per_mtok"]
    output_rate = rates["output_usd_per_mtok"]
    if input_rate is None or output_rate is None:
        logger.warning(
            "model_pricing placeholder for %s (%s); attempt cost stored as 0.0",
            usage.model,
            rates["source"],
        )
        cost = 0.0
    else:
        cost = attempt_cost_usd(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            input_usd_per_mtok=input_rate,
            output_usd_per_mtok=output_rate,
        )
    return AttemptCost(
        model=usage.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=cost,
    )


def total_attempt_cost(attempts: list[AttemptCost] | tuple[AttemptCost, ...]) -> float:
    """Sum every attempt. A retry must increase this; dropping attempts fails callers."""
    total = 0.0
    for attempt in attempts:
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
