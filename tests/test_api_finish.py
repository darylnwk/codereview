"""The served API prices a diff and finishes a queued review once."""

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
from datetime import datetime, timedelta, timezone

from review_grade.api.store import format_time

_TOO_EXPENSIVE = {
    "error": "too_expensive",
    "message": "The review would cost more than $0.15.",
}
_QUEUED = {
    "status": "queued",
    "findings": None,
    "partial": False,
    "omitted": [],
    "cost_usd": None,
}
_FINDING = {
    "file": "billing/charge.py",
    "line": 14,
    "what_breaks": "The charge skips the zero-cent guard.",
    "fix": "Reject a charge of zero cents.",
}
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


class FinishServer:
    def __init__(self, db: str, *, worker: bool, fixture: str | None = None) -> None:
        if worker and fixture is None:
            raise AssertionError("a test worker needs a fixture")
        self.port = _free_port()
        command = [
            sys.executable,
            "-m",
            "review_grade.api",
            "--db",
            db,
            "--port",
            str(self.port),
        ]
        if fixture is not None:
            command.extend(["--fixture", fixture])
        if not worker:
            command.append("--no-worker")
        self.err = tempfile.TemporaryFile()
        self._stopped = False
        self.proc = subprocess.Popen(
            command,
            cwd=_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=self.err,
        )
        self._wait_until_listening()

    def _wait_until_listening(self) -> None:
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(self._stderr() or "server exited")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.02)
        raise RuntimeError(self._stderr() or "server did not listen")

    def _stderr(self) -> str:
        self.err.seek(0)
        return self.err.read().decode("utf-8", errors="replace")

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.err.close()

    def exchange(self, method, path, token=None, payload=None, idempotency_key=None):
        headers = {}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
                raw = response.read()
        except urllib.error.HTTPError as error:
            try:
                status = error.code
                raw = error.read()
            finally:
                error.close()
        return status, json.loads(raw.decode("utf-8"))


class ApiFinishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "finish.sqlite")

    def open_server(self, *, worker: bool, fixture: str | None = None) -> FinishServer:
        server = FinishServer(self.db, worker=worker, fixture=fixture)
        self.addCleanup(server.stop)
        return server

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

    def credits(self, server: FinishServer, key: str) -> int:
        status, body = server.exchange("GET", "/v1/me", key)
        self.assertEqual(status, 200)
        return int(body["credits"])

    def run_count(self) -> int:
        connection = sqlite3.connect(self.db)
        try:
            row = connection.execute("SELECT COUNT(*) FROM runs").fetchone()
        finally:
            connection.close()
        return int(row[0])

    def run_row(self, run_id: str) -> sqlite3.Row:
        connection = sqlite3.connect(self.db)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        finally:
            connection.close()
        if row is None:
            self.fail("missing run " + run_id)
        return row

    def wait_settled(self, server: FinishServer, key: str, run_id: str) -> dict:
        deadline = time.time() + 10
        last = None
        while time.time() < deadline:
            status, body = server.exchange("GET", "/v1/runs/" + run_id, key)
            self.assertEqual(status, 200)
            last = body
            if body["status"] != "queued":
                return body
            time.sleep(0.02)
        self.fail("run stayed queued: " + json.dumps(last))

    def test_price_cap_refuses_7501_lines_and_accepts_20(self) -> None:
        server = self.open_server(worker=False)
        key = self.seed(5)
        status, body = server.exchange(
            "POST",
            "/v1/reviews",
            key,
            {"diff": _added("billing/charge.py", 7501, "x")},
        )
        self.assertEqual(status, 413)
        self.assertEqual(body, _TOO_EXPENSIVE)
        self.assertEqual(self.credits(server, key), 5)
        self.assertEqual(self.run_count(), 0)
        accepted, accepted_body = server.exchange(
            "POST",
            "/v1/reviews",
            key,
            {"diff": _added("billing/charge.py", 20, "y")},
        )
        self.assertEqual(accepted, 202)
        self.assertEqual(list(accepted_body), ["run_id"])
        self.assertEqual(self.credits(server, key), 5)

    def test_finding_fixture_charges_once_and_keeps_the_finding(self) -> None:
        server = self.open_server(worker=True, fixture="finding")
        key = self.seed(5)
        status, body = server.exchange(
            "POST",
            "/v1/reviews",
            key,
            {"diff": _added("billing/charge.py", 20, "y")},
        )
        self.assertEqual(status, 202)
        got = self.wait_settled(server, key, body["run_id"])
        self.assertEqual(got["status"], "done")
        self.assertEqual(got["partial"], False)
        self.assertEqual(got["omitted"], [])
        self.assertEqual(got["findings"], [_FINDING])
        self.assertEqual(got["cost_usd"], 0.0004)
        self.assertEqual(self.credits(server, key), 4)
        again_status, again = server.exchange("GET", "/v1/runs/" + body["run_id"], key)
        self.assertEqual(again_status, 200)
        self.assertEqual(again["findings"], got["findings"])
        self.assertEqual(self.credits(server, key), 4)
        row = self.run_row(body["run_id"])
        self.assertIsNone(row["diff_text"])
        self.assertIsNone(row["cache_read"])
        self.assertIsNone(row["cache_write"])

    def test_error_fixture_leaves_credits_at_the_seed(self) -> None:
        server = self.open_server(worker=True, fixture="error")
        key = self.seed(5)
        status, body = server.exchange(
            "POST",
            "/v1/reviews",
            key,
            {"diff": _added("billing/charge.py", 1, "e")},
        )
        self.assertEqual(status, 202)
        got = self.wait_settled(server, key, body["run_id"])
        self.assertEqual(got["status"], "failed")
        self.assertIsNone(got["findings"])
        self.assertEqual(self.credits(server, key), 5)
        self.assertIsNone(self.run_row(body["run_id"])["diff_text"])

    def test_bad_fixture_fails_with_null_findings(self) -> None:
        server = self.open_server(worker=True, fixture="bad")
        key = self.seed(5)
        status, body = server.exchange(
            "POST",
            "/v1/reviews",
            key,
            {"diff": _added("billing/charge.py", 1, "b")},
        )
        self.assertEqual(status, 202)
        got = self.wait_settled(server, key, body["run_id"])
        self.assertEqual(got["status"], "failed")
        self.assertIsNone(got["findings"])
        self.assertEqual(self.credits(server, key), 5)

    def test_partial_review_lists_the_other_path(self) -> None:
        quiet = self.open_server(worker=False)
        key = self.seed(5)
        diff = _added("keep.py", 1, "K") + _added("skip.py", 1, "S")
        status, body = quiet.exchange(
            "POST",
            "/v1/reviews",
            key,
            {"diff": diff, "files": ["keep.py"]},
        )
        self.assertEqual(status, 202)
        queued_status, queued = quiet.exchange("GET", "/v1/runs/" + body["run_id"], key)
        self.assertEqual(queued_status, 200)
        self.assertEqual(queued, _QUEUED)
        quiet.stop()
        server = self.open_server(worker=True, fixture="finding")
        got = self.wait_settled(server, key, body["run_id"])
        self.assertEqual(got["status"], "done")
        self.assertEqual(got["partial"], True)
        self.assertEqual(got["omitted"], ["skip.py"])
        self.assertEqual(self.credits(server, key), 4)

    def test_one_credit_finishes_the_oldest_run_only(self) -> None:
        quiet = self.open_server(worker=False)
        key = self.seed(1)
        first_status, first = quiet.exchange(
            "POST",
            "/v1/reviews",
            key,
            {"diff": _added("first.py", 1, "a")},
        )
        second_status, second = quiet.exchange(
            "POST",
            "/v1/reviews",
            key,
            {"diff": _added("second.py", 1, "b")},
        )
        self.assertEqual(first_status, 202)
        self.assertEqual(second_status, 202)
        quiet.stop()
        server = self.open_server(worker=True, fixture="finding")
        oldest = self.wait_settled(server, key, first["run_id"])
        newer = self.wait_settled(server, key, second["run_id"])
        self.assertEqual(oldest["status"], "done")
        self.assertEqual(newer["status"], "failed")
        self.assertIsNone(newer["findings"])
        self.assertEqual(self.credits(server, key), 0)

    def test_stale_run_fails_and_keeps_credits(self) -> None:
        quiet = self.open_server(worker=False)
        key = self.seed(5)
        status, body = quiet.exchange(
            "POST",
            "/v1/reviews",
            key,
            {"diff": _added("old.py", 1, "o")},
        )
        self.assertEqual(status, 202)
        old = format_time(datetime.now(timezone.utc) - timedelta(minutes=4))
        connection = sqlite3.connect(self.db)
        try:
            connection.execute(
                "UPDATE runs SET created_at = ? WHERE id = ?",
                (old, body["run_id"]),
            )
            connection.commit()
        finally:
            connection.close()
        quiet.stop()
        server = self.open_server(worker=True, fixture="finding")
        got = self.wait_settled(server, key, body["run_id"])
        self.assertEqual(got["status"], "failed")
        self.assertIsNone(got["findings"])
        self.assertEqual(self.credits(server, key), 5)

    def test_second_worker_pass_charges_once(self) -> None:
        server = self.open_server(worker=True, fixture="finding")
        key = self.seed(5)
        status, body = server.exchange(
            "POST",
            "/v1/reviews",
            key,
            {"diff": _added("billing/charge.py", 1, "z")},
        )
        self.assertEqual(status, 202)
        got = self.wait_settled(server, key, body["run_id"])
        self.assertEqual(got["status"], "done")
        self.assertEqual(self.credits(server, key), 4)
        server.stop()
        restarted = self.open_server(worker=True, fixture="finding")
        self.assertEqual(self.credits(restarted, key), 4)
        again_status, again = restarted.exchange("GET", "/v1/runs/" + body["run_id"], key)
        self.assertEqual(again_status, 200)
        self.assertEqual(again["findings"], got["findings"])
        self.assertEqual(again["status"], "done")

    def test_claimed_queued_run_resumes_and_charges_once(self) -> None:
        quiet = self.open_server(worker=False)
        key = self.seed(5)
        status, body = quiet.exchange(
            "POST",
            "/v1/reviews",
            key,
            {"diff": _added("billing/charge.py", 1, "c")},
        )
        self.assertEqual(status, 202)
        connection = sqlite3.connect(self.db)
        try:
            connection.execute(
                "UPDATE runs SET claim_token = ? WHERE id = ?",
                ("dead-worker", body["run_id"]),
            )
            connection.commit()
        finally:
            connection.close()
        quiet.stop()
        server = self.open_server(worker=True, fixture="finding")
        got = self.wait_settled(server, key, body["run_id"])
        self.assertEqual(got["status"], "done")
        self.assertEqual(got["findings"], [_FINDING])
        self.assertEqual(self.credits(server, key), 4)
        server.stop()
        restarted = self.open_server(worker=True, fixture="error")
        self.assertEqual(self.credits(restarted, key), 4)
