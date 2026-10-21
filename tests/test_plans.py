"""Plan prices, overage, monthly cap, and no legacy per-email trial."""

# ruff: noqa: SIM115

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from review_grade.api.plans import (
    ANNUAL_PRICING,
    DEFAULT_MONTHLY_CAP_USD,
    DEVELOPER,
    FLEET,
    PLANS,
    TRIAL_DAYS,
    allow_next_review,
    charge_for_review,
    get_plan,
    has_legacy_trial_plan,
    monthly_spend_usd,
    overage_reviews,
    overage_spend_usd,
    within_monthly_cap,
)
from review_grade.api.store import Store
from review_grade.api.worker import reviewer_for, settle_one

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _added(path: str, count: int, marker: str) -> str:
    lines = [
        f"diff --git a/{path} b/{path}\n",
        f"--- a/{path}\n",
        f"+++ b/{path}\n",
    ]
    lines.extend(f"+{marker}{index}\n" for index in range(count))
    return "".join(lines)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class PlanMathTests(unittest.TestCase):
    def test_developer_and_fleet_included_and_prices(self) -> None:
        self.assertEqual(DEVELOPER.monthly_price_usd, 25)
        self.assertEqual(DEVELOPER.included_reviews, 250)
        self.assertEqual(DEVELOPER.overage_usd, 0.15)
        self.assertEqual(FLEET.monthly_price_usd, 199)
        self.assertEqual(FLEET.included_reviews, 2500)
        self.assertEqual(FLEET.overage_usd, 0.06)
        self.assertEqual(set(PLANS), {"developer", "fleet"})
        self.assertIs(get_plan("developer"), DEVELOPER)
        self.assertIs(get_plan("fleet"), FLEET)

    def test_no_legacy_twenty_review_trial(self) -> None:
        self.assertFalse(has_legacy_trial_plan())
        self.assertNotIn("trial", PLANS)
        for plan in PLANS.values():
            self.assertNotEqual(plan.included_reviews, 20)
            self.assertNotEqual(plan.monthly_price_usd, 0)
        self.assertEqual(TRIAL_DAYS, 30)
        self.assertFalse(ANNUAL_PRICING)
        self.assertEqual(DEFAULT_MONTHLY_CAP_USD, 50)

    def test_included_reviews_have_no_overage_charge(self) -> None:
        self.assertEqual(overage_reviews(DEVELOPER, 0), 0)
        self.assertEqual(overage_reviews(DEVELOPER, 250), 0)
        self.assertEqual(overage_reviews(DEVELOPER, 251), 1)
        self.assertEqual(charge_for_review(DEVELOPER, 0, status="done"), 0.0)
        self.assertEqual(charge_for_review(DEVELOPER, 249, status="done"), 0.0)
        self.assertEqual(charge_for_review(DEVELOPER, 250, status="done"), 0.15)
        self.assertEqual(charge_for_review(FLEET, 2499, status="done"), 0.0)
        self.assertEqual(charge_for_review(FLEET, 2500, status="done"), 0.06)

    def test_overage_rates_and_monthly_spend(self) -> None:
        self.assertEqual(overage_spend_usd(DEVELOPER, 250), 0.0)
        self.assertAlmostEqual(overage_spend_usd(DEVELOPER, 260), 1.5)
        self.assertAlmostEqual(overage_spend_usd(FLEET, 2600), 6.0)
        self.assertEqual(monthly_spend_usd(DEVELOPER, 36), 25.0)
        self.assertAlmostEqual(monthly_spend_usd(DEVELOPER, 260), 26.5)
        self.assertEqual(monthly_spend_usd(FLEET, 100), 199.0)

    def test_failed_runs_are_free(self) -> None:
        self.assertEqual(charge_for_review(DEVELOPER, 0, status="failed"), 0.0)
        self.assertEqual(charge_for_review(DEVELOPER, 300, status="failed"), 0.0)
        self.assertEqual(charge_for_review(FLEET, 3000, status="failed"), 0.0)

    def test_user_cap_enforcement(self) -> None:
        # Cap $50 on Developer: floor(50/0.15)=333 extras → finished up to 250+333=583.
        self.assertTrue(within_monthly_cap(DEVELOPER, 583, 50))
        self.assertFalse(within_monthly_cap(DEVELOPER, 584, 50))
        self.assertTrue(allow_next_review(DEVELOPER, 582, 50))
        self.assertFalse(allow_next_review(DEVELOPER, 583, 50))
        self.assertTrue(allow_next_review(DEVELOPER, 249, 0))
        self.assertFalse(allow_next_review(DEVELOPER, 250, 0))
        # Fleet $0.06: floor(50/0.06)=833 extras → 2500+833=3333.
        self.assertTrue(within_monthly_cap(FLEET, 3333, 50))
        self.assertFalse(within_monthly_cap(FLEET, 3334, 50))
        self.assertFalse(allow_next_review(FLEET, 3333, 50))


class PlanStoreAndApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "plans.sqlite")

    def seed(self, credits: int) -> str:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "review_grade.api.seed",
                "--db",
                self.db,
                "--credits",
                str(credits),
            ],
            cwd=_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout[:-1]

    def open_quiet_server(self) -> int:
        port = _free_port()
        err = tempfile.TemporaryFile()
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "review_grade.api",
                "--db",
                self.db,
                "--port",
                str(port),
                "--no-worker",
            ],
            cwd=_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=err,
        )

        def stop() -> None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            err.close()

        self.addCleanup(stop)
        deadline = time.time() + 10
        while time.time() < deadline:
            if proc.poll() is not None:
                err.seek(0)
                raise RuntimeError(err.read().decode("utf-8", errors="replace"))
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return port
            except OSError:
                time.sleep(0.02)
        raise RuntimeError("server did not listen")

    def exchange(self, port, method, path, token=None, payload=None):
        headers = {}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read().decode("utf-8"))
            finally:
                error.close()

    def test_cap_reached_refuses_before_queue(self) -> None:
        key = self.seed(5)
        store = Store(self.db)
        try:
            user_id = store.authenticate(key).user_id
            store.set_billing(user_id, reviews_finished=250, monthly_cap_usd=0)
        finally:
            store.close()
        port = self.open_quiet_server()
        status, body = self.exchange(
            port,
            "POST",
            "/v1/reviews",
            key,
            {"diff": _added("billing/charge.py", 1, "c")},
        )
        self.assertEqual(status, 402)
        self.assertEqual(body["error"], "cap_reached")
        connection = sqlite3.connect(self.db)
        try:
            count = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 0)

    def test_done_increments_finished_failed_does_not(self) -> None:
        store = Store(self.db)
        self.addCleanup(store.close)
        key = store.seed_account(5)
        user_id = store.authenticate(key).user_id
        run_ok = store.insert_queued(
            user_id=user_id,
            key_id=store.authenticate(key).id,
            diff_hash="a" * 64,
            diff_text=_added("ok.py", 1, "a"),
            source="api",
            idempotency_key=None,
        )
        self.assertTrue(settle_one(store, reviewer_for("finding")))
        self.assertEqual(store.billing(user_id).reviews_finished, 1)
        row = store.get_run(user_id, run_ok)
        self.assertEqual(row["status"], "done")

        store.insert_queued(
            user_id=user_id,
            key_id=store.authenticate(key).id,
            diff_hash="b" * 64,
            diff_text=_added("bad.py", 1, "b"),
            source="api",
            idempotency_key=None,
        )
        self.assertTrue(settle_one(store, reviewer_for("error")))
        self.assertEqual(store.billing(user_id).reviews_finished, 1)


if __name__ == "__main__":
    unittest.main()
