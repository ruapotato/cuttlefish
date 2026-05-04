"""Password hashing (scrypt) and session helpers.

Uses only stdlib: hashlib.scrypt for KDF, secrets for tokens. No bcrypt /
argon2 dependencies — scrypt is a memory-hard KDF that's perfectly fine for
a self-hosted media server's user count.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

# scrypt parameters: ~16 MiB working set (128 * N * r). Fast enough that
# login is snappy but slow enough that brute force costs real CPU. We pass
# maxmem explicitly so OpenSSL doesn't reject us with "memory limit exceeded"
# when its compile-time default is set just at our working set.
_N = 2 ** 14
_R = 8
_P = 1
_DKLEN = 32
_SALT_BYTES = 16
_MAXMEM = 64 * 1024 * 1024

SESSION_COOKIE_NAME = "cuttlefish_session"
SESSION_TTL = timedelta(days=30)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    h = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN, maxmem=_MAXMEM
    )
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"


def verify_password(password: str, stored: str) -> bool:
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = base64.b64decode(parts[4])
        expected = base64.b64decode(parts[5])
    except (ValueError, base64.binascii.Error):
        return False
    h = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected), maxmem=_MAXMEM
    )
    return secrets.compare_digest(h, expected)


def create_user(
    conn: sqlite3.Connection, username: str, password: str, is_admin: bool = False
) -> int:
    pw_hash = hash_password(password)
    with conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
            (username, pw_hash, 1 if is_admin else 0),
        )
    return cur.lastrowid


def find_user_by_username(conn: sqlite3.Connection, username: str):
    return conn.execute(
        "SELECT id, username, password_hash, is_admin FROM users WHERE username = ?",
        (username,),
    ).fetchone()


def user_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]


def create_session(conn: sqlite3.Connection, user_id: int) -> tuple[str, datetime]:
    token = secrets.token_hex(32)
    expires_at = datetime.now(timezone.utc) + SESSION_TTL
    with conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
            (token, user_id, expires_at.isoformat(sep=" ", timespec="seconds")),
        )
    return token, expires_at


def lookup_session(conn: sqlite3.Connection, token: str):
    if not token:
        return None
    return conn.execute(
        """
        SELECT u.id, u.username, u.is_admin, s.expires_at
        FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.token = ? AND datetime(s.expires_at) > datetime('now')
        """,
        (token,),
    ).fetchone()


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    if not token:
        return
    with conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def purge_expired_sessions(conn: sqlite3.Connection) -> int:
    with conn:
        cur = conn.execute(
            "DELETE FROM sessions WHERE datetime(expires_at) <= datetime('now')"
        )
    return cur.rowcount or 0


_dummy_hash_cache: Optional[str] = None


def _dummy_hash() -> str:
    """A real, valid scrypt hash used to keep authenticate() timing roughly
    constant whether or not the username exists. Computed lazily once."""
    global _dummy_hash_cache
    if _dummy_hash_cache is None:
        _dummy_hash_cache = hash_password("dummy-password-for-constant-time")
    return _dummy_hash_cache


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> Optional[int]:
    """Return user_id on success, None on failure. Roughly constant time."""
    row = find_user_by_username(conn, username)
    if row is None:
        verify_password(password, _dummy_hash())
        return None
    if verify_password(password, row["password_hash"]):
        return row["id"]
    return None
