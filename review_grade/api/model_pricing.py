"""Per-model token prices for internal review cost logging.

Rates are USD per 1,000,000 tokens from official provider or Vercel AI
Gateway pricing pages (accessed 2026-10-21). None means unknown — see
UNKNOWN_PRICING. Cached-input rates are omitted: review_cost only prices
input/output tokens, and the reviewer path does not log cache hits.
"""

from __future__ import annotations

from typing import TypedDict


class ModelRates(TypedDict):
    # USD per 1,000,000 tokens. None means not yet filled from an official page.
    input_usd_per_mtok: float | None
    output_usd_per_mtok: float | None
    source: str


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
    # Grok Build / Cursor Fast variant — not listed on Vercel AI Gateway;
    # short-context (<200k prompt) Fast rates from xAI pricing.
    "grok-4.7-build-fast": {
        "input_usd_per_mtok": 4.0,  # https://docs.x.ai/developers/pricing (Grok 4.7 Fast, <200k) accessed 2026-10-21
        "output_usd_per_mtok": 12.0,  # https://docs.x.ai/developers/pricing (Grok 4.7 Fast, <200k) accessed 2026-10-21
        "source": "https://docs.x.ai/developers/pricing (Grok 4.7 Fast) accessed 2026-10-21",
    },
    # Stress / Sonnet rerun only (review_grade/reviewer.py:17 SONNET_MODEL).
    # Maps to anthropic/claude-sonnet-5.5 on Vercel AI Gateway.
    "claude-sonnet-5-5": {
        "input_usd_per_mtok": 2.0,  # https://vercel.com/ai-gateway/models/claude-sonnet-5.5 accessed 2026-10-21
        "output_usd_per_mtok": 10.0,  # https://vercel.com/ai-gateway/models/claude-sonnet-5.5 accessed 2026-10-21
        "source": "https://vercel.com/ai-gateway/models/claude-sonnet-5.5 accessed 2026-10-21",
    },
    # Haiku stress run only (review_grade/reviewer.py:19 HAIKU_MODEL).
    # Maps to anthropic/claude-haiku-4.5 on Vercel AI Gateway.
    "claude-haiku-4-5-20251001": {
        "input_usd_per_mtok": 1.0,  # https://vercel.com/ai-gateway/models/claude-haiku-4.5 accessed 2026-10-21
        "output_usd_per_mtok": 5.0,  # https://vercel.com/ai-gateway/models/claude-haiku-4.5 accessed 2026-10-21
        "source": "https://vercel.com/ai-gateway/models/claude-haiku-4.5 accessed 2026-10-21",
    },
}


def rates_for(model: str, pricing: dict[str, ModelRates] | None = None) -> ModelRates | None:
    table = MODEL_PRICING if pricing is None else pricing
    return table.get(model)
