"""Behavioural cost logging for each review attempt.

These assertions are the mutation targets for K1:
- a retry must increase total_cost_usd by the second attempt's cost
- a failed run is stored with charged=False
- total_cost_usd equals the sum of per-attempt cost_usd
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from review_grade.api.model_pricing import MODEL_PRICING
from review_grade.api.review_cost import (
    AttemptCost,
    AttemptUsage,
    build_attempt,
    build_review_cost,
    record_review_cost,
    total_attempt_cost,
)
from review_grade.api.store import Store

# Test-only rates. Not production prices — production map stays TODO placeholders.
_TEST_PRICING = {
    "test-model": {
        "input_usd_per_mtok": 1.0,
        "output_usd_per_mtok": 2.0,
        "source": "test fixture — not a provider price",
    },
    "test-model-b": {
        "input_usd_per_mtok": 3.0,
        "output_usd_per_mtok": 4.0,
        "source": "test fixture — not a provider price",
    },
}


def _usage(
    model: str = "test-model",
    *,
    input_tokens: int,
    output_tokens: int,
) -> AttemptUsage:
    return AttemptUsage(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


class ReviewCostBehaviourTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / "cost.sqlite"))
        self.addCleanup(self.store.close)
        self.account = self.store.add_user("cost@reviewgrade.dev", 5)

    def test_retry_adds_to_the_cost(self) -> None:
        first = _usage(input_tokens=1_000_000, output_tokens=0)
        retry = _usage(input_tokens=0, output_tokens=1_000_000)
        first_only = build_review_cost(
            run_id="run_first",
            account=self.account,
            usages=[first],
            charged=True,
            pricing=_TEST_PRICING,
        )
        with_retry = build_review_cost(
            run_id="run_retry",
            account=self.account,
            usages=[first, retry],
            charged=True,
            pricing=_TEST_PRICING,
        )
        # 1M input @ $1/MTok = $1.00; 1M output @ $2/MTok = $2.00
        self.assertEqual(first_only.total_cost_usd, 1.0)
        self.assertEqual(len(with_retry.attempts), 2)
        self.assertEqual(with_retry.attempts[0].cost_usd, 1.0)
        self.assertEqual(with_retry.attempts[1].cost_usd, 2.0)
        self.assertEqual(with_retry.total_cost_usd, 3.0)
        self.assertGreater(with_retry.total_cost_usd, first_only.total_cost_usd)
        self.assertEqual(
            with_retry.total_cost_usd,
            first_only.total_cost_usd + with_retry.attempts[1].cost_usd,
        )
        # Dropping the retry from the total must fail this test.
        self.assertNotEqual(with_retry.total_cost_usd, first_only.total_cost_usd)

    def test_failed_run_is_recorded_and_not_charged(self) -> None:
        usages = [
            _usage(input_tokens=500_000, output_tokens=0),
            _usage(input_tokens=500_000, output_tokens=250_000),
        ]
        with self.assertLogs("review_grade.api.review_cost", level=logging.INFO) as logged:
            record = record_review_cost(
                self.store,
                run_id="run_failed_absorb",
                account=self.account,
                usages=usages,
                charged=False,
                pricing=_TEST_PRICING,
            )
        self.assertFalse(record.charged)
        self.assertEqual(len(record.attempts), 2)
        self.assertGreater(record.total_cost_usd, 0.0)
        stored = self.store.get_review_cost("run_failed_absorb")
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored["run_id"], "run_failed_absorb")
        self.assertEqual(stored["account"], self.account)
        self.assertEqual(len(stored["attempts"]), 2)
        self.assertEqual(stored["total_cost_usd"], record.total_cost_usd)
        self.assertFalse(stored["charged"])
        self.assertTrue(any("charged=False" in line for line in logged.output))
        # Charging the customer on a failed absorb must fail this test.
        self.assertIs(record.charged, False)
        self.assertIs(stored["charged"], False)

    def test_totals_add_up_across_attempts(self) -> None:
        usages = [
            _usage("test-model", input_tokens=100_000, output_tokens=50_000),
            _usage("test-model-b", input_tokens=200_000, output_tokens=25_000),
            _usage("test-model", input_tokens=0, output_tokens=100_000),
        ]
        record = build_review_cost(
            run_id="run_sum",
            account=self.account,
            usages=usages,
            charged=True,
            pricing=_TEST_PRICING,
        )
        per_attempt = [build_attempt(usage, pricing=_TEST_PRICING) for usage in usages]
        expected = (
            per_attempt[0].cost_usd
            + per_attempt[1].cost_usd
            + per_attempt[2].cost_usd
        )
        self.assertEqual(len(record.attempts), 3)
        self.assertAlmostEqual(record.total_cost_usd, expected, places=12)
        self.assertAlmostEqual(total_attempt_cost(record.attempts), expected, places=12)
        self.assertAlmostEqual(
            total_attempt_cost(record.attempts),
            sum(attempt.cost_usd for attempt in record.attempts),
            places=12,
        )
        # Keeping only the last attempt's cost must fail this test.
        self.assertNotAlmostEqual(record.total_cost_usd, per_attempt[-1].cost_usd)
        self.assertNotAlmostEqual(record.total_cost_usd, per_attempt[0].cost_usd)

    def test_production_pricing_map_is_todo_placeholders(self) -> None:
        self.assertIn("grok-4.7-build-fast", MODEL_PRICING)
        for model, rates in MODEL_PRICING.items():
            with self.subTest(model=model):
                self.assertIsNone(rates["input_usd_per_mtok"])
                self.assertIsNone(rates["output_usd_per_mtok"])
                self.assertTrue(str(rates["source"]).startswith("TODO:"))

    def test_store_round_trip_keeps_attempt_costs(self) -> None:
        usages = [
            _usage(input_tokens=1_000_000, output_tokens=500_000),
            _usage(input_tokens=250_000, output_tokens=0),
        ]
        record = record_review_cost(
            self.store,
            run_id="run_done_charge",
            account=self.account,
            usages=usages,
            charged=True,
            pricing=_TEST_PRICING,
        )
        stored = self.store.get_review_cost("run_done_charge")
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertTrue(stored["charged"])
        self.assertEqual(stored["total_cost_usd"], 1.0 + 1.0 + 0.25)
        self.assertEqual(
            [row["cost_usd"] for row in stored["attempts"]],
            [attempt.cost_usd for attempt in record.attempts],
        )


class TotalAttemptCostMutationTests(unittest.TestCase):
    def test_sum_includes_every_attempt(self) -> None:
        attempts = (
            AttemptCost("m", 1, 0, 0.25),
            AttemptCost("m", 1, 0, 0.50),
            AttemptCost("m", 1, 0, 0.125),
        )
        self.assertEqual(total_attempt_cost(attempts), 0.875)
        self.assertEqual(total_attempt_cost([]), 0.0)
        # Omitting any attempt must fail this test.
        self.assertNotEqual(
            total_attempt_cost(attempts),
            attempts[0].cost_usd + attempts[1].cost_usd,
        )


if __name__ == "__main__":
    unittest.main()
