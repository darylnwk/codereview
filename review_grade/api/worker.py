"""Finish queued runs. Public status stays queued until done or failed."""

from __future__ import annotations

import json
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from review_grade.api.cost import project_cost
from review_grade.api.store import Store
from review_grade.findings import check_findings
from review_grade.reviewer import ProviderError

_STALE_AFTER = timedelta(minutes=3)
_POLL_SECONDS = 0.02
_CREATED_AT = "%Y-%m-%dT%H:%M:%S.%fZ"

_FINDING = {
    "file": "billing/charge.py",
    "line": 14,
    "what_breaks": "The charge skips the zero-cent guard.",
    "fix": "Reject a charge of zero cents.",
}


@dataclass(frozen=True)
class _Done:
    findings: list
    cost: float
    partial: bool
    omitted_json: str | None


def reviewer_for(name: str | None):
    if name == "finding":
        return _fixture_finding
    if name == "error":
        return _fixture_error
    if name == "bad":
        return _fixture_bad
    if name is None:
        return _live_review
    raise ValueError(name)


def _fixture_finding(_diff: str) -> dict:
    return dict(_FINDING)


def _fixture_error(_diff: str) -> object:
    raise ProviderError("fixture reviewer failed")


def _fixture_bad(_diff: str) -> str:
    return "4 of 6"


def _live_review(diff: str) -> object:
    from review_grade.reviewer import review_diff
    from review_grade.rubric import assemble_prompt

    return review_diff(assemble_prompt(diff))


def settle_one(store: Store, review) -> bool:
    token = secrets.token_hex(16)
    row = store.claim_oldest(token)
    if row is None:
        return False
    run_id = str(row["id"])
    try:
        outcome = _judge(row, review)
        if isinstance(outcome, _Done):
            store.mark_done(
                run_id,
                token,
                int(row["user_id"]),
                outcome.findings,
                outcome.cost,
                outcome.partial,
                outcome.omitted_json,
            )
        else:
            store.mark_failed(run_id, token)
    except Exception:
        store.mark_failed(run_id, token)
    return True


def _judge(row, review) -> _Done | None:
    if _expired(str(row["created_at"]), datetime.now(timezone.utc)):
        return None
    diff = row["diff_text"]
    if not isinstance(diff, str):
        return None
    try:
        payload = review(diff)
    except ProviderError:
        return None
    findings = _as_findings(payload)
    if findings is None:
        return None
    pending = row["omitted_pending"]
    if pending is None:
        return _Done(findings, project_cost(diff), False, None)
    return _Done(findings, project_cost(diff), True, str(pending))


def _expired(created_at: str, now: datetime) -> bool:
    moment = datetime.strptime(created_at, _CREATED_AT).replace(tzinfo=timezone.utc)
    return now - moment > _STALE_AFTER


def _as_findings(payload: object) -> list | None:
    if not check_findings(payload):
        return None
    value = payload
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and "findings" in value:
        return list(value["findings"])
    if isinstance(value, dict):
        return [value]
    return None


class Worker:
    def __init__(self, store: Store, review) -> None:
        self._store = store
        self._review = review
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop,
            name="review-worker",
            daemon=True,
        )

    def start(self) -> None:
        self._store.release_queued_claims()
        self.drain()
        self._thread.start()

    def drain(self) -> None:
        while settle_one(self._store, self._review):
            pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not settle_one(self._store, self._review):
                self._stop.wait(_POLL_SECONDS)
