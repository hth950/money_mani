"""SQLite-backed users, sessions, login throttling, and audit events."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from web.auth.config import AuthSettings
from web.db import connection


USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256
SESSION_TOUCH_INTERVAL_SECONDS = 5 * 60

_PASSWORD_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)
# A valid fallback hash makes unknown-user and wrong-password work broadly
# similar without storing or logging the submitted password.
_DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash(secrets.token_urlsafe(32))


class AuthError(Exception):
    """Base class for expected authentication errors."""


class InvalidCredentials(AuthError):
    pass


class LoginLocked(AuthError):
    pass


class InvalidAccountInput(AuthError):
    pass


@dataclass(frozen=True)
class AuthenticatedUser:
    id: int
    username: str
    role: str


@dataclass(frozen=True)
class AuthContext:
    user: AuthenticatedUser
    session_id: int
    token_hash: str
    csrf_token_hash: str


@dataclass(frozen=True)
class NewSession:
    user: AuthenticatedUser
    session_token: str
    csrf_token: str
    expires_at: datetime


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_username(username: str) -> str:
    normalized = username.strip().lower()
    if not USERNAME_RE.fullmatch(normalized):
        raise InvalidAccountInput(
            "username must be 3-64 characters using letters, numbers, '.', '_' or '-'"
        )
    return normalized


def validate_password(password: str) -> None:
    if not isinstance(password, str):
        raise InvalidAccountInput("password is required")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise InvalidAccountInput(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise InvalidAccountInput(
            f"password must be at most {MAX_PASSWORD_LENGTH} characters"
        )


def _audit(
    db: sqlite3.Connection,
    event_type: str,
    *,
    user_id: int | None = None,
    username: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    detail: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    db.execute(
        """INSERT INTO auth_audit_events
           (user_id, username, event_type, ip_address, user_agent, detail_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            username,
            event_type,
            ip_address,
            (user_agent or "")[:512] or None,
            json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail else None,
            _timestamp(now or utcnow()),
        ),
    )


def _verify_password(password_hash: str, submitted_password: str) -> bool:
    try:
        return _PASSWORD_HASHER.verify(password_hash, submitted_password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def create_user(username: str, password: str, role: str) -> AuthenticatedUser:
    normalized = normalize_username(username)
    validate_password(password)
    if role not in {"owner", "viewer"}:
        raise InvalidAccountInput("role must be owner or viewer")
    password_hash = _PASSWORD_HASHER.hash(password)
    now = utcnow()
    try:
        with connection.get_db() as db:
            cursor = db.execute(
                """INSERT INTO app_users
                   (username, password_hash, role, is_active, created_at, updated_at,
                    password_changed_at)
                   VALUES (?, ?, ?, 1, ?, ?, ?)""",
                (
                    normalized,
                    password_hash,
                    role,
                    _timestamp(now),
                    _timestamp(now),
                    _timestamp(now),
                ),
            )
            user = AuthenticatedUser(cursor.lastrowid, normalized, role)
            _audit(
                db,
                "user_created",
                user_id=user.id,
                username=normalized,
                detail={"role": role},
                now=now,
            )
            return user
    except sqlite3.IntegrityError as exc:
        raise InvalidAccountInput(f"user already exists: {normalized}") from exc


def reset_password(username: str, password: str) -> None:
    normalized = normalize_username(username)
    validate_password(password)
    password_hash = _PASSWORD_HASHER.hash(password)
    now = utcnow()
    with connection.get_db() as db:
        row = db.execute(
            "SELECT id FROM app_users WHERE username = ? COLLATE NOCASE",
            (normalized,),
        ).fetchone()
        if not row:
            raise InvalidAccountInput(f"user does not exist: {normalized}")
        db.execute(
            """UPDATE app_users
               SET password_hash=?, password_changed_at=?, updated_at=?
               WHERE id=?""",
            (password_hash, _timestamp(now), _timestamp(now), row["id"]),
        )
        db.execute(
            "UPDATE app_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (_timestamp(now), row["id"]),
        )
        _audit(
            db,
            "password_reset",
            user_id=row["id"],
            username=normalized,
            now=now,
        )


def set_user_active(username: str, active: bool) -> None:
    normalized = normalize_username(username)
    now = utcnow()
    with connection.get_db() as db:
        row = db.execute(
            "SELECT id FROM app_users WHERE username = ? COLLATE NOCASE",
            (normalized,),
        ).fetchone()
        if not row:
            raise InvalidAccountInput(f"user does not exist: {normalized}")
        db.execute(
            "UPDATE app_users SET is_active=?, updated_at=? WHERE id=?",
            (1 if active else 0, _timestamp(now), row["id"]),
        )
        if not active:
            db.execute(
                "UPDATE app_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                (_timestamp(now), row["id"]),
            )
        _audit(
            db,
            "user_enabled" if active else "user_disabled",
            user_id=row["id"],
            username=normalized,
            now=now,
        )


def list_users() -> list[dict[str, Any]]:
    with connection.get_db() as db:
        rows = db.execute(
            """SELECT id, username, role, is_active, created_at, updated_at,
                      password_changed_at, last_login_at
               FROM app_users ORDER BY username COLLATE NOCASE"""
        ).fetchall()
    return [dict(row) for row in rows]


def _is_login_locked(
    db: sqlite3.Connection,
    username: str,
    ip_address: str | None,
    settings: AuthSettings,
    now: datetime,
) -> bool:
    since = _timestamp(now - timedelta(seconds=settings.login_window_seconds))
    username_row = db.execute(
        """SELECT COUNT(*) AS failures
           FROM auth_audit_events
           WHERE event_type='login_failed'
             AND username=? COLLATE NOCASE
             AND created_at>=?""",
        (username, since),
    ).fetchone()
    username_locked = int(username_row["failures"]) >= settings.login_max_failures
    if not ip_address:
        return username_locked
    ip_row = db.execute(
        """SELECT COUNT(*) AS failures
           FROM auth_audit_events
           WHERE event_type='login_failed'
             AND ip_address=?
             AND created_at>=?""",
        (ip_address, since),
    ).fetchone()
    return username_locked or int(ip_row["failures"]) >= settings.login_max_failures


def _audit_login_locked_once(
    db: sqlite3.Connection,
    username: str,
    *,
    ip_address: str | None,
    user_agent: str | None,
    settings: AuthSettings,
    now: datetime,
) -> None:
    """Coalesce repeated locked attempts to one audit row per lock window."""
    since = _timestamp(now - timedelta(seconds=settings.login_window_seconds))
    existing = db.execute(
        """SELECT 1 FROM auth_audit_events
           WHERE event_type='login_locked'
             AND (
                 username=? COLLATE NOCASE
                 OR (? IS NOT NULL AND ip_address=?)
             )
             AND created_at>=?
           LIMIT 1""",
        (username, ip_address, ip_address, since),
    ).fetchone()
    if existing:
        return
    _audit(
        db,
        "login_locked",
        username=username,
        ip_address=ip_address,
        user_agent=user_agent,
        now=now,
    )


def login(
    username: str,
    password: str,
    *,
    ip_address: str | None,
    user_agent: str | None,
    settings: AuthSettings,
) -> NewSession:
    try:
        normalized = normalize_username(username)
    except InvalidAccountInput:
        normalized = username.strip().lower()[:64]
    now = utcnow()
    with connection.get_db() as db:
        if _is_login_locked(db, normalized, ip_address, settings, now):
            _audit_login_locked_once(
                db,
                normalized,
                ip_address=ip_address,
                user_agent=user_agent,
                settings=settings,
                now=now,
            )
            # The surrounding DB context rolls back on exceptions. Persist the
            # security event before returning the expected rate-limit error.
            db.commit()
            raise LoginLocked("too many login attempts")

        row = db.execute(
            """SELECT id, username, password_hash, role, is_active
               FROM app_users WHERE username=? COLLATE NOCASE""",
            (normalized,),
        ).fetchone()
        password_hash = row["password_hash"] if row else _DUMMY_PASSWORD_HASH
        valid = _verify_password(password_hash, password)
        if not row or not valid or not bool(row["is_active"]):
            _audit(
                db,
                "login_failed",
                user_id=row["id"] if row else None,
                username=normalized,
                ip_address=ip_address,
                user_agent=user_agent,
                now=now,
            )
            db.commit()
            raise InvalidCredentials("invalid username or password")

        if _PASSWORD_HASHER.check_needs_rehash(row["password_hash"]):
            db.execute(
                "UPDATE app_users SET password_hash=?, updated_at=? WHERE id=?",
                (_PASSWORD_HASHER.hash(password), _timestamp(now), row["id"]),
            )

        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = now + timedelta(seconds=settings.session_absolute_seconds)
        db.execute(
            """INSERT INTO app_sessions
               (user_id, token_hash, csrf_token_hash, created_at, last_seen_at,
                expires_at, client_ip, user_agent)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["id"],
                _hash_token(session_token),
                _hash_token(csrf_token),
                _timestamp(now),
                _timestamp(now),
                _timestamp(expires_at),
                ip_address,
                (user_agent or "")[:512] or None,
            ),
        )
        db.execute(
            "UPDATE app_users SET last_login_at=?, updated_at=? WHERE id=?",
            (_timestamp(now), _timestamp(now), row["id"]),
        )
        _audit(
            db,
            "login_succeeded",
            user_id=row["id"],
            username=row["username"],
            ip_address=ip_address,
            user_agent=user_agent,
            now=now,
        )
        return NewSession(
            user=AuthenticatedUser(row["id"], row["username"], row["role"]),
            session_token=session_token,
            csrf_token=csrf_token,
            expires_at=expires_at,
        )


def authenticate_session(
    session_token: str | None,
    *,
    settings: AuthSettings,
) -> AuthContext | None:
    if not session_token:
        return None
    token_hash = _hash_token(session_token)
    now = utcnow()
    with connection.get_db() as db:
        row = db.execute(
            """SELECT s.id AS session_id, s.csrf_token_hash, s.created_at,
                      s.last_seen_at, s.expires_at,
                      u.id AS user_id, u.username, u.role, u.is_active
               FROM app_sessions s
               JOIN app_users u ON u.id=s.user_id
               WHERE s.token_hash=? AND s.revoked_at IS NULL""",
            (token_hash,),
        ).fetchone()
        if not row or not bool(row["is_active"]):
            return None

        idle_at = _parse_timestamp(row["last_seen_at"]) + timedelta(
            seconds=settings.session_idle_seconds
        )
        absolute_at = _parse_timestamp(row["expires_at"])
        if now >= idle_at or now >= absolute_at:
            db.execute(
                "UPDATE app_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                (_timestamp(now), row["session_id"]),
            )
            _audit(
                db,
                "session_expired",
                user_id=row["user_id"],
                username=row["username"],
                detail={"reason": "idle" if now >= idle_at else "absolute"},
                now=now,
            )
            return None

        last_seen = _parse_timestamp(row["last_seen_at"])
        if (now - last_seen).total_seconds() >= SESSION_TOUCH_INTERVAL_SECONDS:
            db.execute(
                "UPDATE app_sessions SET last_seen_at=? WHERE id=?",
                (_timestamp(now), row["session_id"]),
            )

        return AuthContext(
            user=AuthenticatedUser(row["user_id"], row["username"], row["role"]),
            session_id=row["session_id"],
            token_hash=token_hash,
            csrf_token_hash=row["csrf_token_hash"],
        )


def verify_csrf(context: AuthContext, cookie_token: str | None, header_token: str | None) -> bool:
    if not cookie_token or not header_token:
        return False
    if not hmac.compare_digest(cookie_token, header_token):
        return False
    return hmac.compare_digest(_hash_token(cookie_token), context.csrf_token_hash)


def logout(
    session_token: str | None,
    *,
    ip_address: str | None,
    user_agent: str | None,
) -> None:
    if not session_token:
        return
    token_hash = _hash_token(session_token)
    now = utcnow()
    with connection.get_db() as db:
        row = db.execute(
            """SELECT s.id, u.id AS user_id, u.username
               FROM app_sessions s JOIN app_users u ON u.id=s.user_id
               WHERE s.token_hash=? AND s.revoked_at IS NULL""",
            (token_hash,),
        ).fetchone()
        if not row:
            return
        db.execute(
            "UPDATE app_sessions SET revoked_at=? WHERE id=?",
            (_timestamp(now), row["id"]),
        )
        _audit(
            db,
            "logout",
            user_id=row["user_id"],
            username=row["username"],
            ip_address=ip_address,
            user_agent=user_agent,
            now=now,
        )
