"""Sqlite users, API keys, and queued runs."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

SEED_EMAIL = "seed@reviewgrade.dev"
_LIVE_PREFIX = "ra_live_"
_RUN_COLUMNS = (
    ("claim_token", "TEXT"),
    ("omitted_pending", "TEXT"),
    ("cache_read", "INTEGER"),
    ("cache_write", "INTEGER"),
)
_USER_COLUMNS = (
    ("plan", "TEXT NOT NULL DEFAULT 'developer'"),
    ("monthly_cap_usd", "INTEGER NOT NULL DEFAULT 50"),
    ("reviews_finished", "INTEGER NOT NULL DEFAULT 0"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE,
    credits INTEGER NOT NULL,
    stripe_customer_id TEXT,
    plan TEXT NOT NULL DEFAULT 'developer',
    monthly_cap_usd INTEGER NOT NULL DEFAULT 50,
    reviews_finished INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    prefix TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    name TEXT,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    key_id INTEGER NOT NULL,
    diff_hash TEXT NOT NULL,
    diff_text TEXT,
    findings_json TEXT,
    status TEXT NOT NULL,
    partial INTEGER NOT NULL DEFAULT 0,
    omitted_json TEXT,
    cost_usd REAL,
    source TEXT NOT NULL,
    idempotency_key TEXT,
    created_at TEXT NOT NULL,
    claim_token TEXT,
    omitted_pending TEXT,
    cache_read INTEGER,
    cache_write INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS runs_user_idempotency
    ON runs(user_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
"""


def format_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _stored_prefix(secret: str) -> str | None:
    if not secret.startswith(_LIVE_PREFIX):
        return None
    tail = secret[len(_LIVE_PREFIX) :]
    if len(tail) < 8:
        return None
    return _LIVE_PREFIX + tail[:8]


@dataclass(frozen=True)
class Key:
    id: int
    user_id: int


@dataclass(frozen=True)
class Billing:
    plan: str
    monthly_cap_usd: int
    reviews_finished: int


class Store:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,
            timeout=30,
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL").fetchone()
        self._conn.executescript(_SCHEMA)
        self._ensure_run_columns()
        self._ensure_user_columns()

    def close(self) -> None:
        self._conn.close()

    def _ensure_run_columns(self) -> None:
        have = {str(row["name"]) for row in self._conn.execute("PRAGMA table_info(runs)")}
        for name, decl in _RUN_COLUMNS:
            if name not in have:
                self._conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {decl}")

    def _ensure_user_columns(self) -> None:
        have = {str(row["name"]) for row in self._conn.execute("PRAGMA table_info(users)")}
        for name, decl in _USER_COLUMNS:
            if name not in have:
                self._conn.execute(f"ALTER TABLE users ADD COLUMN {name} {decl}")

    @contextmanager
    def transaction(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def seed_account(self, credits: int) -> str:
        with self.transaction():
            user_id = self._user_id(SEED_EMAIL)
            if user_id is None:
                user_id = self._insert_user(SEED_EMAIL, credits)
            return self._insert_key(user_id)

    def add_user(self, email: str, credits: int) -> int:
        with self.transaction():
            return self._insert_user(email, credits)

    def new_key(self, user_id: int) -> str:
        with self.transaction():
            return self._insert_key(user_id)

    def revoke(self, secret: str) -> None:
        with self.transaction():
            self._conn.execute(
                """
                UPDATE api_keys
                SET revoked_at = ?
                WHERE secret_hash = ? AND revoked_at IS NULL
                """,
                (format_time(datetime.now(UTC)), _secret_hash(secret)),
            )

    def authenticate(self, token: str) -> Key | None:
        with self._lock:
            return self._authenticate(token)

    def credits(self, user_id: int) -> int:
        with self._lock:
            return self._credits(user_id)

    def billing(self, user_id: int) -> Billing:
        with self._lock:
            return self._billing(user_id)

    def set_billing(
        self,
        user_id: int,
        *,
        plan: str | None = None,
        monthly_cap_usd: int | None = None,
        reviews_finished: int | None = None,
    ) -> None:
        with self.transaction():
            row = self._billing(user_id)
            next_plan = row.plan if plan is None else plan
            next_cap = row.monthly_cap_usd if monthly_cap_usd is None else monthly_cap_usd
            next_finished = (
                row.reviews_finished if reviews_finished is None else reviews_finished
            )
            if next_cap < 0 or next_finished < 0:
                raise ValueError("monthly_cap_usd and reviews_finished must be >= 0")
            self._conn.execute(
                """
                UPDATE users
                SET plan = ?, monthly_cap_usd = ?, reviews_finished = ?
                WHERE id = ?
                """,
                (next_plan, next_cap, next_finished, user_id),
            )

    def idempotent_run(self, user_id: int, key: str) -> tuple[str, str] | None:
        with self._lock:
            return self._idempotent_run(user_id, key)

    def count_recent_runs(self, key_id: int, since: str) -> int:
        with self._lock:
            return self._count_recent_runs(key_id, since)

    def get_run(self, user_id: int, run_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._get_run(user_id, run_id)

    def insert_queued(
        self,
        *,
        user_id: int,
        key_id: int,
        diff_hash: str,
        diff_text: str,
        source: str,
        idempotency_key: str | None,
        omitted_pending: str | None = None,
    ) -> str:
        with self._lock:
            return self._insert_queued(
                user_id=user_id,
                key_id=key_id,
                diff_hash=diff_hash,
                diff_text=diff_text,
                source=source,
                idempotency_key=idempotency_key,
                omitted_pending=omitted_pending,
            )

    def release_queued_claims(self) -> None:
        with self.transaction():
            self._conn.execute(
                """
                UPDATE runs
                SET claim_token = NULL
                WHERE status = 'queued' AND claim_token IS NOT NULL
                """
            )

    def claim_oldest(self, token: str) -> sqlite3.Row | None:
        with self.transaction():
            cursor = self._conn.execute(
                """
                UPDATE runs
                SET claim_token = ?
                WHERE rowid = (
                    SELECT rowid FROM runs
                    WHERE status = 'queued' AND claim_token IS NULL
                    ORDER BY created_at ASC, rowid ASC
                    LIMIT 1
                )
                AND status = 'queued'
                AND claim_token IS NULL
                RETURNING id, user_id, diff_text, created_at, omitted_pending
                """,
                (token,),
            )
            return cursor.fetchone()

    def mark_failed(self, run_id: str, token: str) -> bool:
        with self.transaction():
            return (
                self._close(
                    run_id,
                    token,
                    status="failed",
                    findings_json=None,
                    cost_usd=None,
                    partial=0,
                    omitted_json=None,
                )
                == 1
            )

    def mark_done(
        self,
        run_id: str,
        token: str,
        user_id: int,
        findings: list,
        cost: float,
        partial: bool,
        omitted_json: str | None,
    ) -> str:
        with self.transaction():
            owned = self._conn.execute(
                """
                SELECT id FROM runs
                WHERE id = ? AND status = 'queued' AND claim_token = ?
                """,
                (run_id, token),
            ).fetchone()
            if owned is None:
                return "lost"
            credited = self._conn.execute(
                """
                UPDATE users
                SET credits = credits - 1
                WHERE id = ? AND credits > 0
                """,
                (user_id,),
            ).rowcount
            if credited != 1:
                self._close(
                    run_id,
                    token,
                    status="failed",
                    findings_json=None,
                    cost_usd=None,
                    partial=0,
                    omitted_json=None,
                )
                return "failed"
            bumped = self._conn.execute(
                """
                UPDATE users
                SET reviews_finished = reviews_finished + 1
                WHERE id = ?
                """,
                (user_id,),
            ).rowcount
            if bumped != 1:
                raise RuntimeError("user row missing after the credit update")
            closed = self._close(
                run_id,
                token,
                status="done",
                findings_json=json.dumps(findings),
                cost_usd=cost,
                partial=1 if partial else 0,
                omitted_json=omitted_json,
            )
            if closed != 1:
                raise RuntimeError("queued run disappeared after the credit update")
            return "done"

    def _user_id(self, email: str) -> int | None:
        row = self._conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if row is None:
            return None
        return int(row["id"])

    def _insert_user(self, email: str, credits: int) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO users (
                email, credits, stripe_customer_id, plan, monthly_cap_usd, reviews_finished
            ) VALUES (?, ?, NULL, 'developer', 50, 0)
            """,
            (email, credits),
        )
        return int(cursor.lastrowid)

    def _insert_key(self, user_id: int) -> str:
        tail = secrets.token_urlsafe(24)
        secret = _LIVE_PREFIX + tail
        self._conn.execute(
            """
            INSERT INTO api_keys (
                user_id, prefix, secret_hash, name, last_used_at, revoked_at
            ) VALUES (?, ?, ?, NULL, NULL, NULL)
            """,
            (user_id, _LIVE_PREFIX + tail[:8], _secret_hash(secret)),
        )
        return secret

    def _authenticate(self, token: str) -> Key | None:
        digest = _secret_hash(token)
        prefix = _stored_prefix(token)
        if prefix is None:
            hmac.compare_digest(digest, digest)
            return None
        rows = self._conn.execute(
            """
            SELECT id, user_id, secret_hash, revoked_at
            FROM api_keys
            WHERE prefix = ?
            """,
            (prefix,),
        ).fetchall()
        matched = False
        for row in rows:
            if hmac.compare_digest(digest, row["secret_hash"]):
                matched = True
                if row["revoked_at"] is None:
                    return Key(int(row["id"]), int(row["user_id"]))
        if not matched:
            hmac.compare_digest(digest, digest)
        return None

    def _credits(self, user_id: int) -> int:
        row = self._conn.execute(
            "SELECT credits FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            return 0
        return int(row["credits"])

    def _billing(self, user_id: int) -> Billing:
        row = self._conn.execute(
            """
            SELECT plan, monthly_cap_usd, reviews_finished
            FROM users WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        if row is None:
            return Billing("developer", 50, 0)
        return Billing(str(row["plan"]), int(row["monthly_cap_usd"]), int(row["reviews_finished"]))

    def _idempotent_run(self, user_id: int, key: str) -> tuple[str, str] | None:
        row = self._conn.execute(
            "SELECT id, diff_hash FROM runs WHERE user_id = ? AND idempotency_key = ?",
            (user_id, key),
        ).fetchone()
        if row is None:
            return None
        return str(row["id"]), str(row["diff_hash"])

    def _count_recent_runs(self, key_id: int, since: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE key_id = ? AND created_at >= ?",
            (key_id, since),
        ).fetchone()
        return int(row["n"])

    def _get_run(self, user_id: int, run_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM runs WHERE id = ? AND user_id = ?",
            (run_id, user_id),
        ).fetchone()

    def _insert_queued(
        self,
        *,
        user_id: int,
        key_id: int,
        diff_hash: str,
        diff_text: str,
        source: str,
        idempotency_key: str | None,
        omitted_pending: str | None,
    ) -> str:
        run_id = "run_" + secrets.token_urlsafe(12)
        self._conn.execute(
            """
            INSERT INTO runs (
                id, user_id, key_id, diff_hash, diff_text, findings_json, status,
                partial, omitted_json, cost_usd, source, idempotency_key, created_at,
                omitted_pending
            ) VALUES (?, ?, ?, ?, ?, NULL, 'queued', 0, NULL, NULL, ?, ?, ?, ?)
            """,
            (
                run_id,
                user_id,
                key_id,
                diff_hash,
                diff_text,
                source,
                idempotency_key,
                format_time(datetime.now(UTC)),
                omitted_pending,
            ),
        )
        return run_id

    def _close(
        self,
        run_id: str,
        token: str,
        *,
        status: str,
        findings_json: str | None,
        cost_usd: float | None,
        partial: int,
        omitted_json: str | None,
    ) -> int:
        cursor = self._conn.execute(
            """
            UPDATE runs
            SET status = ?,
                findings_json = ?,
                diff_text = NULL,
                cost_usd = ?,
                partial = ?,
                omitted_json = ?,
                claim_token = NULL,
                cache_read = NULL,
                cache_write = NULL
            WHERE id = ? AND status = 'queued' AND claim_token = ?
            """,
            (
                status,
                findings_json,
                cost_usd,
                partial,
                omitted_json,
                run_id,
                token,
            ),
        )
        return int(cursor.rowcount)
