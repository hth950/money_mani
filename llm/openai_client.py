"""OpenAI API client with API key authentication."""

import logging
import time

import requests
from llm.client import BaseLLMClient
from utils.config_loader import get_env

logger = logging.getLogger("money_mani.llm.openai_client")


class OpenAIClient(BaseLLMClient):
    """OpenAI API client using standard API key (Bearer token)."""

    BASE_URL = "https://api.openai.com/v1/chat/completions"

    def __init__(self):
        super().__init__()
        openai_cfg = self._llm_cfg.get("openai", {})
        api_key = openai_cfg.get("api_key") or get_env("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not found in config or environment")
        self._api_key = api_key
        self._default_model = openai_cfg.get("default_model", "gpt-5.4-mini")
        self._fast_model = openai_cfg.get("fast_model", "gpt-5.4-mini")
        self._deep_model = openai_cfg.get("deep_model", "gpt-5.4-mini")
        self._lite_model = openai_cfg.get("lite_model", "gpt-5.4-mini")
        self._headers = {
            "Authorization": f"Bearer {self._api_key}",
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
        payload = {
            "model": self._resolve_model(model),
            "messages": messages,
            "temperature": temperature if temperature is not None else self._temperature,
            "max_tokens": max_tokens if max_tokens is not None else self._max_tokens,
        }

        last_error = None
        for attempt in range(self.MAX_RETRIES):
            try:
                resp = requests.post(
                    self.BASE_URL,
                    headers=self._headers,
                    json=payload,
                    timeout=60,
                )
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
