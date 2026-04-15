import sqlite3
import bcrypt
import jwt
import datetime
import os
import json
import secrets

DB_PATH    = os.environ.get("DATABASE_PATH", "/tmp/pf_users.db")
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-in-prod")
AUTH_COOKIE = "pf_auth"
COOKIE_DAYS = 365 * 10


# ── Database ──────────────────────────────────────────────────────────────────

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with _db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT    UNIQUE NOT NULL COLLATE NOCASE,
                password_hash TEXT    NOT NULL,
                created_at    TEXT    DEFAULT (datetime('now')),
                premium       INTEGER DEFAULT 0,
                stripe_session TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS bookmarks (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                ref_id     TEXT    NOT NULL,
                type       TEXT    NOT NULL DEFAULT 'professor',
                name       TEXT,
                dept       TEXT,
                rating     REAL,
                data       TEXT,
                created_at TEXT    DEFAULT (datetime('now')),
                UNIQUE(user_id, ref_id),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        # Add email_verified column if it doesn't exist yet (migration-safe)
        try:
            c.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER DEFAULT 0")
        except Exception:
            pass
        c.execute("""
            CREATE TABLE IF NOT EXISTS reset_tokens (
                token      TEXT    PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                expires_at TEXT    NOT NULL,
                used       INTEGER DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS professor_views (
                prof_id    TEXT    NOT NULL,
                school_id  TEXT    NOT NULL DEFAULT '',
                name       TEXT,
                dept       TEXT,
                rating     REAL,
                views      INTEGER DEFAULT 1,
                last_seen  TEXT    DEFAULT (datetime('now')),
                PRIMARY KEY (prof_id, school_id)
            )
        """)
        c.commit()


# ── Professor view tracking ───────────────────────────────────────────────────

def record_professor_view(prof_id: str, school_id: str, name: str = "", dept: str = "", rating=None):
    """Upsert a view for this professor — called every time their detail is loaded."""
    try:
        with _db() as c:
            c.execute("""
                INSERT INTO professor_views (prof_id, school_id, name, dept, rating, views, last_seen)
                VALUES (?, ?, ?, ?, ?, 1, datetime('now'))
                ON CONFLICT(prof_id, school_id) DO UPDATE SET
                    views    = views + 1,
                    last_seen = datetime('now'),
                    name     = excluded.name,
                    dept     = excluded.dept,
                    rating   = excluded.rating
            """, (prof_id, school_id, name, dept, rating))
            c.commit()
    except Exception:
        pass


def get_trending_professors(school_id: str = "", limit: int = 8) -> list:
    """Return top professors by view count for this school in the last 14 days."""
    try:
        with _db() as c:
            rows = c.execute("""
                SELECT prof_id, name, dept, rating, views
                FROM professor_views
                WHERE school_id = ?
                  AND last_seen >= datetime('now', '-14 days')
                ORDER BY views DESC
                LIMIT ?
            """, (school_id, limit)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ── Users ─────────────────────────────────────────────────────────────────────

def create_user(email: str, password: str):
    """Create a new user. Returns (user_row, None) or (None, error_str)."""
    email = email.strip().lower()
    if not email or "@" not in email:
        return None, "Invalid email address."
    if len(password) < 6:
        return None, "Password must be at least 6 characters."
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    try:
        with _db() as c:
            c.execute("INSERT INTO users (email, password_hash) VALUES (?, ?)", (email, pw_hash))
            c.commit()
            row = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            return dict(row), None
    except sqlite3.IntegrityError:
        return None, "An account with that email already exists."
    except Exception as e:
        return None, str(e)


def verify_user(email: str, password: str):
    """Returns user dict if credentials are valid, else None."""
    email = email.strip().lower()
    with _db() as c:
        row = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not row:
        return None
    if bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
        return dict(row)
    return None


def get_user(user_id: int):
    with _db() as c:
        row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def set_premium(user_id: int, stripe_session: str):
    with _db() as c:
        c.execute("UPDATE users SET premium = 1, stripe_session = ? WHERE id = ?",
                  (stripe_session, user_id))
        c.commit()


def get_user_by_email(email: str):
    email = email.strip().lower()
    with _db() as c:
        row = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return dict(row) if row else None


# ── Password reset ────────────────────────────────────────────────────────────

def create_reset_token(email: str):
    """Create a password-reset token. Returns (token, None) or (None, error)."""
    user = get_user_by_email(email)
    if not user:
        # Don't reveal whether email exists
        return secrets.token_urlsafe(32), None
    token = secrets.token_urlsafe(32)
    expires = (datetime.datetime.utcnow() + datetime.timedelta(hours=2)).isoformat()
    try:
        with _db() as c:
            # Invalidate old tokens for this user
            c.execute("DELETE FROM reset_tokens WHERE user_id = ?", (user["id"],))
            c.execute("INSERT INTO reset_tokens (token, user_id, expires_at) VALUES (?,?,?)",
                      (token, user["id"], expires))
            c.commit()
        return token, None
    except Exception as e:
        return None, str(e)


def consume_reset_token(token: str):
    """Returns user_id if token is valid + unused + not expired, else None."""
    try:
        with _db() as c:
            row = c.execute(
                "SELECT * FROM reset_tokens WHERE token=? AND used=0", (token,)
            ).fetchone()
            if not row:
                return None
            if datetime.datetime.utcnow().isoformat() > row["expires_at"]:
                return None
            c.execute("UPDATE reset_tokens SET used=1 WHERE token=?", (token,))
            c.commit()
            return row["user_id"]
    except Exception:
        return None


def set_password(user_id: int, new_password: str):
    pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    with _db() as c:
        c.execute("UPDATE users SET password_hash=? WHERE id=?", (pw_hash, user_id))
        c.commit()


# ── JWT auth tokens ───────────────────────────────────────────────────────────

def make_token(user_id: int, email: str, premium: bool) -> str:
    payload = {
        "uid":     user_id,
        "email":   email,
        "premium": premium,
        "iat":     datetime.datetime.utcnow(),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def decode_token(token: str):
    """Returns payload dict or None."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        return None


def get_current_user(request):
    """Extract logged-in user from request cookies. Returns payload dict or None."""
    token = request.cookies.get(AUTH_COOKIE)
    if not token:
        return None
    return decode_token(token)


def set_auth_cookie(response, user_id: int, email: str, premium: bool):
    token = make_token(user_id, email, premium)
    response.set_cookie(
        AUTH_COOKIE, token,
        max_age=60 * 60 * 24 * COOKIE_DAYS,
        httponly=True, secure=True, samesite="Lax",
    )
    return response


def clear_auth_cookie(response):
    response.delete_cookie(AUTH_COOKIE)
    return response


# ── Bookmarks ─────────────────────────────────────────────────────────────────

def add_bookmark(user_id: int, ref_id: str, name: str, dept: str, rating, data: dict):
    try:
        with _db() as c:
            c.execute(
                "INSERT OR REPLACE INTO bookmarks (user_id, ref_id, name, dept, rating, data) VALUES (?,?,?,?,?,?)",
                (user_id, ref_id, name, dept, rating, json.dumps(data))
            )
            c.commit()
        return True
    except Exception:
        return False


def remove_bookmark(user_id: int, ref_id: str):
    with _db() as c:
        c.execute("DELETE FROM bookmarks WHERE user_id = ? AND ref_id = ?", (user_id, ref_id))
        c.commit()


def get_bookmarks(user_id: int) -> list:
    with _db() as c:
        rows = c.execute(
            "SELECT * FROM bookmarks WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,)
        ).fetchall()
    result = []
    for r in rows:
        item = dict(r)
        try:
            item["data"] = json.loads(item["data"] or "{}")
        except Exception:
            item["data"] = {}
        result.append(item)
    return result


def is_bookmarked(user_id: int, ref_id: str) -> bool:
    with _db() as c:
        row = c.execute(
            "SELECT id FROM bookmarks WHERE user_id = ? AND ref_id = ?",
            (user_id, ref_id)
        ).fetchone()
    return row is not None
