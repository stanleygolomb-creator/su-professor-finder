"""
auth.py — SQLite-based user accounts + session helpers.

TODO: Render (and similar ephemeral-filesystem hosts) wipes the disk on each
deploy. The DB is lost on redeploy. For production persistence, migrate to a
hosted DB (Postgres, PlanetScale, etc.) or mount a persistent disk volume.
"""

import os
import sqlite3
import time
from flask import session
from werkzeug.security import generate_password_hash, check_password_hash

# DB path configurable via env; default next to this file.
_DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.db")
DB_PATH = os.environ.get("DB_PATH", _DEFAULT_DB)


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db():
    """Create the users table if it does not already exist."""
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at REAL NOT NULL,
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT,
                subscription_status TEXT DEFAULT 'inactive',
                subscription_expires_at REAL DEFAULT 0
            )
        """)
        conn.commit()


# ── CRUD ──────────────────────────────────────────────────────────────────────

def _row_to_dict(row):
    if row is None:
        return None
    return dict(row)


def get_user_by_id(user_id):
    """Return user dict or None."""
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_dict(row)


def get_user_by_email(email):
    """Return user dict or None."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
        ).fetchone()
    return _row_to_dict(row)


def create_user(email, password):
    """
    Create a new user. Returns the user dict on success, None if the email
    is already taken.
    """
    email = email.lower().strip()
    password_hash = generate_password_hash(password)
    try:
        with _get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO users (email, password_hash, created_at)
                   VALUES (?, ?, ?)""",
                (email, password_hash, time.time()),
            )
            conn.commit()
            return get_user_by_id(cur.lastrowid)
    except sqlite3.IntegrityError:
        # UNIQUE constraint on email failed
        return None


def verify_password(email, password):
    """
    Check email + password. Returns user dict on success, None on failure.
    """
    user = get_user_by_email(email)
    if not user:
        return None
    if not check_password_hash(user["password_hash"], password):
        return None
    return user


# ── Session helpers ───────────────────────────────────────────────────────────

def current_user():
    """Read Flask session, return user dict or None."""
    user_id = session.get("user_id")
    if not user_id:
        return None
    return get_user_by_id(user_id)


def login_user(user):
    """Set the Flask session user_id."""
    session["user_id"] = user["id"]


def logout_user():
    """Clear user_id from the Flask session."""
    session.pop("user_id", None)


# ── Subscription helpers ──────────────────────────────────────────────────────

def update_subscription(user_id, customer_id, sub_id, status, expires_at):
    """Persist Stripe subscription details for a logged-in user."""
    with _get_conn() as conn:
        conn.execute(
            """UPDATE users
               SET stripe_customer_id = ?,
                   stripe_subscription_id = ?,
                   subscription_status = ?,
                   subscription_expires_at = ?
               WHERE id = ?""",
            (customer_id, sub_id, status, float(expires_at), user_id),
        )
        conn.commit()


def is_active(user) -> bool:
    """
    True if the user has an active or trialing subscription that has not
    yet expired according to the stored period end timestamp.
    """
    if not user:
        return False
    status = user.get("subscription_status", "inactive")
    expires_at = user.get("subscription_expires_at", 0) or 0
    return status in ("active", "trialing") and float(expires_at) > time.time()
