"""Default-deny HTTP authentication, role authorization, and CSRF checks."""

from __future__ import annotations

import hmac
import ipaddress
import re
from urllib.parse import quote, urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from web.auth.config import AuthSettings
from web.auth.service import AuthContext, authenticate_session, verify_csrf


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PUBLIC_PATHS = {"/login", "/healthz"}
INTERNAL_POST_PATHS = {"/api/monitor/start", "/api/monitor/stop"}

VIEWER_PAGE_PATTERNS = (
    re.compile(r"^/signals/?$"),
    re.compile(r"^/scoring/?$"),
    re.compile(r"^/strategies/?$"),
    re.compile(r"^/strategies/\d+/?$"),
    re.compile(r"^/backtest/?$"),
    re.compile(r"^/backtest/\d+/?$"),
    re.compile(r"^/performance/?$"),
    re.compile(r"^/intel/?$"),
    re.compile(r"^/guide/?$"),
    re.compile(r"^/macro/?$"),
    re.compile(r"^/paper-trading/?$"),
)

VIEWER_API_PREFIXES = (
    "/api/scoring",
    "/api/strategies",
    "/api/performance",
    "/api/intel",
    "/api/macro",
)


def _is_api_request(request: Request) -> bool:
    return (
        request.url.path.startswith("/api/")
        or bool(request.headers.get("HX-Request"))
        or "application/json" in request.headers.get("accept", "").lower()
    )


def viewer_path_allowed(path: str) -> bool:
    if any(pattern.fullmatch(path) for pattern in VIEWER_PAGE_PATTERNS):
        return True
    if any(path == prefix or path.startswith(prefix + "/") for prefix in VIEWER_API_PREFIXES):
        return True
    if path in {
        "/api/signals",
        "/api/signals/actions",
        "/api/signals/exit-scores",
    }:
        return True
    if path == "/api/backtest/evidence":
        return True
    if path == "/api/backtest/results" or path.startswith("/api/backtest/results/"):
        return True
    if path in {"/api/paper-trading/overview", "/api/paper-trading/trades"}:
        return True
    if re.fullmatch(r"/api/paper-trading/positions/\d+/marks", path):
        return True
    if path == "/api/auth/me":
        return True
    return False


def _private_or_loopback_client(request: Request) -> bool:
    if not request.client:
        return False
    try:
        address = ipaddress.ip_address(request.client.host.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_private or address.is_loopback


def _trusted_proxy_client(request: Request, settings: AuthSettings) -> bool:
    if not request.client:
        return False
    try:
        address = str(
            ipaddress.ip_address(request.client.host.split("%", 1)[0])
        )
    except ValueError:
        return False
    return address in settings.trusted_proxy_ips


def client_ip(request: Request, settings: AuthSettings) -> str | None:
    """Return the original client only when the direct proxy is trusted locally.

    Funnel reaches Uvicorn through the host/Docker private network. A public
    direct peer cannot make an arbitrary X-Forwarded-For value authoritative.
    """
    direct = request.client.host if request.client else None
    if not direct:
        return None
    if not _trusted_proxy_client(request, settings):
        return direct
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    if not forwarded:
        return direct
    try:
        return str(ipaddress.ip_address(forwarded.split("%", 1)[0]))
    except ValueError:
        return direct


def _internal_request_allowed(request: Request, settings: AuthSettings) -> bool:
    if request.method != "POST" or request.url.path not in INTERNAL_POST_PATHS:
        return False
    if not settings.internal_token or not _private_or_loopback_client(request):
        return False
    submitted = request.headers.get("X-Money-Mani-Internal-Token", "")
    return hmac.compare_digest(submitted, settings.internal_token)


def _normalize_origin(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return None
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _request_origins(request: Request, settings: AuthSettings) -> set[str]:
    origins = {
        normalized
        for item in settings.allowed_origins
        if (normalized := _normalize_origin(item))
    }
    host = request.headers.get("host", "").lower()
    if host:
        scheme = request.url.scheme
        if _trusted_proxy_client(request, settings):
            forwarded = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
            if forwarded in {"http", "https"}:
                scheme = forwarded
        origins.add(f"{scheme}://{host}")
    return origins


def request_origin_allowed(request: Request, settings: AuthSettings) -> bool:
    submitted = _normalize_origin(request.headers.get("origin", ""))
    return bool(submitted and submitted in _request_origins(request, settings))


def _unauthenticated_response(request: Request) -> Response:
    if _is_api_request(request):
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    next_path = request.url.path
    if request.url.query:
        next_path += "?" + request.url.query
    return RedirectResponse(
        url=f"/login?next={quote(next_path, safe='/?=&%')}",
        status_code=303,
    )


def _forbidden_response(request: Request, detail: str = "owner access required") -> Response:
    if _is_api_request(request):
        return JSONResponse({"detail": detail}, status_code=403)
    return HTMLResponse(
        "<h1>403</h1><p>이 페이지는 관리자만 접근할 수 있습니다.</p>"
        '<p><a href="/signals">신호 화면으로 돌아가기</a></p>',
        status_code=403,
    )


class AuthMiddleware(BaseHTTPMiddleware):
    """Authenticate every non-public request and enforce role/CSRF rules."""

    def __init__(self, app, *, settings: AuthSettings):
        super().__init__(app)
        self.settings = settings

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request.state.user = None
        request.state.auth_context = None
        request.state.internal_request = False
        request.state.csrf_token = None

        path = request.url.path
        context: AuthContext | None = None
        if not path.startswith("/static/"):
            context = authenticate_session(
                request.cookies.get(self.settings.session_cookie_name),
                settings=self.settings,
            )
            if context:
                request.state.user = context.user
                request.state.auth_context = context
                request.state.csrf_token = request.cookies.get(
                    self.settings.csrf_cookie_name
                )

        is_public = path in PUBLIC_PATHS or path.startswith("/static/")
        if not self.settings.production and path in {"/docs", "/openapi.json", "/redoc"}:
            is_public = True

        if _internal_request_allowed(request, self.settings):
            request.state.internal_request = True
        elif not is_public and path != "/api/auth/me":
            if not context:
                return self._secure(request, _unauthenticated_response(request))
            if context.user.role == "viewer":
                if request.method not in SAFE_METHODS or not viewer_path_allowed(path):
                    return self._secure(request, _forbidden_response(request))

            if request.method not in SAFE_METHODS:
                if not request_origin_allowed(request, self.settings):
                    return self._secure(
                        request,
                        JSONResponse({"detail": "invalid request origin"}, status_code=403),
                    )
                if not verify_csrf(
                    context,
                    request.cookies.get(self.settings.csrf_cookie_name),
                    request.headers.get("X-CSRF-Token"),
                ):
                    return self._secure(
                        request,
                        JSONResponse({"detail": "invalid CSRF token"}, status_code=403),
                    )

        response = await call_next(request)
        return self._secure(request, response)

    @staticmethod
    def _secure(request: Request, response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; "
            "form-action 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net; "
            "connect-src 'self'",
        )
        if request.url.scheme == "https":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        if request.url.path in {"/login", "/logout", "/api/auth/me"}:
            response.headers.setdefault("Cache-Control", "no-store")
        elif getattr(request.state, "user", None):
            response.headers.setdefault("Cache-Control", "private, no-store")
        return response
