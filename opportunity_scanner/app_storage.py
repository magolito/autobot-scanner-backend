"""
App storage — the central users/subscriptions database, deliberately
SEPARATE from opportunity_scanner.db and meme_scanner.db (which store
scan history, not accounts). This separation matters once there are
multiple users: scan data is shared/global (everyone's Scan Now hits the
same underlying market), but accounts, plans, and subscription status
are per-user and need their own table with proper constraints (unique
email, etc.) that scan-history tables don't need.

This is Stage 1 foundation work: the data model real login (Stage 2) and
real billing (Stage 3) will consume. It does NOT wire into either
dashboard's actual auth flow yet — both dashboards still use the shared
DASHBOARD_PASSWORD today. That's intentional: swapping the login UI over
to check against this table is Stage 2's job specifically, not bundled
in here, so each stage stays reviewable on its own.
"""

from __future__ import annotations
import sqlite3
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel

from .auth_utils import hash_password, verify_password
from .plans import PlanTier

DEFAULT_APP_DB_PATH = "app_users.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT 'free',
    stripe_customer_id TEXT,
    subscription_status TEXT NOT NULL DEFAULT 'none',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_stripe_customer ON users(stripe_customer_id);

CREATE TABLE IF NOT EXISTS scan_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    scanner TEXT NOT NULL,
    scan_date TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id, scanner, scan_date)
);
CREATE INDEX IF NOT EXISTS idx_scan_usage_lookup ON scan_usage(user_id, scanner, scan_date);
"""


class User(BaseModel):
    id: int
    email: str
    plan: PlanTier
    stripe_customer_id: Optional[str] = None
    subscription_status: str
    created_at: str
    updated_at: str


class AppStorage:
    def __init__(self, db_path: str = DEFAULT_APP_DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> User:
        return User(
            id=row["id"], email=row["email"], plan=PlanTier(row["plan"]),
            stripe_customer_id=row["stripe_customer_id"], subscription_status=row["subscription_status"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    # -------------------------------------------------------------- create

    def create_user(self, email: str, password: str, plan: PlanTier = PlanTier.FREE) -> Optional[User]:
        """Returns None (not a raised exception) if the email is already
        registered — a duplicate-email attempt is an expected, ordinary
        outcome for a registration form to handle, not an error condition."""
        email_normalized = email.strip().lower()
        password_hash = hash_password(password)
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            try:
                cursor = conn.execute(
                    "INSERT INTO users (email, password_hash, plan, subscription_status, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'none', ?, ?)",
                    (email_normalized, password_hash, plan.value, now, now),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                return None  # email already registered
            row = conn.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return self._row_to_user(row)
        finally:
            conn.close()

    # ---------------------------------------------------------------- read

    def get_user_by_email(self, email: str) -> Optional[User]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
            return self._row_to_user(row) if row else None
        finally:
            conn.close()

    def get_user_by_id(self, user_id: int) -> Optional[User]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return self._row_to_user(row) if row else None
        finally:
            conn.close()

    def get_user_by_stripe_customer_id(self, stripe_customer_id: str) -> Optional[User]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM users WHERE stripe_customer_id = ?", (stripe_customer_id,)).fetchone()
            return self._row_to_user(row) if row else None
        finally:
            conn.close()

    def list_users(self, limit: int = 500) -> list[User]:
        """For an admin view (Stage 5) — not exposed anywhere yet, just present."""
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [self._row_to_user(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------ auth

    def authenticate(self, email: str, password: str) -> Optional[User]:
        """Returns the User on success, None on failure — deliberately the
        same shape whether the email doesn't exist or the password is
        wrong, so a login form can't be used to enumerate registered
        emails by checking which failure message comes back."""
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
            if row is None:
                return None
            if not verify_password(password, row["password_hash"]):
                return None
            return self._row_to_user(row)
        finally:
            conn.close()

    # -------------------------------------------------------- plan/subscription updates

    def update_subscription(
        self, user_id: int, plan: Optional[PlanTier] = None,
        stripe_customer_id: Optional[str] = None, subscription_status: Optional[str] = None,
    ) -> Optional[User]:
        """Stage 3's Stripe webhook handler is the intended caller — only
        updates the fields actually passed, so a webhook that only knows
        about status changes doesn't need to also know/re-send the plan."""
        conn = self._connect()
        try:
            existing = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if existing is None:
                return None
            new_plan = plan.value if plan is not None else existing["plan"]
            new_stripe_id = stripe_customer_id if stripe_customer_id is not None else existing["stripe_customer_id"]
            new_status = subscription_status if subscription_status is not None else existing["subscription_status"]
            conn.execute(
                "UPDATE users SET plan = ?, stripe_customer_id = ?, subscription_status = ?, updated_at = ? WHERE id = ?",
                (new_plan, new_stripe_id, new_status, datetime.now(timezone.utc).isoformat(), user_id),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return self._row_to_user(row)
        finally:
            conn.close()

    def change_password(self, user_id: int, new_password: str) -> bool:
        conn = self._connect()
        try:
            result = conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (hash_password(new_password), datetime.now(timezone.utc).isoformat(), user_id),
            )
            conn.commit()
            return result.rowcount > 0
        finally:
            conn.close()

    # ---------------------------------------------------------------- scan usage (Stage 4 access control)

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def get_scan_count_today(self, user_id: int, scanner: str) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT count FROM scan_usage WHERE user_id = ? AND scanner = ? AND scan_date = ?",
                (user_id, scanner, self._today()),
            ).fetchone()
            return row["count"] if row else 0
        finally:
            conn.close()

    def increment_scan_count(self, user_id: int, scanner: str) -> int:
        """
        Atomic upsert — the UNIQUE(user_id, scanner, scan_date) constraint
        plus ON CONFLICT is what makes this safe against two rapid clicks
        racing each other, rather than a separate SELECT-then-UPDATE that
        could double-count under concurrent access. Returns the count
        AFTER incrementing, so a caller can immediately show "4 of 5 used"
        without a second query.
        """
        conn = self._connect()
        try:
            today = self._today()
            conn.execute(
                """
                INSERT INTO scan_usage (user_id, scanner, scan_date, count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(user_id, scanner, scan_date) DO UPDATE SET count = count + 1
                """,
                (user_id, scanner, today),
            )
            conn.commit()
            row = conn.execute(
                "SELECT count FROM scan_usage WHERE user_id = ? AND scanner = ? AND scan_date = ?",
                (user_id, scanner, today),
            ).fetchone()
            return row["count"]
        finally:
            conn.close()
