"""Regression coverage for Starlette's request-first template API."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

from starlette.requests import Request

from web.routers.guide import guide_page


ROOT = Path(__file__).parent.parent
ROUTER_FILES = (
    ROOT / "web" / "routers" / "auth.py",
    ROOT / "web" / "routers" / "guide.py",
    ROOT / "web" / "routers" / "macro.py",
    ROOT / "web" / "routers" / "pages.py",
    ROOT / "web" / "routers" / "settings.py",
)


def _template_response_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "TemplateResponse"
    ]


def test_router_template_responses_use_request_first_signature():
    violations: list[str] = []
    call_count = 0

    for path in ROUTER_FILES:
        for call in _template_response_calls(path):
            call_count += 1
            request_first = (
                len(call.args) >= 2
                and isinstance(call.args[0], ast.Name)
                and call.args[0].id == "request"
                and isinstance(call.args[1], ast.Constant)
                and isinstance(call.args[1].value, str)
            )
            if not request_first:
                violations.append(f"{path.relative_to(ROOT)}:{call.lineno}")

    assert call_count == 29
    assert not violations, (
        "TemplateResponse must use (request, name, context): "
        + ", ".join(violations)
    )


def test_request_first_route_renders_with_installed_starlette():
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/guide",
            "root_path": "",
            "scheme": "http",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
    )
    request.state.user = SimpleNamespace(username="test-owner", role="owner")
    request.state.csrf_token = "test-csrf"

    response = asyncio.run(guide_page(request))

    assert response.status_code == 200
    assert "Money Mani" in response.body.decode("utf-8")
