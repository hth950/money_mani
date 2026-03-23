"""OpenAI OAuth LLM client with token management and auto-refresh."""

import json
import logging
import threading
import time
from pathlib import Path

import requests
from llm.client import BaseLLMClient

logger = logging.getLogger("money_mani.llm.openai_oauth_client")


class OpenAIOAuthClient(BaseLLMClient):
    """OpenAI API client authenticated via OAuth device flow.

    Manages token lifecycle: load from disk, auto-refresh on expiry,
    re-run device flow if refresh fails.
    """

    BASE_URL = "https://api.openai.com/v1/chat/completions"
    DEFAULT_TOKEN_PATH = "~/.money_mani/openai_oauth_token.json"

    def __init__(self):
        super().__init__()
        oauth_cfg = self._llm_cfg.get("openai_oauth", {})
        self._default_model = oauth_cfg.get("default_model", "gpt-5.4-mini")
        self._fast_model = oauth_cfg.get("fast_model", "gpt-5.4-mini")
        self._deep_model = oauth_cfg.get("deep_model", "gpt-5.4-mini")
        self._lite_model = oauth_cfg.get("lite_model", "gpt-5.4-mini")
        self._token_path = Path(
            oauth_cfg.get("token_path", self.DEFAULT_TOKEN_PATH)
        ).expanduser()
        self._token_data = None
        self._token_lock = threading.Lock()
        self._load_or_auth()

    def _load_or_auth(self):
        """Load token from disk or run device auth flow."""
        if self._token_path.exists():
            try:
                self._token_data = json.loads(self._token_path.read_text())
                if self._is_token_expired():
                    logger.info("Access token expired, refreshing...")
                    self._refresh_token()
                else:
                    logger.info("Loaded existing OpenAI OAuth token")
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Invalid token file: {e}, re-authenticating")
                self._run_device_flow()
        else:
            self._run_device_flow()

    def _is_token_expired(self) -> bool:
        """Check if the current access token is expired (with 60s buffer)."""
        if not self._token_data:
            return True
        expires_at = self._token_data.get("expires_at", 0)
        return time.time() >= (expires_at - 60)

    def _run_device_flow(self):
        """Run device auth flow and save token."""
        from llm.device_auth import run_device_flow
        self._token_data = run_device_flow()
        self._save_token()

    def _save_token(self):
        """Save token to disk with restricted permissions (atomic write)."""
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._token_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._token_data, indent=2))
        tmp.chmod(0o600)
        tmp.rename(self._token_path)
        logger.info(f"Token saved to {self._token_path}")

    def _refresh_token(self):
        """Refresh access token. Falls back to device flow on failure."""
        with self._token_lock:
            # Double-check after acquiring lock (another thread may have refreshed)
            if not self._is_token_expired():
                return
            try:
                from llm.device_auth import refresh_access_token
                new_tokens = refresh_access_token(
                    self._token_data["refresh_token"]
                )
                self._token_data.update(new_tokens)
                self._save_token()
                logger.info("Token refreshed successfully")
            except Exception as e:
                logger.warning(f"Token refresh failed: {e}, re-running device flow")
                self._notify_reauth_needed()
                self._run_device_flow()

    def _notify_reauth_needed(self):
        """Send Discord notification that re-authentication is needed."""
        try:
            from alerts.discord_webhook import DiscordNotifier
            notifier = DiscordNotifier()
            notifier.send(
                content="⚠️ OpenAI OAuth 토큰 갱신 실패 — 재인증이 필요합니다. "
                        "서버에서 `python -m llm.cli_auth` 를 실행하세요."
            )
        except Exception:
            logger.warning("Failed to send Discord re-auth notification")

    def _get_headers(self) -> dict:
        """Build request headers with current access token."""
        return {
            "Authorization": f"Bearer {self._token_data['access_token']}",
            "Content-Type": "application/json",
        }

    def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Send chat messages via OpenAI API and return assistant response.

        Args:
            messages: List of {role, content} dicts.
            model: Model alias ("fast", "deep", "default", "lite") or full model ID.
            temperature: Sampling temperature. Defaults to config value.
            max_tokens: Max output tokens. Defaults to config value.

        Returns:
            Assistant response as a string.

        Raises:
            RuntimeError: After all retries are exhausted.
        """
        # Refresh token if expired before making request
        if self._is_token_expired():
            self._refresh_token()

        payload = {
            "model": self._resolve_model(model),
            "messages": messages,
            "temperature": temperature if temperature is not None else self._temperature,
            "max_tokens": max_tokens if max_tokens is not None else self._max_tokens,
        }

        last_error = None
        retried_auth = False

        for attempt in range(self.MAX_RETRIES):
            try:
                resp = requests.post(
                    self.BASE_URL,
                    headers=self._get_headers(),
                    json=payload,
                    timeout=60,
                )
                # Auto-refresh on 401 (once)
                if resp.status_code == 401 and not retried_auth:
                    logger.info("Got 401, attempting token refresh...")
                    retried_auth = True
                    self._refresh_token()
                    continue

                if resp.status_code in (429, 500, 502, 503, 504):
                    wait = 2 ** attempt
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            except requests.exceptions.RequestException as exc:
                last_error = str(exc)
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)

        raise RuntimeError(
            f"OpenAI request failed after {self.MAX_RETRIES} retries: {last_error}"
        )
