"""Authentication, role policy, CSRF, session, and login-throttle tests."""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from web.auth import service
from web.auth.config import get_auth_settings
from web.auth.middleware import AuthMiddleware
from web.db import connection
from web.db.migrate import run_schema_migrations
from web.routers.auth import router as auth_router


PASSWORD = "correct horse battery staple"
INTERNAL_TOKEN = "internal-test-token-that-is-longer-than-32-bytes"


def _csrf_from_html(response) -> str:
    match = re.search(r'name="login_csrf" value="([^"]+)"', response.text)
    assert match, response.text
    return match.group(1)


def _login(client: TestClient, username: str, password: str = PASSWORD, *, next_path="/"):
    page = client.get("/login")
    assert page.status_code == 200
    token = _csrf_from_html(page)
    return client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "next": next_path,
            "login_csrf": token,
        },
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )


@pytest.fixture
def auth_app(tmp_path, monkeypatch):
    monkeypatch.setattr(connection, "DB_PATH", tmp_path / "auth.db")
    monkeypatch.setenv("MONEY_MANI_ENV", "test")
    monkeypatch.setenv("MONEY_MANI_SESSION_SECURE", "false")
    monkeypatch.setenv("MONEY_MANI_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("MONEY_MANI_ALLOWED_ORIGINS", "http://testserver")
    monkeypatch.setenv("MONEY_MANI_INTERNAL_TOKEN", INTERNAL_TOKEN)
    connection.init_db()
    run_schema_migrations()

    app = FastAPI()
    settings = get_auth_settings()
    app.add_middleware(AuthMiddleware, settings=settings)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    app.include_router(auth_router)
    templates = Jinja2Templates(
        directory=str(Path(__file__).parent.parent / "web" / "templates")
    )

    @app.get("/healthz")
    async def health():
        return Response(status_code=204)

    @app.get("/signals")
    async def signals_page(request: Request):
        return templates.TemplateResponse(
            "signals/index.html", {"request": request}
        )

    @app.get("/paper-trading")
    async def paper_page(request: Request):
        return templates.TemplateResponse(
            "paper_trading/index.html", {"request": request}
        )

    @app.get("/portfolio")
    async def portfolio_page():
        return {"page": "portfolio"}

    @app.get("/api/signals")
    async def signals_api():
        return {"signals": []}

    @app.get("/api/signals/summary/{ticker}")
    async def signals_summary(ticker: str):
        return {"ticker": ticker, "summary": "generated"}

    @app.get("/api/scoring/today")
    async def scoring_api():
        return {"scores": []}

    @app.get("/api/paper-trading/overview")
    async def paper_overview():
        return {"positions": []}

    @app.get("/api/paper-trading/jobs/abc")
    async def paper_job():
        return {"status": "done"}

    @app.get("/api/portfolio/live")
    async def portfolio_api():
        return {"account": "secret"}

    @app.post("/api/strategies")
    async def mutate_strategy():
        return {"created": True}

    @app.post("/api/monitor/start")
    async def monitor_start(request: Request):
        return {"internal": request.state.internal_request}

    return app


@pytest.fixture
def client(auth_app):
    with TestClient(
        auth_app,
        follow_redirects=False,
        client=("127.0.0.1", 50000),
    ) as test_client:
        yield test_client


def test_schema_is_idempotent_and_password_is_argon2id(auth_app):
    connection.init_db()
    run_schema_migrations()
    user = service.create_user("Owner.One", PASSWORD, "owner")
    assert user.username == "owner.one"
    with connection.get_db() as db:
        password_hash = db.execute(
            "SELECT password_hash FROM app_users WHERE id=?", (user.id,)
        ).fetchone()["password_hash"]
        tables = {
            row["name"]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert password_hash.startswith("$argon2id$")
    assert {"app_users", "app_sessions", "auth_audit_events"} <= tables


def test_unauthenticated_html_redirects_but_api_is_401_and_health_is_public(client):
    response = client.get("/signals")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=/signals")

    response = client.get("/api/signals")
    assert response.status_code == 401
    assert response.json() == {"detail": "authentication required"}
    assert response.headers["x-frame-options"] == "DENY"

    assert client.get("/api/auth/me").status_code == 401
    health = client.get("/healthz")
    assert health.status_code == 204
    assert health.content == b""


def test_login_requires_same_origin_double_submit_token(client):
    service.create_user("owner", PASSWORD, "owner")
    response = client.post(
        "/login",
        data={
            "username": "owner",
            "password": PASSWORD,
            "next": "/",
            "login_csrf": "forged",
        },
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    with connection.get_db() as db:
        failures = db.execute(
            "SELECT COUNT(*) AS count FROM auth_audit_events WHERE event_type='login_failed'"
        ).fetchone()["count"]
    assert failures == 0


def test_owner_login_cookie_me_csrf_origin_and_logout(client):
    service.create_user("owner", PASSWORD, "owner")
    response = _login(client, "owner")
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    csrf_token = client.cookies.get("money_mani_csrf")
    assert csrf_token

    assert client.get("/api/auth/me").json()["role"] == "owner"
    assert client.post("/api/strategies").json()["detail"] == "invalid request origin"
    response = client.post(
        "/api/strategies", headers={"Origin": "http://testserver"}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "invalid CSRF token"

    response = client.post(
        "/api/strategies",
        headers={
            "Origin": "http://testserver",
            "X-CSRF-Token": csrf_token,
        },
    )
    assert response.status_code == 200
    assert response.json() == {"created": True}
    assert response.headers["cache-control"] == "private, no-store"

    response = client.post(
        "/logout",
        headers={
            "Origin": "http://testserver",
            "X-CSRF-Token": csrf_token,
        },
    )
    assert response.status_code == 303
    assert client.get("/api/auth/me").status_code == 401


def test_viewer_has_explicit_read_only_allowlist(client):
    service.create_user("guest", PASSWORD, "viewer")
    response = _login(client, "guest", next_path="/portfolio")
    assert response.status_code == 303
    assert response.headers["location"] == "/signals"

    assert client.get("/signals").status_code == 200
    assert client.get("/api/signals").status_code == 200
    assert client.get("/api/signals/summary/AAPL").status_code == 403
    assert client.get("/api/scoring/today").status_code == 200
    assert client.get("/api/paper-trading/overview").status_code == 200

    assert client.get("/portfolio").status_code == 403
    assert client.get("/api/portfolio/live").status_code == 403
    assert client.get("/api/paper-trading/jobs/abc").status_code == 403
    assert client.post("/api/strategies").status_code == 403


def test_viewer_pages_hide_mutation_controls_and_base_injects_csrf(client):
    service.create_user("guest", PASSWORD, "viewer")
    assert _login(client, "guest").status_code == 303

    signals = client.get("/signals")
    assert "const SIGNALS_CAN_PAPER_TRADE = false" in signals.text
    assert "const SIGNALS_CAN_AI_SUMMARY = false" in signals.text
    assert "headers.set('X-CSRF-Token', token)" in signals.text
    assert "htmx:configRequest" in signals.text

    paper = client.get("/paper-trading")
    assert paper.status_code == 200
    assert "const PAPER_CAN_TRADE = false" in paper.text
    assert '<button id="paper-refresh-button"' not in paper.text
    assert 'href="/portfolio"' not in paper.text
    assert "현재 계정은 조회 전용" in paper.text


def test_internal_token_only_bypasses_exact_monitor_posts_on_private_peer(client):
    headers = {"X-Money-Mani-Internal-Token": INTERNAL_TOKEN}
    response = client.post("/api/monitor/start", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"internal": True}

    response = client.post("/api/strategies", headers=headers)
    assert response.status_code == 401


def test_login_lock_uses_forwarded_client_ip_from_private_proxy(client):
    service.create_user("owner", PASSWORD, "owner")
    token = _csrf_from_html(client.get("/login"))
    response = None
    for _ in range(5):
        response = client.post(
            "/login",
            data={
                "username": "owner",
                "password": "this password is incorrect",
                "next": "/",
                "login_csrf": token,
            },
            headers={
                "Origin": "http://testserver",
                "X-Forwarded-For": "203.0.113.41, 172.18.0.1",
            },
        )
        assert response.status_code == 401
        token = _csrf_from_html(response)

    response = client.post(
        "/login",
        data={
            "username": "owner",
            "password": PASSWORD,
            "next": "/",
            "login_csrf": token,
        },
        headers={
            "Origin": "http://testserver",
            "X-Forwarded-For": "203.0.113.41, 172.18.0.1",
        },
    )
    assert response.status_code == 429
    with connection.get_db() as db:
        ips = {
            row["ip_address"]
            for row in db.execute(
                "SELECT ip_address FROM auth_audit_events WHERE event_type='login_failed'"
            ).fetchall()
        }
    assert ips == {"203.0.113.41"}


def test_login_lock_applies_by_username_across_changed_ips_and_coalesces_audit(client):
    service.create_user("owner", PASSWORD, "owner")
    token = _csrf_from_html(client.get("/login"))
    for index in range(5):
        response = client.post(
            "/login",
            data={
                "username": "owner",
                "password": "this password is incorrect",
                "next": "/",
                "login_csrf": token,
            },
            headers={
                "Origin": "http://testserver",
                "X-Forwarded-For": f"203.0.113.{index + 1}",
            },
        )
        assert response.status_code == 401
        token = _csrf_from_html(response)

    for index in range(3):
        response = client.post(
            "/login",
            data={
                "username": "owner",
                "password": PASSWORD,
                "next": "/",
                "login_csrf": token,
            },
            headers={
                "Origin": "http://testserver",
                "X-Forwarded-For": f"198.51.100.{index + 50}",
            },
        )
        assert response.status_code == 429
        token = _csrf_from_html(response)

    with connection.get_db() as db:
        locked = db.execute(
            """SELECT COUNT(*) AS count FROM auth_audit_events
               WHERE event_type='login_locked' AND username='owner'"""
        ).fetchone()["count"]
    assert locked == 1


def test_idle_expiry_and_password_reset_revoke_sessions(auth_app, monkeypatch):
    settings = get_auth_settings()
    service.create_user("owner", PASSWORD, "owner")
    first = service.login(
        "owner", PASSWORD, ip_address="127.0.0.1", user_agent="pytest", settings=settings
    )
    assert service.authenticate_session(first.session_token, settings=settings)

    future = service.utcnow() + timedelta(seconds=settings.session_idle_seconds + 1)
    real_utcnow = service.utcnow
    monkeypatch.setattr(service, "utcnow", lambda: future)
    assert service.authenticate_session(first.session_token, settings=settings) is None

    monkeypatch.setattr(service, "utcnow", real_utcnow)
    second = service.login(
        "owner", PASSWORD, ip_address="127.0.0.1", user_agent="pytest", settings=settings
    )
    service.reset_password("owner", "a completely different strong password")
    assert service.authenticate_session(second.session_token, settings=settings) is None


def test_absolute_expiry_and_disable_revoke_sessions(auth_app):
    settings = get_auth_settings()
    service.create_user("owner", PASSWORD, "owner")
    expired = service.login(
        "owner", PASSWORD, ip_address="127.0.0.1", user_agent="pytest", settings=settings
    )
    with connection.get_db() as db:
        db.execute(
            "UPDATE app_sessions SET expires_at=? WHERE token_hash=?",
            (
                (service.utcnow() - timedelta(seconds=1)).isoformat(),
                service._hash_token(expired.session_token),
            ),
        )
    assert service.authenticate_session(expired.session_token, settings=settings) is None

    active = service.login(
        "owner", PASSWORD, ip_address="127.0.0.1", user_agent="pytest", settings=settings
    )
    service.set_user_active("owner", False)
    assert service.authenticate_session(active.session_token, settings=settings) is None


def test_production_login_cookies_have_secure_attributes(auth_app, monkeypatch):
    service.create_user("owner", PASSWORD, "owner")
    monkeypatch.setenv("MONEY_MANI_ENV", "production")
    monkeypatch.setenv("MONEY_MANI_SESSION_SECURE", "true")
    monkeypatch.setenv("MONEY_MANI_ALLOWED_ORIGINS", "https://testserver")
    with TestClient(
        auth_app,
        base_url="https://testserver",
        follow_redirects=False,
        client=("127.0.0.1", 50000),
    ) as secure_client:
        page = secure_client.get("/login")
        response = secure_client.post(
            "/login",
            data={
                "username": "owner",
                "password": PASSWORD,
                "next": "/",
                "login_csrf": _csrf_from_html(page),
            },
            headers={"Origin": "https://testserver"},
        )
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(item for item in cookies if item.startswith("money_mani_session="))
    csrf_cookie = next(item for item in cookies if item.startswith("money_mani_csrf="))
    assert "Secure" in session_cookie and "HttpOnly" in session_cookie
    assert "SameSite=lax" in session_cookie
    assert "Secure" in csrf_cookie and "HttpOnly" not in csrf_cookie
    assert "SameSite=lax" in csrf_cookie


def test_secure_cookie_default_depends_on_environment(monkeypatch):
    monkeypatch.delenv("MONEY_MANI_SESSION_SECURE", raising=False)
    monkeypatch.setenv("MONEY_MANI_ENV", "development")
    assert get_auth_settings().session_secure is False
    monkeypatch.setenv("MONEY_MANI_ENV", "production")
    assert get_auth_settings().session_secure is True
    monkeypatch.setenv("MONEY_MANI_SESSION_SECURE", "false")
    assert get_auth_settings().session_secure is False


def test_production_rejects_wildcard_hosts_and_proxy_trust(monkeypatch):
    monkeypatch.setenv("MONEY_MANI_ENV", "production")
    monkeypatch.setenv("MONEY_MANI_ALLOWED_HOSTS", "*.ts.net")
    with pytest.raises(RuntimeError, match="wildcard trusted hosts"):
        get_auth_settings()

    monkeypatch.setenv("MONEY_MANI_ALLOWED_HOSTS", "money.example.ts.net")
    monkeypatch.setenv("MONEY_MANI_FORWARDED_ALLOW_IPS", "*")
    with pytest.raises(RuntimeError, match="wildcard forwarded proxy trust"):
        get_auth_settings()
