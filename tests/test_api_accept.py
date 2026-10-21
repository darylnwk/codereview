"""HTTP acceptance for a queued review. The server and sqlite file are real."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from review_grade.api.http import bind
from review_grade.api.store import Store

_RUN_ID = re.compile(r"^run_[A-Za-z0-9_-]{16}$")
_UNAUTHORIZED = {"error": "unauthorized", "message": "The API key is not valid."}
_BAD_REQUEST = {"error": "bad_request", "message": "The review request is not valid."}
_NOT_FOUND = {"error": "not_found", "message": "The run was not found."}
_CONFLICT = {
    "error": "conflict",
    "message": "The idempotency key was already used for a different diff.",
}
_TOO_LARGE = {"error": "too_large", "message": "The diff is too large to review."}
_RATE_LIMITED = {
    "error": "rate_limited",
    "message": "Too many reviews were accepted in the last minute.",
}
_QUEUED = {
    "status": "queued",
    "findings": None,
    "partial": False,
    "omitted": [],
    "cost_usd": None,
}


def _added(path: str, count: int, marker: str) -> str:
    lines = [
        f"diff --git a/{path} b/{path}\n",
        f"--- a/{path}\n",
        f"+++ b/{path}\n",
    ]
    lines.extend(f"+{marker}{index}\n" for index in range(count))
    return "".join(lines)


def _tiny(name: str) -> str:
    return (
        f"diff --git a/{name} b/{name}\n"
        f"--- a/{name}\n"
        f"+++ b/{name}\n"
        "+ok\n"
    )


class ApiAcceptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "accept.sqlite")
        self.httpd = bind(self.db, 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.port = self.httpd.server_address[1]

    def _stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.httpd.store.close()

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
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.stderr, "")
        self.assertTrue(completed.stdout.endswith("\n"))
        secret = completed.stdout[:-1]
        self.assertNotIn("\n", secret)
        self.assertTrue(secret.startswith("ra_live_"))
        self.assertGreaterEqual(len(secret), len("ra_live_") + 8)
        return secret

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

    def run_rows(self):
        connection = sqlite3.connect(self.db)
        connection.row_factory = sqlite3.Row
        try:
            return connection.execute(
                "SELECT id, user_id, diff_hash, diff_text, status, source, idempotency_key FROM runs"
            ).fetchall()
        finally:
            connection.close()

    def test_unknown_run_returns_404(self) -> None:
        key = self.seed(2)
        status, body = self.exchange("GET", "/v1/runs/run_does_not_exist", key)
        self.assertEqual(status, 404)
        self.assertEqual(body, _NOT_FOUND)

    def test_empty_diff_returns_400(self) -> None:
        key = self.seed(2)
        payloads = (
            {"diff": ""},
            {"diff": "\n"},
            {},
            {"diff": _tiny("a.py"), "source": "email"},
            {"diff": _tiny("a.py"), "files": "a.py"},
            {"diff": _tiny("a.py"), "files": []},
        )
        for payload in payloads:
            status, body = self.exchange("POST", "/v1/reviews", key, payload)
            self.assertEqual(status, 400)
            self.assertEqual(body, _BAD_REQUEST)
        self.assertEqual(self.run_rows(), [])

    def test_revoked_key_returns_401(self) -> None:
        key = self.seed(2)
        store = Store(self.db)
        try:
            store.revoke(key)
        finally:
            store.close()
        revoked_status, revoked_body = self.exchange("POST", "/v1/reviews", key, {"diff": _tiny("a.py")})
        unknown_status, unknown_body = self.exchange(
            "POST",
            "/v1/reviews",
            "ra_live_not_a_real_key_xxxxxxxx",
            {"diff": _tiny("a.py")},
        )
        missing_status, missing_body = self.exchange("POST", "/v1/reviews", None, {"diff": _tiny("a.py")})
        self.assertEqual(revoked_status, 401)
        self.assertEqual(unknown_status, 401)
        self.assertEqual(missing_status, 401)
        self.assertEqual(revoked_body, _UNAUTHORIZED)
        self.assertEqual(unknown_body, revoked_body)
        self.assertEqual(missing_body, revoked_body)
        self.assertEqual(self.run_rows(), [])

    def test_idempotency_key_conflict_returns_409(self) -> None:
        key = self.seed(2)
        first_status, first_body = self.exchange(
            "POST",
            "/v1/reviews",
            key,
            {"diff": _tiny("one.py"), "source": "ci"},
            "same-key",
        )
        self.assertEqual(first_status, 202)
        status, body = self.exchange(
            "POST",
            "/v1/reviews",
            key,
            {"diff": _tiny("two.py"), "source": "ci"},
            "same-key",
        )
        self.assertEqual(status, 409)
        self.assertEqual(body, _CONFLICT)
        rows = self.run_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], first_body["run_id"])
        self.assertEqual(rows[0]["source"], "ci")

    def test_idempotency_key_repeats_the_same_run(self) -> None:
        key = self.seed(2)
        diff = _tiny("same.py")
        first_status, first_body = self.exchange(
            "POST", "/v1/reviews", key, {"diff": diff}, "repeat-me"
        )
        second_status, second_body = self.exchange(
            "POST", "/v1/reviews", key, {"diff": diff, "source": "agent"}, "repeat-me"
        )
        self.assertEqual(first_status, 202)
        self.assertEqual(second_status, 202)
        self.assertEqual(second_body, first_body)
        self.assertEqual(list(first_body), ["run_id"])
        self.assertRegex(first_body["run_id"], _RUN_ID)
        rows = self.run_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], first_body["run_id"])
        self.assertEqual(rows[0]["source"], "api")
        self.assertEqual(rows[0]["diff_text"], diff)
        self.assertEqual(rows[0]["diff_hash"], hashlib.sha256(diff.encode("utf-8")).hexdigest())

    def test_accept_returns_202_and_queued_get(self) -> None:
        key = self.seed(4)
        diff = _tiny("review.py")
        status, body = self.exchange("POST", "/v1/reviews", key, {"diff": diff})
        self.assertEqual(status, 202)
        self.assertEqual(list(body), ["run_id"])
        self.assertRegex(body["run_id"], _RUN_ID)
        got_status, got = self.exchange("GET", "/v1/runs/" + body["run_id"], key)
        self.assertEqual(got_status, 200)
        self.assertEqual(got, _QUEUED)
        me_status, me = self.exchange("GET", "/v1/me", key)
        self.assertEqual(me_status, 200)
        self.assertEqual(me, {"credits": 4})
        self.assertIsNone(got["findings"])
        self.assertNotEqual(got["findings"], [])

    def test_zero_credits_under_cap_is_accepted(self) -> None:
        # credits is legacy; access uses per-period plan/cap.
        key = self.seed(0)
        status, body = self.exchange("POST", "/v1/reviews", key, {"diff": _tiny("a.py")})
        self.assertEqual(status, 202)
        self.assertEqual(list(body), ["run_id"])
        me_status, me = self.exchange("GET", "/v1/me", key)
        self.assertEqual(me_status, 200)
        self.assertEqual(me, {"credits": 0})
        self.assertEqual(len(self.run_rows()), 1)

    def test_generated_diff_returns_413(self) -> None:
        key = self.seed(3)
        generated = (
            "diff --git a/web/package-lock.json b/web/package-lock.json\n"
            "--- a/web/package-lock.json\n"
            "+++ b/web/package-lock.json\n"
            "+1\n",
            "diff --git a/pnpm-lock.yaml b/pnpm-lock.yaml\n"
            "--- a/pnpm-lock.yaml\n"
            "+++ b/pnpm-lock.yaml\n"
            "+1\n",
            "diff --git a/yarn.lock b/yarn.lock\n"
            "--- a/yarn.lock\n"
            "+++ b/yarn.lock\n"
            "+1\n",
            "diff --git a/src/snapshots/a.js b/src/snapshots/a.js\n"
            "--- a/src/snapshots/a.js\n"
            "+++ b/src/snapshots/a.js\n"
            "+1\n",
            "diff --git a/vendor/lib.c b/dev/null\n"
            "--- a/vendor/lib.c\n"
            "+++ /dev/null\n"
            "-old\n",
            "diff --git a/app/vendored/x.go b/app/vendored/x.go\n"
            "--- a/app/vendored/x.go\n"
            "+++ b/app/vendored/x.go\n"
            "+1\n",
        )
        for diff in generated:
            status, body = self.exchange("POST", "/v1/reviews", key, {"diff": diff})
            self.assertEqual(status, 413)
            self.assertEqual(body, _TOO_LARGE)
        self.assertEqual(self.run_rows(), [])
        mixed = generated[0] + _tiny("src/app.py")
        status, body = self.exchange("POST", "/v1/reviews", key, {"diff": mixed})
        self.assertEqual(status, 202)
        self.assertRegex(body["run_id"], _RUN_ID)
        me_status, me = self.exchange("GET", "/v1/me", key)
        self.assertEqual(me_status, 200)
        self.assertEqual(me, {"credits": 3})

    def test_test_file_is_a_normal_file(self) -> None:
        key = self.seed(1)
        status, body = self.exchange(
            "POST",
            "/v1/reviews",
            key,
            {"diff": _tiny("tests/test_api_accept.py")},
        )
        self.assertEqual(status, 202)
        self.assertRegex(body["run_id"], _RUN_ID)

    def test_line_cap_lists_files_until_20000(self) -> None:
        key = self.seed(2)
        under_status, under_body = self.exchange(
            "POST",
            "/v1/reviews",
            key,
            {"diff": _added("src/app.py", 7999, "u")},
        )
        self.assertEqual(under_status, 202)
        self.assertRegex(under_body["run_id"], _RUN_ID)
        at_status, at_body = self.exchange(
            "POST",
            "/v1/reviews",
            key,
            {"diff": _added("src/app.py", 8000, "a")},
        )
        self.assertEqual(at_status, 413)
        self.assertEqual(at_body, {**_TOO_LARGE, "files": ["src/app.py"]})
        top_status, top_body = self.exchange(
            "POST",
            "/v1/reviews",
            key,
            {"diff": _added("src/app.py", 20000, "t")},
        )
        self.assertEqual(top_status, 413)
        self.assertEqual(top_body, {**_TOO_LARGE, "files": ["src/app.py"]})
        over_status, over_body = self.exchange(
            "POST",
            "/v1/reviews",
            key,
            {"diff": _added("src/app.py", 20001, "o")},
        )
        self.assertEqual(over_status, 413)
        self.assertEqual(over_body, _TOO_LARGE)
        quoted = _added("my file.py", 8000, "q").replace(
            "diff --git a/my file.py b/my file.py\n",
            'diff --git "a/my file.py" "b/my file.py"\n',
        )
        quoted_status, quoted_body = self.exchange("POST", "/v1/reviews", key, {"diff": quoted})
        self.assertEqual(quoted_status, 413)
        self.assertEqual(quoted_body, {**_TOO_LARGE, "files": ["my file.py"]})

    def test_file_subset_can_accept(self) -> None:
        key = self.seed(2)
        full = _added("big.py", 8000, "B") + _added("keep.py", 2, "K")
        refused_status, refused = self.exchange("POST", "/v1/reviews", key, {"diff": full})
        self.assertEqual(refused_status, 413)
        self.assertEqual(refused, {**_TOO_LARGE, "files": ["big.py", "keep.py"]})
        status, body = self.exchange(
            "POST",
            "/v1/reviews",
            key,
            {"diff": full, "files": ["keep.py"]},
        )
        self.assertEqual(status, 202)
        self.assertRegex(body["run_id"], _RUN_ID)
        rows = self.run_rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("K0", rows[0]["diff_text"])
        self.assertNotIn("B0", rows[0]["diff_text"])
        self.assertEqual(rows[0]["diff_hash"], hashlib.sha256(full.encode("utf-8")).hexdigest())
        got_status, got = self.exchange("GET", "/v1/runs/" + body["run_id"], key)
        self.assertEqual(got_status, 200)
        self.assertEqual(got, _QUEUED)

    def test_eleventh_accept_is_rate_limited(self) -> None:
        key = self.seed(3)
        blocked_status, blocked = self.exchange(
            "POST",
            "/v1/reviews",
            key,
            {"diff": "diff --git a/yarn.lock b/yarn.lock\n--- a/yarn.lock\n+++ b/yarn.lock\n+1\n"},
        )
        self.assertEqual(blocked_status, 413)
        self.assertEqual(blocked, _TOO_LARGE)
        first_status, first_body = self.exchange(
            "POST", "/v1/reviews", key, {"diff": _tiny("f0.py")}, "slot"
        )
        self.assertEqual(first_status, 202)
        for index in range(1, 10):
            status, body = self.exchange(
                "POST", "/v1/reviews", key, {"diff": _tiny(f"f{index}.py")}
            )
            self.assertEqual(status, 202, body)
            self.assertRegex(body["run_id"], _RUN_ID)
        me_status, me = self.exchange("GET", "/v1/me", key)
        self.assertEqual(me_status, 200)
        self.assertEqual(me, {"credits": 3})
        replay_status, replay_body = self.exchange(
            "POST", "/v1/reviews", key, {"diff": _tiny("f0.py")}, "slot"
        )
        self.assertEqual(replay_status, 202)
        self.assertEqual(replay_body, first_body)
        limited_status, limited = self.exchange(
            "POST", "/v1/reviews", key, {"diff": _tiny("f10.py")}
        )
        self.assertEqual(limited_status, 429)
        self.assertEqual(limited, _RATE_LIMITED)
        self.assertEqual(len(self.run_rows()), 10)

    def test_other_user_cannot_read_the_run(self) -> None:
        key = self.seed(2)
        status, body = self.exchange("POST", "/v1/reviews", key, {"diff": _tiny("owned.py")})
        self.assertEqual(status, 202)
        store = Store(self.db)
        try:
            other = store.new_key(store.add_user("other@reviewgrade.dev", 5))
        finally:
            store.close()
        hidden_status, hidden = self.exchange("GET", "/v1/runs/" + body["run_id"], other)
        self.assertEqual(hidden_status, 404)
        self.assertEqual(hidden, _NOT_FOUND)
        own_status, own = self.exchange("GET", "/v1/runs/" + body["run_id"], key)
        self.assertEqual(own_status, 200)
        self.assertEqual(own, _QUEUED)

    def test_second_key_has_its_own_accept_window(self) -> None:
        key_a = self.seed(2)
        store = Store(self.db)
        try:
            owner = store.authenticate(key_a)
            if owner is None:
                self.fail("seed key did not authenticate")
            key_b = store.new_key(owner.user_id)
        finally:
            store.close()
        first_status, first_body = self.exchange(
            "POST", "/v1/reviews", key_a, {"diff": _tiny("shared.py")}, "shared"
        )
        self.assertEqual(first_status, 202)
        conflict_status, conflict_body = self.exchange(
            "POST", "/v1/reviews", key_b, {"diff": _tiny("other.py")}, "shared"
        )
        self.assertEqual(conflict_status, 409)
        self.assertEqual(conflict_body, _CONFLICT)
        for index in range(1, 10):
            status, body = self.exchange(
                "POST", "/v1/reviews", key_a, {"diff": _tiny(f"a{index}.py")}
            )
            self.assertEqual(status, 202, body)
        limited_status, limited = self.exchange(
            "POST", "/v1/reviews", key_a, {"diff": _tiny("a10.py")}
        )
        self.assertEqual(limited_status, 429)
        self.assertEqual(limited, _RATE_LIMITED)
        other_status, other_body = self.exchange(
            "POST", "/v1/reviews", key_b, {"diff": _tiny("b0.py")}
        )
        self.assertEqual(other_status, 202)
        self.assertRegex(other_body["run_id"], _RUN_ID)
        self.assertNotEqual(other_body["run_id"], first_body["run_id"])
        self.assertEqual(len(self.run_rows()), 11)

    def test_concurrent_idempotency_key_returns_one_run(self) -> None:
        key = self.seed(4)
        diff = _tiny("race.py")
        barrier = threading.Barrier(8)
        results = []
        guard = threading.Lock()

        def post() -> None:
            barrier.wait()
            status, body = self.exchange(
                "POST", "/v1/reviews", key, {"diff": diff}, "race"
            )
            with guard:
                results.append((status, body))

        threads = [threading.Thread(target=post) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 8)
        run_ids = []
        for status, body in results:
            self.assertEqual(status, 202, body)
            self.assertEqual(list(body), ["run_id"])
            run_ids.append(body["run_id"])
        self.assertEqual(set(run_ids), {run_ids[0]})
        self.assertEqual(len(self.run_rows()), 1)

    def test_missing_idempotency_key_inserts_a_new_run(self) -> None:
        key = self.seed(2)
        diff = _tiny("again.py")
        first_status, first_body = self.exchange("POST", "/v1/reviews", key, {"diff": diff})
        second_status, second_body = self.exchange("POST", "/v1/reviews", key, {"diff": diff})
        self.assertEqual(first_status, 202)
        self.assertEqual(second_status, 202)
        self.assertNotEqual(first_body["run_id"], second_body["run_id"])
        self.assertEqual(len(self.run_rows()), 2)
