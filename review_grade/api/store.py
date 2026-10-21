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
from datetime import UTC, datetime, timezone

from review_grade.api.plans import (
    DEFAULT_MONTHLY_CAP_CENTS,
    billing_period_start,
    trial_ends_at,
)

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
    (
        "monthly_cap_cents",
        f"INTEGER NOT NULL DEFAULT {int(DEFAULT_MONTHLY_CAP_CENTS)}",
    ),
    # billing_anchor_day is set from the card-add date in record_card_on_file.
    ("billing_anchor_day", "INTEGER NOT NULL DEFAULT 1"),
    ("card_on_file_at", "TEXT"),
    ("trial_ends_at", "TEXT"),
    ("stripe_subscription_id", "TEXT"),
)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE,
    credits INTEGER NOT NULL,
    stripe_customer_id TEXT,
    plan TEXT NOT NULL DEFAULT 'developer',
    monthly_cap_cents INTEGER NOT NULL DEFAULT {int(DEFAULT_MONTHLY_CAP_CENTS)},
    billing_anchor_day INTEGER NOT NULL DEFAULT 1,
    card_on_file_at TEXT,
    trial_ends_at TEXT,
    stripe_subscription_id TEXT
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
CREATE TABLE IF NOT EXISTS stripe_events (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    processed_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS runs_user_idempotency
    ON runs(user_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS review_costs (
    run_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    attempts_json TEXT NOT NULL,
    total_cost_usd REAL,
    charged INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS review_costs_user_id ON review_costs(user_id);
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


@dataclass(frozen=True)
class Billing:
    plan: str
    monthly_cap_cents: int
    # queued + done in the current billing period (failed never count).
    period_reviews: int
    billing_anchor_day: int = 1
    card_on_file_at: str | None = None
    trial_ends_at: str | None = None
    stripe_customer_id: str | None = None
    stripe_subscription_id: str | None = None


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
        added_cents = False
        for name, decl in _USER_COLUMNS:
            if name not in have:
                self._conn.execute(f"ALTER TABLE users ADD COLUMN {name} {decl}")
                if name == "monthly_cap_cents":
                    added_cents = True
        have = {str(row["name"]) for row in self._conn.execute("PRAGMA table_info(users)")}
        if added_cents and "monthly_cap_usd" in have:
            # One-shot: preserve dollar values as cents. Leave monthly_cap_usd in place.
            self._conn.execute(
                "UPDATE users SET monthly_cap_cents = monthly_cap_usd * 100"
            )
        self._drop_reviews_finished_column()

    def _drop_reviews_finished_column(self) -> None:
        """Remove the lifetime counter if present. No-op on SQLite < 3.35."""
        have = {str(row["name"]) for row in self._conn.execute("PRAGMA table_info(users)")}
        if "reviews_finished" not in have:
            return
        try:
            self._conn.execute("ALTER TABLE users DROP COLUMN reviews_finished")
        except sqlite3.OperationalError:
            # DROP COLUMN needs SQLite 3.35+. Leave the unused column.
            return

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

    def billing(self, user_id: int, *, now: datetime | None = None) -> Billing:
        with self._lock:
            return self._billing(user_id, now=now)

    def set_billing(
        self,
        user_id: int,
        *,
        plan: str | None = None,
        monthly_cap_cents: int | None = None,
        billing_anchor_day: int | None = None,
    ) -> None:
        with self.transaction():
            row = self._billing(user_id)
            next_plan = row.plan if plan is None else plan
            next_cap = row.monthly_cap_cents if monthly_cap_cents is None else monthly_cap_cents
            next_anchor = (
                row.billing_anchor_day if billing_anchor_day is None else billing_anchor_day
            )
            if not isinstance(next_cap, int) or isinstance(next_cap, bool):
                raise TypeError("monthly_cap_cents must be an int")
            if next_cap < 0:
                raise ValueError("monthly_cap_cents must be >= 0")
            if not isinstance(next_anchor, int) or isinstance(next_anchor, bool):
                raise TypeError("billing_anchor_day must be an int")
            if not 1 <= next_anchor <= 31:
                raise ValueError("billing_anchor_day must be in 1..31")
            self._conn.execute(
                """
                UPDATE users
                SET plan = ?, monthly_cap_cents = ?, billing_anchor_day = ?
                WHERE id = ?
                """,
                (next_plan, next_cap, next_anchor, user_id),
            )

    def user_email(self, user_id: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT email FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                return None
            return str(row["email"])

    def stripe_customer_id(self, user_id: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT stripe_customer_id FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if row is None or row["stripe_customer_id"] is None:
                return None
            return str(row["stripe_customer_id"])

    def set_stripe_customer_id(self, user_id: int, customer_id: str) -> None:
        with self.transaction():
            self._conn.execute(
                "UPDATE users SET stripe_customer_id = ? WHERE id = ?",
                (customer_id, user_id),
            )

    def record_card_on_file(
        self,
        user_id: int,
        card_on_file_at: datetime,
        *,
        stripe_customer_id: str,
        stripe_subscription_id: str | None = None,
    ) -> dict[str, object]:
        """Set trial end and billing_anchor_day from the card-add instant.

        Leaves ``monthly_cap_cents`` unchanged (default $0). Anchor day is the
        UTC calendar day of ``card_on_file_at``.
        """
        if card_on_file_at.tzinfo is None:
            moment = card_on_file_at.replace(tzinfo=UTC)
        else:
            moment = card_on_file_at.astimezone(UTC)
        ends = trial_ends_at(moment)
        anchor_day = moment.day
        card_stamp = format_time(moment)
        ends_stamp = format_time(ends)
        with self.transaction():
            before = self._billing(user_id, now=moment)
            self._conn.execute(
                """
                UPDATE users
                SET stripe_customer_id = ?,
                    stripe_subscription_id = COALESCE(?, stripe_subscription_id),
                    card_on_file_at = ?,
                    trial_ends_at = ?,
                    billing_anchor_day = ?
                WHERE id = ?
                """,
                (
                    stripe_customer_id,
                    stripe_subscription_id,
                    card_stamp,
                    ends_stamp,
                    anchor_day,
                    user_id,
                ),
            )
            after = self._billing(user_id, now=moment)
            if after.monthly_cap_cents != before.monthly_cap_cents:
                raise RuntimeError("record_card_on_file must not change monthly_cap_cents")
        return {
            "billing_anchor_day": anchor_day,
            "trial_ends_at": ends_stamp,
            "card_on_file_at": card_stamp,
            "monthly_cap_cents": after.monthly_cap_cents,
        }

    def claim_stripe_event(self, event_id: str, event_type: str) -> bool:
        """Insert a Stripe event id. False when the id was already processed."""
        with self.transaction():
            try:
                self._conn.execute(
                    """
                    INSERT INTO stripe_events (id, type, processed_at)
                    VALUES (?, ?, ?)
                    """,
                    (event_id, event_type, format_time(datetime.now(UTC))),
                )
            except sqlite3.IntegrityError:
                return False
            return True

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
        created_at: str | None = None,
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
                created_at=created_at,
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

    def save_review_cost(self, record) -> None:
        """Insert-only cost row for one review. Not exposed on GET /v1/runs.

        A lost claim must never overwrite a winner's row: conflict is ignored.
        """
        total = record.total_cost_usd
        with self.transaction():
            self._conn.execute(
                """
                INSERT OR IGNORE INTO review_costs (
                    run_id, user_id, attempts_json, total_cost_usd, charged, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.run_id,
                    int(record.account),
                    json.dumps(
                        [
                            {
                                "model": attempt.model,
                                "input_tokens": attempt.input_tokens,
                                "output_tokens": attempt.output_tokens,
                                "cost_usd": attempt.cost_usd,
                                "usage_known": attempt.usage_known,
                                "priced": attempt.priced,
                            }
                            for attempt in record.attempts
                        ]
                    ),
                    None if total is None else float(total),
                    1 if record.charged else 0,
                    format_time(datetime.now(UTC)),
                ),
            )

    def get_review_cost(self, run_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT run_id, user_id, attempts_json, total_cost_usd, charged
                FROM review_costs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            total = row["total_cost_usd"]
            return {
                "run_id": str(row["run_id"]),
                "account": int(row["user_id"]),
                "attempts": json.loads(row["attempts_json"]),
                "total_cost_usd": None if total is None else float(total),
                "charged": bool(row["charged"]),
            }

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
            # credits is legacy and unused for access; period/cap gating lives in accept.
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
                raise RuntimeError("queued run disappeared while marking done")
            return "done"

    def _user_id(self, email: str) -> int | None:
        row = self._conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if row is None:
            return None
        return int(row["id"])

    def _insert_user(self, email: str, credits: int) -> int:
        # billing_anchor_day stays 1 until record_card_on_file sets the card day.
        cursor = self._conn.execute(
            """
            INSERT INTO users (
                email, credits, stripe_customer_id, plan,
                monthly_cap_cents, billing_anchor_day,
                card_on_file_at, trial_ends_at, stripe_subscription_id
            ) VALUES (?, ?, NULL, 'developer', ?, 1, NULL, NULL, NULL)
            """,
            (email, credits, int(DEFAULT_MONTHLY_CAP_CENTS)),
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

    def _billing(self, user_id: int, *, now: datetime | None = None) -> Billing:
        row = self._conn.execute(
            """
            SELECT plan, monthly_cap_cents, billing_anchor_day,
                   card_on_file_at, trial_ends_at,
                   stripe_customer_id, stripe_subscription_id
            FROM users WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        if row is None:
            return Billing("developer", int(DEFAULT_MONTHLY_CAP_CENTS), 0, 1)
        anchor = int(row["billing_anchor_day"])
        since = format_time(billing_period_start(now, anchor_day=anchor))
        period_reviews = self._count_period_reviews(user_id, since)
        return Billing(
            str(row["plan"]),
            int(row["monthly_cap_cents"]),
            period_reviews,
            anchor,
            None if row["card_on_file_at"] is None else str(row["card_on_file_at"]),
            None if row["trial_ends_at"] is None else str(row["trial_ends_at"]),
            None if row["stripe_customer_id"] is None else str(row["stripe_customer_id"]),
            None
            if row["stripe_subscription_id"] is None
            else str(row["stripe_subscription_id"]),
        )

    def _count_period_reviews(self, user_id: int, since: str) -> int:
        """Billable in-period runs: accepted (queued) and done. Failed never count."""
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS n FROM runs
            WHERE user_id = ?
              AND created_at >= ?
              AND status IN ('queued', 'done')
            """,
            (user_id, since),
        ).fetchone()
        return int(row["n"])

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
        created_at: str | None,
    ) -> str:
        run_id = "run_" + secrets.token_urlsafe(12)
        when = created_at if created_at is not None else format_time(datetime.now(UTC))
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
                when,
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
