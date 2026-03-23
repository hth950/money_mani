"""OpenAI OAuth LLM client using ChatGPT Backend API (Codex endpoint)."""

import json
import logging
import threading
import time
from pathlib import Path

import requests
from llm.client import BaseLLMClient
from llm.device_auth import extract_account_id

logger = logging.getLogger("money_mani.llm.openai_oauth_client")


class OpenAIOAuthClient(BaseLLMClient):
    """ChatGPT Backend API client authenticated via OAuth device flow.

    Uses the Codex responses endpoint (chatgpt.com/backend-api/codex/responses)
    instead of the standard OpenAI API. This endpoint works with ChatGPT
    subscription OAuth tokens that don't have platform API scopes.
    """

    BASE_URL = "https://chatgpt.com/backend-api/codex/responses"
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
        self._account_id = None
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

        # Extract account_id from id_token
        self._account_id = extract_account_id(
            self._token_data.get("id_token", "")
        )
        if not self._account_id:
            logger.warning("Could not extract account_id from id_token. "
                           "Re-authentication may be needed.")

    def _is_token_expired(self) -> bool:
        if not self._token_data:
            return True
        expires_at = self._token_data.get("expires_at", 0)
        return time.time() >= (expires_at - 60)

    def _run_device_flow(self):
        from llm.device_auth import run_device_flow
        self._token_data = run_device_flow()
        self._save_token()

    def _save_token(self):
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._token_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._token_data, indent=2))
        tmp.chmod(0o600)
        tmp.rename(self._token_path)
        logger.info(f"Token saved to {self._token_path}")

    def _refresh_token(self):
        with self._token_lock:
            if not self._is_token_expired():
                return
            try:
                from llm.device_auth import refresh_access_token
                new_tokens = refresh_access_token(
                    self._token_data["refresh_token"]
                )
                self._token_data.update(new_tokens)
                self._save_token()
                # Update account_id if new id_token received
                if new_tokens.get("id_token"):
                    self._account_id = extract_account_id(new_tokens["id_token"])
                logger.info("Token refreshed successfully")
            except Exception as e:
                logger.warning(f"Token refresh failed: {e}, re-running device flow")
                self._notify_reauth_needed()
                self._run_device_flow()

    def _notify_reauth_needed(self):
        try:
            from alerts.discord_webhook import DiscordNotifier
            notifier = DiscordNotifier()
            notifier.send(
                content="⚠️ OpenAI OAuth 토큰 갱신 실패 — /settings 에서 재인증 필요"
            )
        except Exception:
            logger.warning("Failed to send Discord re-auth notification")

    def _get_headers(self) -> dict:
        headers = {
            "Authorization": f"Bearer {self._token_data['access_token']}",
            "Content-Type": "application/json",
            "OpenAI-Beta": "responses=experimental",
        }
        if self._account_id:
            headers["chatgpt-account-id"] = self._account_id
        return headers

    @staticmethod
    def _convert_messages(messages: list[dict]) -> list[dict]:
        """Convert Chat Completions messages to Responses API input format."""
        result = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                content = [{"type": "input_text", "text": content}]
            result.append({"role": msg["role"], "content": content})
        return result

    @staticmethod
    def _parse_sse_response(resp: requests.Response) -> str:
        """Parse SSE streaming response and extract text content."""
        full_text = []
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str == "[DONE]":
                break
            try:
                event = json.loads(data_str)
                event_type = event.get("type", "")
                if event_type == "response.output_text.delta":
                    full_text.append(event.get("delta", ""))
            except json.JSONDecodeError:
                continue
        return "".join(full_text)

    def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Send chat messages via ChatGPT Backend API and return response.

        Args:
            messages: List of {role, content} dicts.
            model: Model alias or full model ID.
            temperature: Sampling temperature.
            max_tokens: Ignored (not supported by this endpoint).

        Returns:
            Assistant response as a string.
        """
        if self._is_token_expired():
            self._refresh_token()

        payload = {
            "model": self._resolve_model(model),
            "stream": True,
            "store": False,
            "input": self._convert_messages(messages),
        }
        if temperature is not None:
            payload["temperature"] = temperature
        elif self._temperature:
            payload["temperature"] = self._temperature

        last_error = None
        retried_auth = False

        for attempt in range(self.MAX_RETRIES):
            try:
                resp = requests.post(
                    self.BASE_URL,
                    headers=self._get_headers(),
                    json=payload,
                    timeout=120,
                    stream=True,
                )
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
                return self._parse_sse_response(resp)
            except requests.exceptions.RequestException as exc:
                last_error = str(exc)
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)

        raise RuntimeError(
            f"ChatGPT API request failed after {self.MAX_RETRIES} retries: {last_error}"
        )
