"""Per-model token prices for internal review cost logging.

Values are TODO placeholders. Do not treat them as real provider prices.
Fill each entry from the provider's public pricing page before relying on
dollar totals in production.
"""

from __future__ import annotations

from typing import TypedDict


class ModelRates(TypedDict):
    # USD per 1,000,000 tokens. None means not yet filled from the provider page.
    input_usd_per_mtok: float | None
    output_usd_per_mtok: float | None
    source: str


# One map for every reviewer model id the product may call.
# TODO: replace None rates with figures from the linked pricing pages.
MODEL_PRICING: dict[str, ModelRates] = {
    # Default product reviewer (see review_grade.reviewer.REVIEWER_MODEL).
    "grok-4.7-build-fast": {
        "input_usd_per_mtok": None,  # TODO: https://x.ai/api (or current Grok pricing page)
        "output_usd_per_mtok": None,  # TODO: https://x.ai/api (or current Grok pricing page)
        "source": "TODO: https://x.ai/api",
    },
    # Internal stress / strong-model paths only.
    "claude-sonnet-5-5": {
        "input_usd_per_mtok": None,  # TODO: https://www.anthropic.com/pricing
        "output_usd_per_mtok": None,  # TODO: https://www.anthropic.com/pricing
        "source": "TODO: https://www.anthropic.com/pricing",
    },
    "claude-haiku-4-5-20251001": {
        "input_usd_per_mtok": None,  # TODO: https://www.anthropic.com/pricing
        "output_usd_per_mtok": None,  # TODO: https://www.anthropic.com/pricing
        "source": "TODO: https://www.anthropic.com/pricing",
    },
}


def rates_for(model: str, pricing: dict[str, ModelRates] | None = None) -> ModelRates | None:
    table = MODEL_PRICING if pricing is None else pricing
    return table.get(model)
