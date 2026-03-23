"""LLM provider settings: toggle OpenRouter / OpenAI OAuth with web UI."""

import json
import logging
import time
import threading
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from utils.config_loader import load_config

logger = logging.getLogger("money_mani.web.settings")
router = APIRouter(tags=["settings"])

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

SETTINGS_PATH = Path(__file__).parent.parent.parent / "config" / "settings.yaml"

# In-memory state for active device auth flow
_active_flow = {
    "active": False,
    "verify_url": None,
    "user_code": None,
    "status": "idle",  # idle | waiting | success | error
    "error": None,
    "thread": None,
}


def _get_current_provider() -> str:
    cfg = load_config()
    return cfg.get("llm", {}).get("provider", "openrouter")


def _get_token_path() -> Path:
    cfg = load_config()
    oauth_cfg = cfg.get("llm", {}).get("openai_oauth", {})
    return Path(
        oauth_cfg.get("token_path", "~/.money_mani/openai_oauth_token.json")
    ).expanduser()


def _get_token_status() -> dict:
    """Check OAuth token status."""
    token_path = _get_token_path()
    if not token_path.exists():
        return {"exists": False, "valid": False, "expires_at": None}
    try:
        data = json.loads(token_path.read_text())
        expires_at = data.get("expires_at", 0)
        has_refresh = bool(data.get("refresh_token"))
        return {
            "exists": True,
            "valid": time.time() < expires_at or has_refresh,
            "expired": time.time() >= expires_at,
            "has_refresh": has_refresh,
            "expires_at": expires_at,
        }
    except Exception:
        return {"exists": True, "valid": False, "expires_at": None}


def _switch_provider(new_provider: str):
    """Update provider in settings.yaml."""
    import re
    text = SETTINGS_PATH.read_text()
    text = re.sub(
        r'(provider:\s*)"[^"]*"',
        f'\\1"{new_provider}"',
        text,
        count=1,
    )
    SETTINGS_PATH.write_text(text)


def _run_device_flow_background():
    """Run device auth flow in background thread."""
    try:
        from llm.device_auth import request_device_code, poll_for_authorization, exchange_code_for_tokens

        # Step 1: Get device code
        device_data = request_device_code()
        _active_flow["verify_url"] = "https://auth.openai.com/codex/device"
        _active_flow["user_code"] = device_data["user_code"]
        _active_flow["status"] = "waiting"

        # Step 2: Poll for authorization
        auth_data = poll_for_authorization(
            device_data["device_auth_id"],
            device_data["user_code"],
            int(device_data.get("interval", 5)),
        )

        # Step 3: Exchange for tokens
        tokens = exchange_code_for_tokens(
            auth_data["authorization_code"],
            auth_data["code_verifier"],
        )

        # Step 4: Save token
        token_path = _get_token_path()
        token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = token_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(tokens, indent=2))
        tmp.chmod(0o600)
        tmp.rename(token_path)

        _active_flow["status"] = "success"
        logger.info("OAuth device flow completed successfully")

    except TimeoutError:
        _active_flow["status"] = "error"
        _active_flow["error"] = "인증 시간이 초과되었습니다 (15분). 다시 시도해주세요."
    except Exception as e:
        _active_flow["status"] = "error"
        _active_flow["error"] = str(e)
        logger.error(f"Device flow failed: {e}")
    finally:
        _active_flow["active"] = False


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    provider = _get_current_provider()
    token_status = _get_token_status()
    return templates.TemplateResponse("settings/index.html", {
        "request": request,
        "provider": provider,
        "token_status": token_status,
        "flow": _active_flow,
    })


@router.post("/api/settings/provider", response_class=HTMLResponse)
async def switch_provider(request: Request):
    """Toggle LLM provider and return updated UI fragment."""
    form = await request.form()
    new_provider = form.get("provider", "openrouter")

    if new_provider not in ("openrouter", "openai_oauth"):
        return HTMLResponse('<div class="error">잘못된 provider</div>', status_code=400)

    _switch_provider(new_provider)

    token_status = _get_token_status()
    return templates.TemplateResponse("settings/_provider_status.html", {
        "request": request,
        "provider": new_provider,
        "token_status": token_status,
        "flow": _active_flow,
    })


@router.post("/api/settings/oauth/start", response_class=HTMLResponse)
async def start_oauth_flow(request: Request):
    """Start OAuth device flow in background and return auth UI."""
    if _active_flow["active"]:
        return templates.TemplateResponse("settings/_oauth_flow.html", {
            "request": request,
            "flow": _active_flow,
        })

    _active_flow["active"] = True
    _active_flow["status"] = "starting"
    _active_flow["error"] = None
    _active_flow["verify_url"] = None
    _active_flow["user_code"] = None

    thread = threading.Thread(target=_run_device_flow_background, daemon=True)
    thread.start()
    _active_flow["thread"] = thread

    # Wait briefly for device code to arrive
    for _ in range(20):
        if _active_flow["user_code"] or _active_flow["status"] == "error":
            break
        time.sleep(0.25)

    return templates.TemplateResponse("settings/_oauth_flow.html", {
        "request": request,
        "flow": _active_flow,
    })


@router.get("/api/settings/oauth/poll", response_class=HTMLResponse)
async def poll_oauth_status(request: Request):
    """Poll OAuth flow status (called by HTMX polling)."""
    return templates.TemplateResponse("settings/_oauth_flow.html", {
        "request": request,
        "flow": _active_flow,
    })


@router.get("/api/settings/oauth/status", response_class=JSONResponse)
async def oauth_token_status():
    """Get current OAuth token status as JSON."""
    return _get_token_status()
