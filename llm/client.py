"""LLM client abstraction with OpenRouter implementation and provider factory."""

import time
from abc import ABC, abstractmethod

import requests
from utils.config_loader import load_config, get_env


class BaseLLMClient(ABC):
    """Abstract base for LLM API clients."""

    MAX_RETRIES = 3

    def __init__(self):
        cfg = load_config()
        self._llm_cfg = cfg.get("llm", {})
        self._max_tokens = self._llm_cfg.get("max_tokens", 4096)
        self._temperature = self._llm_cfg.get("temperature", 0.3)

    def _resolve_model(self, model: str | None) -> str:
        if model is None or model == "default":
            return self._default_model
        if model == "fast":
            return self._fast_model
        if model == "deep":
            return self._deep_model
        if model == "lite":
            return self._lite_model
        return model

    @abstractmethod
    def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Send chat messages and return assistant response text."""


class OpenRouterClient(BaseLLMClient):
    """HTTP client for OpenRouter API with exponential backoff retry."""

    BASE_URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self):
        super().__init__()
        api_key = self._llm_cfg.get("api_key") or get_env("OPENROUTER_KEY")
        if not api_key:
            raise ValueError("OPENROUTER_KEY not found in config or environment")
        self._api_key = api_key
        self._default_model = self._llm_cfg.get("default_model", "anthropic/claude-sonnet-4")
        self._fast_model = self._llm_cfg.get("fast_model", "anthropic/claude-haiku-4")
        self._deep_model = self._llm_cfg.get("deep_model", "anthropic/claude-opus-4")
        self._lite_model = self._llm_cfg.get("lite_model", "anthropic/claude-haiku-4")
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
        """Send chat messages and return assistant response text.

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

        raise RuntimeError(f"OpenRouter request failed after {self.MAX_RETRIES} retries: {last_error}")


def create_llm_client() -> BaseLLMClient:
    """Factory: create LLM client based on config provider setting.

    Returns OpenRouterClient or OpenAIOAuthClient depending on
    llm.provider in config/settings.yaml.
    """
    cfg = load_config()
    provider = cfg.get("llm", {}).get("provider", "openrouter")
    if provider == "openai_oauth":
        from llm.openai_oauth_client import OpenAIOAuthClient
        return OpenAIOAuthClient()
    return OpenRouterClient()
