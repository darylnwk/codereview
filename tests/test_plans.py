"""Plan prices, overage, monthly cap, and no legacy per-email trial."""

# ruff: noqa: SIM115

from __future__ import annotations

import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import UTC, datetime

from review_grade.api import http as http_api
from review_grade.api.plans import (
    ANNUAL_PRICING,
    DEFAULT_MONTHLY_CAP_CENTS,
    DEFAULT_MONTHLY_CAP_USD,
    DEVELOPER,
    FLEET,
    PLANS,
    _cap_cents,
    allow_next_review,
    billing_period_start,
    charge_for_review,
    charge_for_review_cents,
    get_plan,
    has_legacy_trial_plan,
    monthly_spend_usd,
    overage_reviews,
    overage_spend_cents,
    overage_spend_usd,
    trial_ends_at,
    within_monthly_cap,
)
from review_grade.api.store import Store, format_time
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


def _seed_period_runs(
    store: Store,
    *,
    user_id: int,
    key_id: int,
    done: int = 0,
    failed: int = 0,
    queued: int = 0,
    created_at: str | None = None,
) -> None:
    """Insert settled/queued rows with a chosen created_at (current period by default)."""
    when = created_at or format_time(datetime.now(UTC))
    with store.transaction():
        for index in range(done):
            run_id = "run_done_" + secrets.token_hex(8) + f"_{index}"
            store._conn.execute(
                """
                INSERT INTO runs (
                    id, user_id, key_id, diff_hash, diff_text, findings_json, status,
                    partial, omitted_json, cost_usd, source, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, NULL, '[]', 'done', 0, NULL, 0.0, 'api', NULL, ?)
                """,
                (run_id, user_id, key_id, "d" * 64, when),
            )
        for index in range(failed):
            run_id = "run_fail_" + secrets.token_hex(8) + f"_{index}"
            store._conn.execute(
                """
                INSERT INTO runs (
                    id, user_id, key_id, diff_hash, diff_text, findings_json, status,
                    partial, omitted_json, cost_usd, source, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, NULL, NULL, 'failed', 0, NULL, NULL, 'api', NULL, ?)
                """,
                (run_id, user_id, key_id, "f" * 64, when),
            )
        for index in range(queued):
            store._insert_queued(
                user_id=user_id,
                key_id=key_id,
                diff_hash="q" * 64,
                diff_text=_added(f"q{index}.py", 1, "q"),
                source="api",
                idempotency_key=None,
                omitted_pending=None,
                created_at=when,
            )


class PlanMathTests(unittest.TestCase):
    def test_developer_and_fleet_included_and_prices(self) -> None:
        self.assertEqual(DEVELOPER.monthly_price_usd, 25)
        self.assertEqual(DEVELOPER.monthly_price_cents, 2500)
        self.assertEqual(DEVELOPER.included_reviews, 250)
        self.assertEqual(DEVELOPER.overage_usd, 0.15)
        self.assertEqual(DEVELOPER.overage_cents, 15)
        self.assertEqual(FLEET.monthly_price_usd, 199)
        self.assertEqual(FLEET.monthly_price_cents, 19900)
        self.assertEqual(FLEET.included_reviews, 2500)
        self.assertEqual(FLEET.overage_usd, 0.06)
        self.assertEqual(FLEET.overage_cents, 6)
        self.assertEqual(set(PLANS), {"developer", "fleet"})
        self.assertIs(get_plan("developer"), DEVELOPER)
        self.assertIs(get_plan("fleet"), FLEET)

    def test_default_cap_constant_is_zero(self) -> None:
        # Daryl, 21 Oct 2026: no default overage on any tier.
        self.assertEqual(DEFAULT_MONTHLY_CAP_USD, 0)
        self.assertEqual(DEFAULT_MONTHLY_CAP_CENTS, 0)
        self.assertEqual(DEFAULT_MONTHLY_CAP_CENTS, DEFAULT_MONTHLY_CAP_USD * 100)

    def test_no_legacy_twenty_review_trial(self) -> None:
        self.assertFalse(has_legacy_trial_plan())
        self.assertNotIn("trial", PLANS)
        for plan in PLANS.values():
            self.assertNotEqual(plan.included_reviews, 20)
            self.assertNotEqual(plan.monthly_price_usd, 0)
        self.assertFalse(ANNUAL_PRICING)

    def test_included_reviews_have_no_overage_charge(self) -> None:
        self.assertEqual(overage_reviews(DEVELOPER, 0), 0)
        self.assertEqual(overage_reviews(DEVELOPER, 250), 0)
        self.assertEqual(overage_reviews(DEVELOPER, 251), 1)
        self.assertEqual(charge_for_review(DEVELOPER, 0, status="done"), 0.0)
        self.assertEqual(charge_for_review(DEVELOPER, 249, status="done"), 0.0)
        self.assertEqual(charge_for_review(DEVELOPER, 250, status="done"), 0.15)
        self.assertEqual(charge_for_review_cents(DEVELOPER, 250, status="done"), 15)
        self.assertEqual(charge_for_review(FLEET, 2499, status="done"), 0.0)
        self.assertEqual(charge_for_review(FLEET, 2500, status="done"), 0.06)
        self.assertEqual(charge_for_review_cents(FLEET, 2500, status="done"), 6)

    def test_overage_rates_and_monthly_spend(self) -> None:
        self.assertEqual(overage_spend_usd(DEVELOPER, 250), 0.0)
        self.assertEqual(overage_spend_cents(DEVELOPER, 260), 150)
        self.assertAlmostEqual(overage_spend_usd(DEVELOPER, 260), 1.5)
        self.assertEqual(overage_spend_cents(FLEET, 2600), 600)
        self.assertEqual(monthly_spend_usd(DEVELOPER, 36), 25.0)
        self.assertAlmostEqual(monthly_spend_usd(DEVELOPER, 260), 26.5)
        self.assertEqual(monthly_spend_usd(FLEET, 100), 199.0)

    def test_failed_runs_are_free(self) -> None:
        self.assertEqual(charge_for_review(DEVELOPER, 0, status="failed"), 0.0)
        self.assertEqual(charge_for_review_cents(DEVELOPER, 300, status="failed"), 0)
        self.assertEqual(charge_for_review(FLEET, 3000, status="failed"), 0.0)

    def test_user_cap_boundary_exactly_at_cap(self) -> None:
        # Cap $0: last included allowed; first extra refused.
        self.assertTrue(allow_next_review(DEVELOPER, 249, 0))
        self.assertFalse(allow_next_review(DEVELOPER, 250, 0))
        self.assertTrue(allow_next_review(FLEET, 2499, 0))
        self.assertFalse(allow_next_review(FLEET, 2500, 0))
        # Cap $50 on Developer: 333 extras * 15¢ = 4995¢ <= 5000¢; 334th exceeds.
        self.assertTrue(within_monthly_cap(DEVELOPER, 583, 50))
        self.assertFalse(within_monthly_cap(DEVELOPER, 584, 50))
        self.assertTrue(allow_next_review(DEVELOPER, 582, 50))
        self.assertFalse(allow_next_review(DEVELOPER, 583, 50))
        # Fleet $0.06: 833 extras * 6¢ = 4998¢; 834th exceeds.
        self.assertTrue(within_monthly_cap(FLEET, 3333, 50))
        self.assertFalse(within_monthly_cap(FLEET, 3334, 50))
        self.assertFalse(allow_next_review(FLEET, 3333, 50))

    def test_fractional_dollar_cap_uses_round(self) -> None:
        # $12.99 → 1299¢. Spend $12.15 = 81 extras * 15¢; that review is allowed.
        # int(12.99)*100 == 1200 would wrongly refuse it (mutation target).
        self.assertEqual(_cap_cents(12.99), 1299)
        self.assertEqual(overage_spend_cents(DEVELOPER, 331), 1215)
        self.assertTrue(within_monthly_cap(DEVELOPER, 331, 12.99))
        self.assertTrue(allow_next_review(DEVELOPER, 330, 12.99))
        # Boundary: 86 extras = 1290¢ ok; 87 extras = 1305¢ over 1299¢.
        self.assertTrue(within_monthly_cap(DEVELOPER, 336, 12.99))
        self.assertFalse(within_monthly_cap(DEVELOPER, 337, 12.99))
        self.assertTrue(allow_next_review(DEVELOPER, 335, 12.99))
        self.assertFalse(allow_next_review(DEVELOPER, 336, 12.99))

    def test_billing_period_start_is_month_utc(self) -> None:
        moment = datetime(2026, 10, 21, 15, 30, tzinfo=UTC)
        start = billing_period_start(moment)
        self.assertEqual(start, datetime(2026, 10, 1, 0, 0, tzinfo=UTC))

    def test_trial_ends_at_next_calendar_month(self) -> None:
        card = datetime(2026, 10, 21, 15, 30, tzinfo=UTC)
        self.assertEqual(trial_ends_at(card), datetime(2026, 11, 1, 0, 0, tzinfo=UTC))
        year_end = datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
        self.assertEqual(trial_ends_at(year_end), datetime(2027, 1, 1, 0, 0, tzinfo=UTC))


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

    def test_default_cap_used_by_schema_and_insert(self) -> None:
        store = Store(self.db)
        self.addCleanup(store.close)
        key = store.seed_account(1)
        user_id = store.authenticate(key).user_id
        billing = store.billing(user_id)
        self.assertEqual(billing.monthly_cap_usd, DEFAULT_MONTHLY_CAP_USD)
        row = store._conn.execute(
            "SELECT monthly_cap_usd FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        self.assertEqual(int(row["monthly_cap_usd"]), DEFAULT_MONTHLY_CAP_USD)
        cols = {str(r["name"]) for r in store._conn.execute("PRAGMA table_info(users)")}
        self.assertNotIn("reviews_finished", cols)

    def test_cap_zero_last_included_accepted_first_extra_refused(self) -> None:
        key = self.seed(0)
        store = Store(self.db)
        try:
            auth = store.authenticate(key)
            assert auth is not None
            self.assertEqual(store.billing(auth.user_id).monthly_cap_usd, 0)
            when = format_time(billing_period_start())
            _seed_period_runs(
                store,
                user_id=auth.user_id,
                key_id=auth.id,
                done=249,
                created_at=when,
            )
        finally:
            store.close()
        port = self.open_quiet_server()
        ok_status, ok_body = self.exchange(
            port,
            "POST",
            "/v1/reviews",
            key,
            {"diff": _added("last_included.py", 1, "a")},
        )
        self.assertEqual(ok_status, 202)
        self.assertIn("run_id", ok_body)
        denied_status, denied = self.exchange(
            port,
            "POST",
            "/v1/reviews",
            key,
            {"diff": _added("first_extra.py", 1, "b")},
        )
        self.assertEqual(denied_status, 402)
        self.assertEqual(denied["error"], "cap_reached")
        self.assertIn("Set a monthly cap", denied["message"])

    def test_after_setting_cap_extras_accepted_up_to_it(self) -> None:
        key = self.seed(0)
        store = Store(self.db)
        try:
            auth = store.authenticate(key)
            assert auth is not None
            when = format_time(billing_period_start())
            # Past included; $0.15 overage; cap $0.30 allows two extras.
            _seed_period_runs(
                store,
                user_id=auth.user_id,
                key_id=auth.id,
                done=250,
                created_at=when,
            )
            store.set_billing(auth.user_id, monthly_cap_usd=0)
        finally:
            store.close()
        port = self.open_quiet_server()
        blocked, body = self.exchange(
            port,
            "POST",
            "/v1/reviews",
            key,
            {"diff": _added("blocked.py", 1, "x")},
        )
        self.assertEqual(blocked, 402)
        self.assertEqual(body["error"], "cap_reached")
        store = Store(self.db)
        try:
            auth = store.authenticate(key)
            assert auth is not None
            # $1 → six extras at $0.15; seventh is refused.
            store.set_billing(auth.user_id, monthly_cap_usd=1)
        finally:
            store.close()
        for i in range(6):
            status, _ = self.exchange(
                port,
                "POST",
                "/v1/reviews",
                key,
                {"diff": _added(f"extra{i}.py", 1, "y")},
            )
            self.assertEqual(status, 202, msg=f"extra {i + 1} of 6")
        over, over_body = self.exchange(
            port,
            "POST",
            "/v1/reviews",
            key,
            {"diff": _added("over_cap.py", 1, "z")},
        )
        self.assertEqual(over, 402)
        self.assertEqual(over_body["error"], "cap_reached")

    def test_failed_runs_never_count_toward_period(self) -> None:
        store = Store(self.db)
        self.addCleanup(store.close)
        key = store.seed_account(5)
        auth = store.authenticate(key)
        assert auth is not None
        store.set_billing(auth.user_id, monthly_cap_usd=0)
        _seed_period_runs(store, user_id=auth.user_id, key_id=auth.id, failed=400)
        self.assertEqual(store.billing(auth.user_id).period_reviews, 0)
        store.insert_queued(
            user_id=auth.user_id,
            key_id=auth.id,
            diff_hash="a" * 64,
            diff_text=_added("bad.py", 1, "b"),
            source="api",
            idempotency_key=None,
        )
        self.assertTrue(settle_one(store, reviewer_for("error")))
        self.assertEqual(store.billing(auth.user_id).period_reviews, 0)

    def test_done_counts_failed_does_not_via_worker(self) -> None:
        store = Store(self.db)
        self.addCleanup(store.close)
        key = store.seed_account(5)
        auth = store.authenticate(key)
        assert auth is not None
        run_ok = store.insert_queued(
            user_id=auth.user_id,
            key_id=auth.id,
            diff_hash="a" * 64,
            diff_text=_added("ok.py", 1, "a"),
            source="api",
            idempotency_key=None,
        )
        self.assertTrue(settle_one(store, reviewer_for("finding")))
        self.assertEqual(store.billing(auth.user_id).period_reviews, 1)
        self.assertEqual(store.get_run(auth.user_id, run_ok)["status"], "done")

        store.insert_queued(
            user_id=auth.user_id,
            key_id=auth.id,
            diff_hash="b" * 64,
            diff_text=_added("bad.py", 1, "b"),
            source="api",
            idempotency_key=None,
        )
        self.assertTrue(settle_one(store, reviewer_for("error")))
        self.assertEqual(store.billing(auth.user_id).period_reviews, 1)

    def test_month_rollover_resets_included_reviews(self) -> None:
        store = Store(self.db)
        self.addCleanup(store.close)
        key = store.seed_account(5)
        auth = store.authenticate(key)
        assert auth is not None
        store.set_billing(auth.user_id, monthly_cap_usd=0)
        prior = format_time(datetime(2026, 9, 15, 12, 0, tzinfo=UTC))
        _seed_period_runs(
            store,
            user_id=auth.user_id,
            key_id=auth.id,
            done=250,
            created_at=prior,
        )
        now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
        self.assertEqual(store.billing(auth.user_id, now=now).period_reviews, 0)
        self.assertTrue(allow_next_review(DEVELOPER, 0, 0))

    def test_queued_reserves_cap_slot(self) -> None:
        store = Store(self.db)
        self.addCleanup(store.close)
        key = store.seed_account(5)
        auth = store.authenticate(key)
        assert auth is not None
        store.set_billing(auth.user_id, monthly_cap_usd=0)
        _seed_period_runs(store, user_id=auth.user_id, key_id=auth.id, done=249, queued=1)
        self.assertEqual(store.billing(auth.user_id).period_reviews, 250)
        self.assertFalse(
            allow_next_review(
                DEVELOPER,
                store.billing(auth.user_id).period_reviews,
                0,
            )
        )

    def _cap_race_fixture(self, store: Store, credits: int = 5) -> tuple[str, int]:
        secret = store.seed_account(credits)
        auth = store.authenticate(secret)
        assert auth is not None
        store.set_billing(auth.user_id, monthly_cap_usd=0)
        when = format_time(billing_period_start())
        _seed_period_runs(
            store,
            user_id=auth.user_id,
            key_id=auth.id,
            done=249,
            created_at=when,
        )
        self.assertEqual(store.billing(auth.user_id).period_reviews, 249)
        return secret, auth.user_id

    def test_accept_review_cap_race_one_winner(self) -> None:
        """Two accept_review threads at the cap; barrier between check and insert.

        With accept_review's transaction, one waiter times out on the barrier and
        inserts; the other sees cap_reached. Mutating away that transaction must
        make both insert (covered by the 20x harness in the PR body).
        """
        store = Store(self.db)
        self.addCleanup(store.close)
        secret, user_id = self._cap_race_fixture(store)
        barrier = threading.Barrier(2)

        def hook() -> None:
            try:
                barrier.wait(timeout=1.0)
            except threading.BrokenBarrierError:
                return

        http_api.after_cap_check = hook
        self.addCleanup(lambda: setattr(http_api, "after_cap_check", None))
        results: list[tuple[int, dict]] = []

        def worker(index: int) -> None:
            raw = json.dumps({"diff": _added(f"race{index}.py", 1, "r")}).encode("utf-8")
            status, body = http_api.accept_review(store, secret, None, raw)
            results.append((status, body))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 2, results)
        accepted = [body for status, body in results if status == 202]
        capped = [body for status, body in results if status == 402]
        self.assertEqual(len(accepted), 1, results)
        self.assertEqual(len(capped), 1, results)
        self.assertEqual(capped[0].get("error"), "cap_reached")
        queued = store._conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE status = 'queued'"
        ).fetchone()["n"]
        self.assertEqual(int(queued), 1)
        self.assertEqual(store.billing(user_id).period_reviews, 250)

    def test_drop_reviews_finished_tolerates_old_sqlite(self) -> None:
        store = Store(self.db)
        self.addCleanup(store.close)
        real = store._conn
        real.execute(
            "ALTER TABLE users ADD COLUMN reviews_finished INTEGER NOT NULL DEFAULT 0"
        )

        class _OldSqliteConn:
            def execute(self, sql, parameters=()):
                if isinstance(sql, str) and "DROP COLUMN reviews_finished" in sql:
                    raise sqlite3.OperationalError('near "COLUMN": syntax error')
                return real.execute(sql, parameters)

        store._conn = _OldSqliteConn()  # type: ignore[assignment]
        store._drop_reviews_finished_column()
        cols = {str(row["name"]) for row in real.execute("PRAGMA table_info(users)")}
        self.assertIn("reviews_finished", cols)
        store._conn = real


if __name__ == "__main__":
    unittest.main()
