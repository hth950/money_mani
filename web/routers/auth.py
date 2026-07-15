"""Login, logout, and current-user routes."""

from __future__ import annotations

from pathlib import Path
import hmac
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from web.auth.config import get_auth_settings
from web.auth.middleware import client_ip, request_origin_allowed
from web.auth.service import InvalidCredentials, LoginLocked, login, logout


router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _safe_next(value: str | None, *, viewer: bool = False) -> str:
    fallback = "/signals" if viewer else "/"
    if not value:
        return fallback
    try:
        parsed = urlsplit(value)
    except ValueError:
        return fallback
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return fallback
    target = parsed.path + (("?" + parsed.query) if parsed.query else "")
    if viewer:
        from web.auth.middleware import viewer_path_allowed

        if not viewer_path_allowed(parsed.path):
            return fallback
    return target


def _login_page_response(
    request: Request,
    *,
    next_path: str,
    error: str | None,
    status_code: int = 200,
):
    settings = get_auth_settings()
    csrf_token = secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        "auth/login.html",
        {
            "request": request,
            "next": next_path,
            "error": error,
            "login_csrf": csrf_token,
        },
        status_code=status_code,
    )
    response.set_cookie(
        settings.login_csrf_cookie_name,
        csrf_token,
        max_age=10 * 60,
        path="/login",
        secure=settings.session_secure,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str | None = None):
    if request.state.user:
        return RedirectResponse(
            _safe_next(next, viewer=request.state.user.role == "viewer"), status_code=303
        )
    return _login_page_response(
        request,
        next_path=_safe_next(next),
        error=None,
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    login_csrf: str = Form(...),
):
    settings = get_auth_settings()
    submitted_cookie = request.cookies.get(settings.login_csrf_cookie_name, "")
    if (
        not submitted_cookie
        or not hmac.compare_digest(submitted_cookie, login_csrf)
        or not request_origin_allowed(request, settings)
    ):
        return _login_page_response(
            request,
            next_path=_safe_next(next),
            error="로그인 요청을 확인할 수 없습니다. 다시 시도하세요.",
            status_code=403,
        )
    source_ip = client_ip(request, settings)
    user_agent = request.headers.get("user-agent")
    try:
        session = login(
            username,
            password,
            ip_address=source_ip,
            user_agent=user_agent,
            settings=settings,
        )
    except LoginLocked:
        response = _login_page_response(
            request,
            next_path=_safe_next(next),
            error="로그인 시도가 너무 많습니다. 15분 후 다시 시도하세요.",
            status_code=429,
        )
        response.headers["Retry-After"] = str(settings.login_window_seconds)
        return response
    except InvalidCredentials:
        return _login_page_response(
            request,
            next_path=_safe_next(next),
            error="아이디 또는 비밀번호가 올바르지 않습니다.",
            status_code=401,
        )

    response = RedirectResponse(
        _safe_next(next, viewer=session.user.role == "viewer"), status_code=303
    )
    max_age = settings.session_absolute_seconds
    response.set_cookie(
        settings.session_cookie_name,
        session.session_token,
        max_age=max_age,
        expires=session.expires_at,
        path="/",
        secure=settings.session_secure,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        settings.csrf_cookie_name,
        session.csrf_token,
        max_age=max_age,
        expires=session.expires_at,
        path="/",
        secure=settings.session_secure,
        httponly=False,
        samesite="lax",
    )
    response.delete_cookie(settings.login_csrf_cookie_name, path="/login")
    return response


@router.post("/logout")
async def logout_submit(request: Request):
    settings = get_auth_settings()
    logout(
        request.cookies.get(settings.session_cookie_name),
        ip_address=client_ip(request, settings),
        user_agent=request.headers.get("user-agent"),
    )
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")
    return response


@router.get("/api/auth/me")
async def auth_me(request: Request):
    user = request.state.user
    if not user:
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    return {"id": user.id, "username": user.username, "role": user.role}
