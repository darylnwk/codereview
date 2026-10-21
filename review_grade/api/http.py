"""Parse HTTP, ask the gate, and write the status."""

from __future__ import annotations

import json
import socketserver
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote, urlsplit

from review_grade.api.cost import over_cap
from review_grade.api.gate import (
    WINDOW_SECONDS,
    Accepted,
    GetMeOp,
    GetRunOp,
    PostOp,
    Refused,
    Replay,
    ShowCredits,
    ShowRun,
    decide,
    refuse,
)
from review_grade.api.store import Store, format_time


class ReviewServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    store: Store


def bind(db_path: str, port: int) -> ReviewServer:
    store = Store(db_path)
    server = ReviewServer(("127.0.0.1", port), _handler(store))
    server.store = store
    return server


def _handler(store: Store):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            token = _bearer(self.headers.get("Authorization"))
            if path == "/v1/me":
                status, body = read_me(store, token)
            elif path.startswith("/v1/runs/"):
                run_id = unquote(path[len("/v1/runs/") :])
                if run_id == "" or "/" in run_id:
                    status, body = read_run(store, token, None)
                else:
                    status, body = read_run(store, token, run_id)
            else:
                status, body = read_run(store, token, None)
            _send(self, status, body)

        def do_POST(self) -> None:
            raw = _read_body(self)
            path = urlsplit(self.path).path
            token = _bearer(self.headers.get("Authorization"))
            price_cap = bool(getattr(self.server, "price_cap", False))
            if path != "/v1/reviews":
                status, body = read_run(store, token, None)
            else:
                status, body = accept_review(
                    store,
                    token,
                    _idempotency(self),
                    raw,
                    price_cap=price_cap,
                )
            _send(self, status, body)

        def log_message(self, fmt: str, *args) -> None:
            return

    return Handler


def accept_review(
    store: Store,
    token: str | None,
    idempotency_key: str | None,
    raw: bytes | None,
    *,
    price_cap: bool = False,
) -> tuple[int, dict]:
    payload = _decode(raw)
    with store.transaction():
        key = store.authenticate(token) if token else None
        credits = 0
        prior_run_id = None
        prior_hash = None
        accepts = 0
        if key is not None:
            credits = store.credits(key.user_id)
            if idempotency_key is not None:
                prior = store.idempotent_run(key.user_id, idempotency_key)
                if prior is not None:
                    prior_run_id, prior_hash = prior
            since = format_time(datetime.now(timezone.utc) - timedelta(seconds=WINDOW_SECONDS))
            accepts = store.count_recent_runs(key.id, since)
        decision = decide(
            PostOp(
                key_ok=key is not None,
                credits=credits,
                payload=payload,
                idempotency_key=idempotency_key,
                prior_run_id=prior_run_id,
                prior_diff_hash=prior_hash,
                accepts_in_window=accepts,
            )
        )
        if isinstance(decision, Accepted) and key is not None:
            if price_cap and over_cap(decision.kept_diff):
                denied = refuse("too_expensive")
                return denied.status, _error_body(denied)
            pending = None
            if decision.omitted is not None:
                pending = json.dumps(list(decision.omitted))
            run_id = store.insert_queued(
                user_id=key.user_id,
                key_id=key.id,
                diff_hash=decision.diff_hash,
                diff_text=decision.kept_diff,
                source=decision.source,
                idempotency_key=idempotency_key,
                omitted_pending=pending,
            )
            return decision.status, {"run_id": run_id}
        if isinstance(decision, Replay):
            return decision.status, {"run_id": decision.run_id}
        if isinstance(decision, Refused):
            return decision.status, _error_body(decision)
        raise RuntimeError(type(decision))


def read_me(store: Store, token: str | None) -> tuple[int, dict]:
    with store.transaction():
        key = store.authenticate(token) if token else None
        credits = store.credits(key.user_id) if key is not None else 0
        decision = decide(GetMeOp(key_ok=key is not None))
        if isinstance(decision, ShowCredits):
            return decision.status, {"credits": credits}
        return decision.status, _error_body(decision)


def read_run(store: Store, token: str | None, run_id: str | None) -> tuple[int, dict]:
    with store.transaction():
        key = store.authenticate(token) if token else None
        row = None
        if key is not None and run_id is not None:
            row = store.get_run(key.user_id, run_id)
        decision = decide(GetRunOp(key_ok=key is not None, visible=row is not None))
        if isinstance(decision, ShowRun):
            return decision.status, _run_body(row)
        return decision.status, _error_body(decision)


def _run_body(row) -> dict:
    findings = None if row["findings_json"] is None else json.loads(row["findings_json"])
    if row["omitted_json"] is None:
        omitted = []
    else:
        omitted = json.loads(row["omitted_json"])
    return {
        "status": row["status"],
        "findings": findings,
        "partial": bool(row["partial"]),
        "omitted": omitted,
        "cost_usd": row["cost_usd"],
    }


def _error_body(decision: Refused) -> dict:
    body = {"error": decision.code, "message": decision.message}
    if decision.files is not None:
        body["files"] = list(decision.files)
    return body


def _decode(raw: bytes | None) -> object:
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _bearer(header: str | None) -> str | None:
    if header is None:
        return None
    scheme, sep, rest = header.partition(" ")
    if sep != " " or scheme != "Bearer":
        return None
    token = rest.strip()
    if token == "" or " " in token:
        return None
    return token


def _idempotency(handler: BaseHTTPRequestHandler) -> str | None:
    value = handler.headers.get("Idempotency-Key")
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    return value


def _read_body(handler: BaseHTTPRequestHandler) -> bytes | None:
    raw_length = handler.headers.get("Content-Length")
    if raw_length is None:
        return b""
    try:
        length = int(raw_length)
    except ValueError:
        return None
    if length < 0:
        return None
    return handler.rfile.read(length)


def _send(handler: BaseHTTPRequestHandler, status: int, body: dict) -> None:
    encoded = json.dumps(body).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)
