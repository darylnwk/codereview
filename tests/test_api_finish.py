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

_TOO_EXPENSIVE = {
    "error": "too_expensive",
    "message": "The review would cost more than $0.15.",
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
    def __init__(self, db: str, *, worker: bool) -> None:
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
        if not worker:
            command.append("--no-worker")
        self.err = tempfile.TemporaryFile()
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
                raise RuntimeError(self._stderr())
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
        self.server = FinishServer(self.db, worker=False)
        self.addCleanup(self.server.stop)

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

    def credits(self, key: str) -> int:
        status, body = self.server.exchange("GET", "/v1/me", key)
        self.assertEqual(status, 200)
        return int(body["credits"])

    def run_count(self) -> int:
        connection = sqlite3.connect(self.db)
        try:
            row = connection.execute("SELECT COUNT(*) FROM runs").fetchone()
        finally:
            connection.close()
        return int(row[0])

    def test_price_cap_refuses_7501_lines_and_accepts_20(self) -> None:
        key = self.seed(5)
        status, body = self.server.exchange(
            "POST",
            "/v1/reviews",
            key,
            {"diff": _added("billing/charge.py", 7501, "x")},
        )
        self.assertEqual(status, 413)
        self.assertEqual(body, _TOO_EXPENSIVE)
        self.assertEqual(self.credits(key), 5)
        self.assertEqual(self.run_count(), 0)
        accepted, accepted_body = self.server.exchange(
            "POST",
            "/v1/reviews",
            key,
            {"diff": _added("billing/charge.py", 20, "y")},
        )
        self.assertEqual(accepted, 202)
        self.assertEqual(list(accepted_body), ["run_id"])
        self.assertEqual(self.credits(key), 5)
