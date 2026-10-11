"""Sqlite users, API keys, and queued runs."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

SEED_EMAIL = "seed@reviewgrade.dev"
_LIVE_PREFIX = "ra_live_"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE,
    credits INTEGER NOT NULL,
    stripe_customer_id TEXT
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
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS runs_user_idempotency
    ON runs(user_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
"""


def format_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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

    def close(self) -> None:
        self._conn.close()

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
                (format_time(datetime.now(timezone.utc)), _secret_hash(secret)),
            )

    def authenticate(self, token: str) -> Key | None:
        with self._lock:
            return self._authenticate(token)

    def credits(self, user_id: int) -> int:
        with self._lock:
            return self._credits(user_id)

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
    ) -> str:
        with self._lock:
            return self._insert_queued(
                user_id=user_id,
                key_id=key_id,
                diff_hash=diff_hash,
                diff_text=diff_text,
                source=source,
                idempotency_key=idempotency_key,
            )

    def _user_id(self, email: str) -> int | None:
        row = self._conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if row is None:
            return None
        return int(row["id"])

    def _insert_user(self, email: str, credits: int) -> int:
        cursor = self._conn.execute(
            "INSERT INTO users (email, credits, stripe_customer_id) VALUES (?, ?, NULL)",
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
    ) -> str:
        run_id = "run_" + secrets.token_urlsafe(12)
        self._conn.execute(
            """
            INSERT INTO runs (
                id, user_id, key_id, diff_hash, diff_text, findings_json, status,
                partial, omitted_json, cost_usd, source, idempotency_key, created_at
            ) VALUES (?, ?, ?, ?, ?, NULL, 'queued', 0, NULL, NULL, ?, ?, ?)
            """,
            (
                run_id,
                user_id,
                key_id,
                diff_hash,
                diff_text,
                source,
                idempotency_key,
                format_time(datetime.now(timezone.utc)),
            ),
        )
        return run_id
