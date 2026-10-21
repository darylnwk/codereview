"""Behavioural cost logging for each review attempt.

Helper tests cover NULL-vs-zero pricing. Settle/review_diff tests cover the
worker paths and the mutants called out in the K1 send-back.
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from review_grade.api.model_pricing import MODEL_PRICING
from review_grade.api.review_cost import (
    AttemptCost,
    AttemptUsage,
    ReviewCostRecord,
    build_attempt,
    build_review_cost,
    known_usage,
    record_review_cost,
    total_attempt_cost,
    unknown_usage,
)
from review_grade.api.store import Store, format_time
from review_grade.api.worker import settle_one
from review_grade.reviewer import ProviderError, _usage_from_cli_body, review_diff
from review_grade.rubric import assemble_prompt

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

_FINDING = {
    "file": "billing/charge.py",
    "line": 14,
    "what_breaks": "The charge skips the zero-cent guard.",
    "fix": "Reject a charge of zero cents.",
}


def _usage(
    model: str = "test-model",
    *,
    input_tokens: int,
    output_tokens: int,
) -> AttemptUsage:
    return known_usage(model, input_tokens=input_tokens, output_tokens=output_tokens)


def _tiny_diff(name: str = "a.py") -> str:
    return (
        f"diff --git a/{name} b/{name}\n"
        f"--- a/{name}\n"
        f"+++ b/{name}\n"
        "+ok\n"
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
        self.assertEqual(first_only.total_cost_usd, 1.0)
        self.assertEqual(len(with_retry.attempts), 2)
        self.assertEqual(with_retry.attempts[0].cost_usd, 1.0)
        self.assertEqual(with_retry.attempts[1].cost_usd, 2.0)
        self.assertTrue(with_retry.attempts[0].priced)
        self.assertEqual(with_retry.total_cost_usd, 3.0)
        self.assertGreater(with_retry.total_cost_usd, first_only.total_cost_usd)
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
        self.assertNotAlmostEqual(record.total_cost_usd, per_attempt[-1].cost_usd)
        self.assertNotAlmostEqual(record.total_cost_usd, per_attempt[0].cost_usd)

    def test_unknown_usage_and_unpriced_rates_store_null(self) -> None:
        unknown = build_attempt(unknown_usage("test-model"), pricing=_TEST_PRICING)
        self.assertIsNone(unknown.input_tokens)
        self.assertIsNone(unknown.output_tokens)
        self.assertIsNone(unknown.cost_usd)
        self.assertFalse(unknown.usage_known)
        self.assertFalse(unknown.priced)

        zero = build_attempt(
            known_usage("test-model", input_tokens=0, output_tokens=0),
            pricing=_TEST_PRICING,
        )
        self.assertEqual(zero.input_tokens, 0)
        self.assertEqual(zero.output_tokens, 0)
        self.assertEqual(zero.cost_usd, 0.0)
        self.assertTrue(zero.usage_known)
        self.assertTrue(zero.priced)

        todo = build_attempt(
            known_usage("grok-4.7-build-fast", input_tokens=100, output_tokens=0),
            pricing=MODEL_PRICING,
        )
        self.assertEqual(todo.input_tokens, 100)
        self.assertIsNone(todo.cost_usd)
        self.assertTrue(todo.usage_known)
        self.assertFalse(todo.priced)

        missing = build_attempt(
            known_usage("no-such-model", input_tokens=10, output_tokens=1),
            pricing=_TEST_PRICING,
        )
        self.assertEqual(missing.input_tokens, 10)
        self.assertIsNone(missing.cost_usd)
        self.assertFalse(missing.priced)

        mixed = build_review_cost(
            run_id="run_mixed",
            account=self.account,
            usages=[
                known_usage("test-model", input_tokens=1_000_000, output_tokens=0),
                unknown_usage("test-model"),
            ],
            charged=False,
            pricing=_TEST_PRICING,
        )
        self.assertIsNone(mixed.total_cost_usd)
        self.assertFalse(mixed.attempts[1].usage_known)

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
        self.assertTrue(all(row["priced"] for row in stored["attempts"]))
        self.assertTrue(all(row["usage_known"] for row in stored["attempts"]))

    def test_review_costs_user_id_index_exists(self) -> None:
        rows = self.store._conn.execute("PRAGMA index_list(review_costs)").fetchall()
        names = {str(row["name"]) for row in rows}
        self.assertIn("review_costs_user_id", names)


class TotalAttemptCostMutationTests(unittest.TestCase):
    def test_sum_includes_every_attempt(self) -> None:
        attempts = (
            AttemptCost("m", 1, 0, 0.25, True, True),
            AttemptCost("m", 1, 0, 0.50, True, True),
            AttemptCost("m", 1, 0, 0.125, True, True),
        )
        self.assertEqual(total_attempt_cost(attempts), 0.875)
        self.assertEqual(total_attempt_cost([]), 0.0)
        self.assertNotEqual(
            total_attempt_cost(attempts),
            attempts[0].cost_usd + attempts[1].cost_usd,
        )

    def test_unknown_attempt_makes_total_null(self) -> None:
        attempts = (
            AttemptCost("m", 1, 0, 0.25, True, True),
            AttemptCost("m", None, None, None, False, False),
        )
        self.assertIsNone(total_attempt_cost(attempts))


class SettleAndReviewDiffCostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / "settle.sqlite"))
        self.addCleanup(self.store.close)
        secret = self.store.seed_account(5)
        key = self.store.authenticate(secret)
        assert key is not None
        self.key = key

    def _queue(self, *, created_at: str | None = None, name: str = "a.py") -> str:
        return self.store.insert_queued(
            user_id=self.key.user_id,
            key_id=self.key.id,
            diff_hash="d" * 64,
            diff_text=_tiny_diff(name),
            source="api",
            idempotency_key=None,
            created_at=created_at,
        )

    def _cost(self, run_id: str) -> dict:
        stored = self.store.get_review_cost(run_id)
        self.assertIsNotNone(stored, "cost record missing for " + run_id)
        assert stored is not None
        return stored

    def test_review_diff_retry_records_failed_then_success(self) -> None:
        calls: list[str] = []

        def once(prompt: str, *, model: str, timeout_s: int):
            if not calls:
                calls.append("fail")
                raise ProviderError("transient non-json")
            calls.append("ok")
            return (
                dict(_FINDING),
                known_usage(model, input_tokens=12, output_tokens=3),
            )

        usages: list[AttemptUsage] = []
        with patch("review_grade.reviewer._review_diff_once", once):
            payload = review_diff(
                assemble_prompt(_tiny_diff()),
                model="test-model",
                attempt_usages=usages,
            )
        self.assertEqual(payload, _FINDING)
        self.assertEqual(calls, ["fail", "ok"])
        self.assertEqual(len(usages), 2)
        self.assertFalse(usages[0].usage_known)
        self.assertIsNone(usages[0].input_tokens)
        self.assertTrue(usages[1].usage_known)
        self.assertEqual(usages[1].input_tokens, 12)
        self.assertEqual(usages[1].output_tokens, 3)

        run_id = self._queue(name="retry.py")

        def live(diff: str, attempt_usages: list | None = None) -> object:
            return review_diff(
                assemble_prompt(diff),
                model="test-model",
                attempt_usages=attempt_usages,
            )

        calls.clear()
        with patch("review_grade.reviewer._review_diff_once", once):
            self.assertTrue(settle_one(self.store, live))
        stored = self._cost(run_id)
        self.assertEqual(len(stored["attempts"]), 2)
        self.assertFalse(stored["attempts"][0]["usage_known"])
        self.assertTrue(stored["attempts"][1]["usage_known"])
        self.assertIsNone(stored["total_cost_usd"])
        self.assertTrue(stored["charged"])

    def test_settle_expiry_writes_uncharged_record(self) -> None:
        old = format_time(datetime.now(UTC) - timedelta(minutes=4))
        run_id = self._queue(created_at=old, name="old.py")

        def finding(_diff: str) -> dict:
            return dict(_FINDING)

        self.assertTrue(settle_one(self.store, finding))
        stored = self._cost(run_id)
        self.assertFalse(stored["charged"])
        self.assertGreaterEqual(len(stored["attempts"]), 1)
        self.assertFalse(stored["attempts"][0]["usage_known"])
        row = self.store.get_run(self.key.user_id, run_id)
        self.assertEqual(row["status"], "failed")

    def test_settle_provider_error_writes_uncharged_record(self) -> None:
        run_id = self._queue(name="err.py")

        def boom(_diff: str) -> object:
            raise ProviderError("fixture reviewer failed")

        self.assertTrue(settle_one(self.store, boom))
        stored = self._cost(run_id)
        self.assertFalse(stored["charged"])
        self.assertIs(stored["charged"], False)
        row = self.store.get_run(self.key.user_id, run_id)
        self.assertEqual(row["status"], "failed")

    def test_settle_bad_payload_writes_uncharged_record(self) -> None:
        run_id = self._queue(name="bad.py")

        def bad(_diff: str) -> str:
            return "4 of 6"

        self.assertTrue(settle_one(self.store, bad))
        stored = self._cost(run_id)
        self.assertFalse(stored["charged"])
        row = self.store.get_run(self.key.user_id, run_id)
        self.assertEqual(row["status"], "failed")

    def test_settle_exception_writes_uncharged_record(self) -> None:
        run_id = self._queue(name="exc.py")

        def explode(_diff: str) -> object:
            raise RuntimeError("unexpected worker blow-up")

        self.assertTrue(settle_one(self.store, explode))
        stored = self._cost(run_id)
        self.assertFalse(stored["charged"])
        self.assertIs(stored["charged"], False)
        self.assertGreaterEqual(len(stored["attempts"]), 1)
        row = self.store.get_run(self.key.user_id, run_id)
        self.assertEqual(row["status"], "failed")

    def test_settle_unknown_model_does_not_crash_and_is_unpriced(self) -> None:
        run_id = self._queue(name="unk.py")
        usages_box: list[list] = []

        def once(prompt: str, *, model: str, timeout_s: int):
            return (
                dict(_FINDING),
                known_usage("totally-unknown-model", input_tokens=9, output_tokens=1),
            )

        def live(diff: str, attempt_usages: list | None = None) -> object:
            if attempt_usages is not None:
                usages_box.append(attempt_usages)
            return review_diff(
                assemble_prompt(diff),
                model="totally-unknown-model",
                attempt_usages=attempt_usages,
            )

        with patch("review_grade.reviewer._review_diff_once", once):
            self.assertTrue(settle_one(self.store, live))
        stored = self._cost(run_id)
        self.assertTrue(stored["charged"])
        self.assertEqual(stored["attempts"][0]["model"], "totally-unknown-model")
        self.assertTrue(stored["attempts"][0]["usage_known"])
        self.assertFalse(stored["attempts"][0]["priced"])
        self.assertIsNone(stored["attempts"][0]["cost_usd"])
        self.assertIsNone(stored["total_cost_usd"])

    def test_call_review_does_not_double_invoke_fixture(self) -> None:
        run_id = self._queue(name="once.py")
        hits = {"n": 0}

        def finding(_diff: str) -> dict:
            hits["n"] += 1
            return dict(_FINDING)

        self.assertTrue(settle_one(self.store, finding))
        self.assertEqual(hits["n"], 1)
        self._cost(run_id)

    def test_absorb_paths_keep_charged_false(self) -> None:
        """Mutant: charged=True after mark_failed must fail these paths."""
        cases = []

        def error(_diff: str) -> object:
            raise ProviderError("nope")

        def bad(_diff: str) -> str:
            return "4 of 6"

        def explode(_diff: str) -> object:
            raise RuntimeError("boom")

        for name, review in (("e", error), ("b", bad), ("x", explode)):
            run_id = self._queue(name=name + ".py")
            settle_one(self.store, review)
            cases.append(self._cost(run_id))
        for stored in cases:
            self.assertIs(stored["charged"], False)

    def test_lost_claim_race_does_not_overwrite_winner_cost(self) -> None:
        """Loser mark_done='lost' must not clobber the winner's cost row."""
        run_id = self._queue(name="race.py")

        def winner(_diff: str) -> dict:
            return dict(_FINDING)

        def loser(_diff: str) -> dict:
            # First settler owns the claim; release so a second settler can win.
            self.store.release_queued_claims()
            self.assertTrue(settle_one(self.store, winner))
            winner_cost = self._cost(run_id)
            self.assertTrue(winner_cost["charged"])
            self.assertEqual(
                self.store.get_run(self.key.user_id, run_id)["status"],
                "done",
            )
            return dict(_FINDING)

        self.assertTrue(settle_one(self.store, loser))
        stored = self._cost(run_id)
        self.assertIs(stored["charged"], True)
        self.assertEqual(self.store.get_run(self.key.user_id, run_id)["status"], "done")
        # Insert-only: a forged loser write must not replace the winner either.
        self.store.save_review_cost(
            ReviewCostRecord(
                run_id=run_id,
                account=self.key.user_id,
                attempts=(),
                total_cost_usd=None,
                charged=False,
            )
        )
        again = self._cost(run_id)
        self.assertIs(again["charged"], True)

    def test_save_review_cost_raise_does_not_undo_settle(self) -> None:
        run_id = self._queue(name="raise.py")

        def finding(_diff: str) -> dict:
            return dict(_FINDING)

        with patch.object(
            self.store,
            "save_review_cost",
            side_effect=RuntimeError("disk full"),
        ):
            self.assertTrue(settle_one(self.store, finding))
        row = self.store.get_run(self.key.user_id, run_id)
        self.assertEqual(row["status"], "done")
        self.assertIsNone(self.store.get_review_cost(run_id))


class CliUsageEnvelopeTests(unittest.TestCase):
    def test_missing_or_null_token_fields_are_unknown_not_zero(self) -> None:
        cases = [
            {"text": "{}"},
            {"usage": None},
            {"usage": {}},
            {"usage": {"input_tokens": None, "output_tokens": None}},
            {"usage": {"input_tokens": 10}},
            {"usage": {"output_tokens": 4}},
            {"usage": {"input_tokens": None, "output_tokens": 4}},
            {"usage": {"prompt_tokens": None, "completion_tokens": 1}},
        ]
        for body in cases:
            with self.subTest(body=body):
                usage = _usage_from_cli_body(body, model="test-model")
                self.assertFalse(usage.usage_known)
                self.assertIsNone(usage.input_tokens)
                self.assertIsNone(usage.output_tokens)
                # Mutant `int(raw or 0)` must fail: unknown stays None, not 0.
                self.assertNotEqual(usage.input_tokens, 0)
                self.assertNotEqual(usage.output_tokens, 0)

    def test_explicit_zero_tokens_are_known(self) -> None:
        usage = _usage_from_cli_body(
            {"usage": {"input_tokens": 0, "output_tokens": 0}},
            model="test-model",
        )
        self.assertTrue(usage.usage_known)
        self.assertEqual(usage.input_tokens, 0)
        self.assertEqual(usage.output_tokens, 0)


class KnownUsageValidationTests(unittest.TestCase):
    def test_known_usage_rejects_negative_token_counts(self) -> None:
        with self.assertRaises(ValueError):
            known_usage("test-model", input_tokens=-1, output_tokens=0)
        with self.assertRaises(ValueError):
            known_usage("test-model", input_tokens=0, output_tokens=-2)


if __name__ == "__main__":
    unittest.main()
