"""Account management: registration, login, sessions and profile data.

Design constraints mirrored from the rest of the app:

* stdlib only — no new dependency just to hash a password.  ``hashlib.scrypt``
  with a per-user random salt is part of Python and is a sound choice here.
* sessions are opaque random tokens stored server-side, so a logout really
  invalidates them and nothing security-relevant lives in the browser.
* every profile field is optional except the credentials, because the user
  should be able to sign up first and fill in details later.

Because profile data and papers both live in the same SQLite file keyed by
``user_fk``, signing in from another device shows exactly the same library.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import Database, now

SESSION_DAYS = 30
"""How long a login stays valid; refreshed on every authenticated request."""

SESSION_COOKIE = "mesoscope_session"

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")

# scrypt parameters: interactive-login cost, ~16 MB per hash.
_N, _R, _P = 2**14, 8, 1
_DKLEN = 32

PROFILE_FIELDS: tuple[str, ...] = (
    "display_name",
    "email",
    "institution",
    "research_area",
    "bio",
)

_FIELD_LIMITS: dict[str, int] = {
    "display_name": 60,
    "email": 120,
    "institution": 120,
    "research_area": 120,
    "bio": 1000,
}


class AuthError(Exception):
    """Raised for every credential / permission problem, with a safe message."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# password hashing
# --------------------------------------------------------------------------- #

def hash_password(password: str) -> str:
    """``scrypt$salt$digest``, both halves hex-encoded."""
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$", 2)
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    actual = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN
    )
    # Constant-time: never leak how much of the digest matched.
    return hmac.compare_digest(actual, expected)


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

def _check_username(raw: Any) -> str:
    username = str(raw or "").strip()
    if not USERNAME_RE.match(username):
        raise AuthError("用户名需为 3-32 位字母、数字、下划线、点或连字符")
    return username


def _check_password(raw: Any) -> str:
    password = str(raw or "")
    if len(password) < 6:
        raise AuthError("密码至少 6 位")
    if len(password) > 128:
        raise AuthError("密码过长（最多 128 位）")
    return password


def clean_profile(body: dict[str, Any]) -> dict[str, str]:
    """Keep only known profile fields, trimmed and length-capped."""
    out: dict[str, str] = {}
    for key in PROFILE_FIELDS:
        if key not in body:
            continue
        value = str(body.get(key) or "").strip()[: _FIELD_LIMITS[key]]
        if key == "email" and value and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value):
            raise AuthError("邮箱格式不正确")
        out[key] = value
    return out


def user_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Public view of a user: never includes the password hash."""
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row.get("display_name") or row["username"],
        "email": row.get("email") or "",
        "institution": row.get("institution") or "",
        "research_area": row.get("research_area") or "",
        "bio": row.get("bio") or "",
        "created_at": row.get("created_at") or "",
        "updated_at": row.get("updated_at") or "",
    }


# --------------------------------------------------------------------------- #
# accounts
# --------------------------------------------------------------------------- #

class Accounts:
    """All account operations for one database."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # -- registration / login ---------------------------------------------- #

    def register(self, body: dict[str, Any]) -> dict[str, Any]:
        username = _check_username(body.get("username"))
        password = _check_password(body.get("password"))
        if self.db.one("SELECT id FROM users WHERE username = ?", (username,)):
            raise AuthError("该用户名已被注册", 409)

        profile = clean_profile(body)
        stamp = now()
        user_fk = self.db.execute(
            """
            INSERT INTO users (username, password_hash, display_name, email, institution,
                               research_area, bio, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                username,
                hash_password(password),
                profile.get("display_name") or username,
                profile.get("email", ""),
                profile.get("institution", ""),
                profile.get("research_area", ""),
                profile.get("bio", ""),
                stamp,
                stamp,
            ),
        )
        return self.require_user(user_fk)

    def login(self, body: dict[str, Any]) -> dict[str, Any]:
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        row = self.db.one("SELECT * FROM users WHERE username = ?", (username,))
        # Same message either way: do not reveal whether the account exists.
        if not row or not verify_password(password, row["password_hash"]):
            raise AuthError("用户名或密码不正确", 401)
        return row

    def change_password(self, user_fk: int, body: dict[str, Any]) -> None:
        row = self.require_user_row(user_fk)
        if not verify_password(str(body.get("old_password") or ""), row["password_hash"]):
            raise AuthError("当前密码不正确", 403)
        new_password = _check_password(body.get("new_password"))
        self.db.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (hash_password(new_password), now(), user_fk),
        )
        # A password change invalidates other devices' sessions.
        self.db.execute("DELETE FROM sessions WHERE user_fk = ?", (user_fk,))

    # -- profile ------------------------------------------------------------ #

    def require_user_row(self, user_fk: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM users WHERE id = ?", (user_fk,))
        if not row:
            raise AuthError("账号不存在", 404)
        return row

    def require_user(self, user_fk: int) -> dict[str, Any]:
        return user_payload(self.require_user_row(user_fk))

    def update_profile(self, user_fk: int, body: dict[str, Any]) -> dict[str, Any]:
        self.require_user_row(user_fk)
        profile = clean_profile(body)
        if not profile:
            raise AuthError("没有需要更新的个人信息字段")
        assignments = ", ".join(f"{key} = ?" for key in profile)
        self.db.execute(
            f"UPDATE users SET {assignments}, updated_at = ? WHERE id = ?",
            (*profile.values(), now(), user_fk),
        )
        return self.require_user(user_fk)

    # -- sessions ----------------------------------------------------------- #

    def open_session(self, user_fk: int) -> str:
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
        self.db.execute(
            "INSERT INTO sessions (token, user_fk, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_fk, now(), expires.isoformat(timespec="seconds")),
        )
        return token

    def close_session(self, token: str) -> None:
        if token:
            self.db.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def user_for_token(self, token: str | None) -> dict[str, Any] | None:
        """Resolve a session token, dropping it if it has expired."""
        if not token:
            return None
        row = self.db.one(
            """
            SELECT sessions.token, sessions.expires_at, users.*
            FROM sessions JOIN users ON users.id = sessions.user_fk
            WHERE sessions.token = ?
            """,
            (token,),
        )
        if not row:
            return None
        if row["expires_at"] <= now():
            self.db.execute("DELETE FROM sessions WHERE token = ?", (token,))
            return None
        return row

    def purge_expired(self) -> int:
        return self.db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now(),))

    def stats(self, user_fk: int) -> dict[str, int]:
        row = self.db.one(
            """
            SELECT
              (SELECT COUNT(*) FROM papers WHERE owner_fk = ?) AS own_papers,
              (SELECT COUNT(*) FROM papers) AS all_papers,
              (SELECT COUNT(*) FROM records JOIN papers ON papers.id = records.paper_fk
                WHERE papers.owner_fk = ?) AS own_records
            """,
            (user_fk, user_fk),
        ) or {}
        return {key: int(value or 0) for key, value in row.items()}


__all__ = [
    "Accounts",
    "AuthError",
    "PROFILE_FIELDS",
    "SESSION_COOKIE",
    "SESSION_DAYS",
    "clean_profile",
    "hash_password",
    "user_payload",
    "verify_password",
]
