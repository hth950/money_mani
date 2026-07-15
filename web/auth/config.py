"""Environment-backed authentication configuration."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return parsed


def _csv(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class AuthSettings:
    environment: str
    session_cookie_name: str
    csrf_cookie_name: str
    login_csrf_cookie_name: str
    session_secure: bool
    session_idle_seconds: int
    session_absolute_seconds: int
    login_window_seconds: int
    login_max_failures: int
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    trusted_proxy_ips: tuple[str, ...]
    internal_token: str | None

    @property
    def production(self) -> bool:
        return self.environment == "production"


def get_auth_settings() -> AuthSettings:
    """Read settings at app construction time.

    Values are intentionally not cached so test processes and management
    commands can use isolated environment settings without module reloads.
    """
    environment = os.getenv("MONEY_MANI_ENV", "development").strip().lower()
    if environment not in {"development", "test", "production"}:
        raise RuntimeError("MONEY_MANI_ENV must be development, test, or production")

    allowed_hosts = _csv(
        "MONEY_MANI_ALLOWED_HOSTS",
        ("localhost", "127.0.0.1", "testserver"),
    )
    if not allowed_hosts:
        raise RuntimeError("MONEY_MANI_ALLOWED_HOSTS must contain at least one host")
    if environment == "production" and any("*" in host for host in allowed_hosts):
        raise RuntimeError("wildcard trusted hosts are not allowed in production")

    trusted_proxy_ips = _csv(
        "MONEY_MANI_FORWARDED_ALLOW_IPS",
        ("127.0.0.1", "::1"),
    )
    if environment == "production" and "*" in trusted_proxy_ips:
        raise RuntimeError("wildcard forwarded proxy trust is not allowed in production")
    try:
        trusted_proxy_ips = tuple(
            str(ipaddress.ip_address(value)) for value in trusted_proxy_ips
        )
    except ValueError as exc:
        raise RuntimeError(
            "MONEY_MANI_FORWARDED_ALLOW_IPS must contain exact IP addresses"
        ) from exc

    allowed_origins = tuple(
        origin.rstrip("/") for origin in _csv("MONEY_MANI_ALLOWED_ORIGINS")
    )
    internal_token = os.getenv("MONEY_MANI_INTERNAL_TOKEN")
    if internal_token is not None:
        internal_token = internal_token.strip() or None
    if internal_token is not None and len(internal_token) < 32:
        raise RuntimeError("MONEY_MANI_INTERNAL_TOKEN must be at least 32 characters")

    return AuthSettings(
        environment=environment,
        session_cookie_name=os.getenv(
            "MONEY_MANI_SESSION_COOKIE", "money_mani_session"
        ).strip(),
        csrf_cookie_name=os.getenv(
            "MONEY_MANI_CSRF_COOKIE", "money_mani_csrf"
        ).strip(),
        login_csrf_cookie_name=os.getenv(
            "MONEY_MANI_LOGIN_CSRF_COOKIE", "money_mani_login_csrf"
        ).strip(),
        session_secure=_env_bool(
            "MONEY_MANI_SESSION_SECURE", environment == "production"
        ),
        session_idle_seconds=_env_int(
            "MONEY_MANI_SESSION_IDLE_SECONDS", 12 * 60 * 60, minimum=60
        ),
        session_absolute_seconds=_env_int(
            "MONEY_MANI_SESSION_ABSOLUTE_SECONDS", 7 * 24 * 60 * 60, minimum=60
        ),
        login_window_seconds=_env_int(
            "MONEY_MANI_LOGIN_WINDOW_SECONDS", 15 * 60, minimum=60
        ),
        login_max_failures=_env_int(
            "MONEY_MANI_LOGIN_MAX_FAILURES", 5, minimum=1
        ),
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        trusted_proxy_ips=trusted_proxy_ips,
        internal_token=internal_token,
    )
