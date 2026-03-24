"""LLM provider settings: toggle OpenRouter / OpenAI OAuth with web UI."""

import json
import logging
import subprocess
import time
import threading
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
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
    token_path = _get_token_path()
    if not token_path.exists():
        return {"exists": False, "valid": False}
    try:
        data = json.loads(token_path.read_text())
        expires_at = data.get("expires_at", 0)
        has_refresh = bool(data.get("refresh_token"))
        return {
            "exists": True,
            "valid": time.time() < expires_at or has_refresh,
            "has_refresh": has_refresh,
        }
    except Exception:
        return {"exists": True, "valid": False}


def _switch_provider(new_provider: str):
    import re
    text = SETTINGS_PATH.read_text()
    text = re.sub(
        r'(provider:\s*)"[^"]*"',
        f'\\1"{new_provider}"',
        text,
        count=1,
    )
    SETTINGS_PATH.write_text(text)


def _restart_services():
    def _do_restart():
        try:
            result = subprocess.run(
                ["sudo", "systemctl", "restart", "money-mani", "money-mani-scheduler"],
                timeout=30, capture_output=True, text=True,
            )
            if result.returncode == 0:
                logger.info("Services restarted successfully")
            else:
                logger.error(f"Service restart failed (exit {result.returncode}): {result.stderr}")
        except Exception as e:
            logger.error(f"Service restart failed: {e}")
    threading.Thread(target=_do_restart, daemon=True).start()


def _run_device_flow_background():
    try:
        from llm.device_auth import request_device_code, poll_for_authorization, exchange_code_for_tokens

        device_data = request_device_code()
        _active_flow["verify_url"] = "https://auth.openai.com/codex/device"
        _active_flow["user_code"] = device_data["user_code"]
        _active_flow["status"] = "waiting"

        auth_data = poll_for_authorization(
            device_data["device_auth_id"],
            device_data["user_code"],
            int(device_data.get("interval", 5)),
        )

        # Get tokens — either directly or via code exchange
        if "access_token" in auth_data:
            import time as _time
            expires_in = auth_data.get("expires_in", 3600)
            tokens = {
                "access_token": auth_data["access_token"],
                "refresh_token": auth_data.get("refresh_token", ""),
                "id_token": auth_data.get("id_token", ""),
                "expires_at": int(_time.time()) + expires_in,
            }
        elif "authorization_code" in auth_data:
            tokens = exchange_code_for_tokens(
                auth_data["authorization_code"],
                auth_data.get("code_verifier", ""),
            )
        else:
            raise RuntimeError(f"Unexpected auth response: {list(auth_data.keys())}")

        # Save token
        token_path = _get_token_path()
        token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = token_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(tokens, indent=2))
        tmp.chmod(0o600)
        tmp.rename(token_path)

        _active_flow["status"] = "success"
        logger.info("OAuth device flow completed successfully")

        # Switch provider and restart
        _switch_provider("openai_oauth")
        _restart_services()

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
    form = await request.form()
    new_provider = form.get("provider", "openrouter")

    if new_provider == "openrouter":
        _switch_provider("openrouter")
        _restart_services()
        return templates.TemplateResponse("settings/_provider_status.html", {
            "request": request,
            "provider": "openrouter",
            "token_status": _get_token_status(),
            "flow": _active_flow,
            "switched": True,
        })
    elif new_provider == "openai_oauth":
        # Check if we already have a valid token
        token_status = _get_token_status()
        if token_status.get("valid"):
            _switch_provider("openai_oauth")
            _restart_services()
            return templates.TemplateResponse("settings/_provider_status.html", {
                "request": request,
                "provider": "openai_oauth",
                "token_status": token_status,
                "flow": _active_flow,
                "switched": True,
            })
        else:
            # Need to authenticate first — show OAuth flow
            return templates.TemplateResponse("settings/_provider_status.html", {
                "request": request,
                "provider": _get_current_provider(),
                "token_status": token_status,
                "flow": _active_flow,
                "need_auth": True,
            })

    return HTMLResponse('<div class="error">잘못된 provider</div>', status_code=400)


@router.post("/api/settings/oauth/start", response_class=HTMLResponse)
async def start_oauth_flow(request: Request):
    if _active_flow["active"]:
        return templates.TemplateResponse("settings/_oauth_flow.html", {
            "request": request, "flow": _active_flow,
        })

    _active_flow["active"] = True
    _active_flow["status"] = "starting"
    _active_flow["error"] = None
    _active_flow["verify_url"] = None
    _active_flow["user_code"] = None

    thread = threading.Thread(target=_run_device_flow_background, daemon=True)
    thread.start()

    for _ in range(20):
        if _active_flow["user_code"] or _active_flow["status"] == "error":
            break
        time.sleep(0.25)

    return templates.TemplateResponse("settings/_oauth_flow.html", {
        "request": request, "flow": _active_flow,
    })


@router.get("/api/settings/oauth/poll", response_class=HTMLResponse)
async def poll_oauth_status(request: Request):
    return templates.TemplateResponse("settings/_oauth_flow.html", {
        "request": request, "flow": _active_flow,
    })


@router.post("/api/settings/test", response_class=HTMLResponse)
async def test_provider(request: Request):
    provider = _get_current_provider()
    try:
        from llm.client import create_llm_client
        client = create_llm_client()
        result = client.chat(
            [{"role": "user", "content": "say ok"}],
            model="fast", max_tokens=10,
        )
        return templates.TemplateResponse("settings/_test_result.html", {
            "request": request, "provider": provider,
            "result": {"ok": True, "response": result[:50]},
        })
    except Exception as e:
        return templates.TemplateResponse("settings/_test_result.html", {
            "request": request, "provider": provider,
            "result": {"ok": False, "error": str(e)[:200]},
        })
