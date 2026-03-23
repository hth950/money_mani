"""LLM provider settings: toggle OpenRouter / OpenAI with web UI."""

import logging
import subprocess
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


def _get_current_provider() -> str:
    cfg = load_config()
    return cfg.get("llm", {}).get("provider", "openrouter")


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


def _restart_services():
    """Restart money-mani services in background."""
    def _do_restart():
        try:
            subprocess.run(
                ["sudo", "systemctl", "restart", "money-mani", "money-mani-scheduler"],
                timeout=30, capture_output=True,
            )
            logger.info("Services restarted successfully")
        except Exception as e:
            logger.error(f"Service restart failed: {e}")
    threading.Thread(target=_do_restart, daemon=True).start()


def _test_provider(provider: str) -> dict:
    """Quick test if the provider is working."""
    try:
        from llm.client import create_llm_client
        client = create_llm_client()
        result = client.chat(
            [{"role": "user", "content": "say ok"}],
            model="fast", max_tokens=10,
        )
        return {"ok": True, "response": result[:50]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    provider = _get_current_provider()
    return templates.TemplateResponse("settings/index.html", {
        "request": request,
        "provider": provider,
    })


@router.post("/api/settings/provider", response_class=HTMLResponse)
async def switch_provider(request: Request):
    """Toggle LLM provider and return updated UI fragment."""
    form = await request.form()
    new_provider = form.get("provider", "openrouter")

    if new_provider not in ("openrouter", "openai"):
        return HTMLResponse('<div class="error">잘못된 provider</div>', status_code=400)

    _switch_provider(new_provider)
    _restart_services()

    return templates.TemplateResponse("settings/_provider_status.html", {
        "request": request,
        "provider": new_provider,
        "switched": True,
    })


@router.post("/api/settings/test", response_class=HTMLResponse)
async def test_provider(request: Request):
    """Test current provider with a simple API call."""
    provider = _get_current_provider()
    result = _test_provider(provider)
    return templates.TemplateResponse("settings/_test_result.html", {
        "request": request,
        "provider": provider,
        "result": result,
    })
