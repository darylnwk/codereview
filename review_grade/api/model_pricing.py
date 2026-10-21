"""Per-model token prices for internal review cost logging.

Rates are list-price estimates (USD per 1,000,000 tokens) taken from
public pricing pages (accessed 2026-10-21). They are not billed amounts:
product calls go through the logged-in `grok` and `claude` CLIs, which
bill via those plans. None means unknown — see UNKNOWN_PRICING.
Cached-input rates are omitted: review_cost only prices input/output
tokens, and the reviewer path does not log cache hits.
"""

from __future__ import annotations

from typing import NotRequired, TypedDict


class ModelRates(TypedDict):
    # USD per 1,000,000 tokens. None means not yet filled from an official page.
    input_usd_per_mtok: float | None
    output_usd_per_mtok: float | None
    source: str
    # Optional long-prompt tier: when input_tokens > long_prompt_after_input_tokens,
    # use long_prompt_* rates instead of the base pair.
    long_prompt_after_input_tokens: NotRequired[int]
    long_prompt_input_usd_per_mtok: NotRequired[float]
    long_prompt_output_usd_per_mtok: NotRequired[float]


# Models that stay in MODEL_PRICING with None rates until an official page
# lists the exact product model id (or an unambiguous mapping). Empty when
# every configured model has filled rates.
UNKNOWN_PRICING: frozenset[str] = frozenset()


# One map for every reviewer model id the product may call.
# Call sites: review_grade/reviewer.py (REVIEWER_MODEL / SONNET_MODEL /
# HAIKU_MODEL) and review_grade/api/worker.py (live path → REVIEWER_MODEL).
MODEL_PRICING: dict[str, ModelRates] = {
    # Default product reviewer (review_grade/reviewer.py:15 REVIEWER_MODEL;
    # worker live path review_grade/api/worker.py:66-69 → review_diff default).
    # Maps to the page's "Grok 4.7 Fast" row (exact id not listed). Plan-billed
    # via Grok Build; figures below are list-price estimates, not billed amounts.
    # Base = short prompt (≤200k input); long_prompt_* when input_tokens > 200k.
    "grok-4.7-build-fast": {
        "input_usd_per_mtok": 4.0,  # list-price estimate: https://docs.x.ai/developers/pricing (Grok 4.7 Fast, ≤200k) accessed 2026-10-21
        "output_usd_per_mtok": 12.0,  # list-price estimate: https://docs.x.ai/developers/pricing (Grok 4.7 Fast, ≤200k) accessed 2026-10-21
        "long_prompt_after_input_tokens": 200_000,
        "long_prompt_input_usd_per_mtok": 6.0,  # list-price estimate: https://docs.x.ai/developers/pricing (Grok 4.7 Fast, >200k) accessed 2026-10-21
        "long_prompt_output_usd_per_mtok": 18.0,  # list-price estimate: https://docs.x.ai/developers/pricing (Grok 4.7 Fast, >200k) accessed 2026-10-21
        "source": "list-price estimate https://docs.x.ai/developers/pricing (Grok 4.7 Fast; exact id not listed; Grok Build plan-billed) accessed 2026-10-21",
    },
    # Stress / Sonnet rerun only (review_grade/reviewer.py:17 SONNET_MODEL).
    # Called via the logged-in `claude` CLI. List-price estimate from the
    # public model page (not a Vercel AI Gateway billed amount).
    "claude-sonnet-5-5": {
        "input_usd_per_mtok": 2.0,  # list-price estimate: https://vercel.com/ai-gateway/models/claude-sonnet-5.5 accessed 2026-10-21
        "output_usd_per_mtok": 10.0,  # list-price estimate: https://vercel.com/ai-gateway/models/claude-sonnet-5.5 accessed 2026-10-21
        "source": "list-price estimate https://vercel.com/ai-gateway/models/claude-sonnet-5.5 (claude CLI; not gateway-billed) accessed 2026-10-21",
    },
    # Haiku stress run only (review_grade/reviewer.py:19 HAIKU_MODEL).
    # Called via the logged-in `claude` CLI. List-price estimate from the
    # public model page (not a Vercel AI Gateway billed amount).
    "claude-haiku-4-5-20251001": {
        "input_usd_per_mtok": 1.0,  # list-price estimate: https://vercel.com/ai-gateway/models/claude-haiku-4.5 accessed 2026-10-21
        "output_usd_per_mtok": 5.0,  # list-price estimate: https://vercel.com/ai-gateway/models/claude-haiku-4.5 accessed 2026-10-21
        "source": "list-price estimate https://vercel.com/ai-gateway/models/claude-haiku-4.5 (claude CLI; not gateway-billed) accessed 2026-10-21",
    },
}


def rates_for(model: str, pricing: dict[str, ModelRates] | None = None) -> ModelRates | None:
    table = MODEL_PRICING if pricing is None else pricing
    return table.get(model)


def effective_mtok_rates(
    rates: ModelRates,
    *,
    input_tokens: int,
) -> tuple[float, float] | None:
    """Base or long-prompt (input, output) $/MTok. None if either base rate is unset."""
    input_rate = rates["input_usd_per_mtok"]
    output_rate = rates["output_usd_per_mtok"]
    if input_rate is None or output_rate is None:
        return None
    threshold = rates.get("long_prompt_after_input_tokens")
    if threshold is not None and input_tokens > threshold:
        long_in = rates.get("long_prompt_input_usd_per_mtok")
        long_out = rates.get("long_prompt_output_usd_per_mtok")
        if long_in is not None and long_out is not None:
            return long_in, long_out
    return input_rate, output_rate
