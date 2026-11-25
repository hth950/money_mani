"""Opt-in, single-attempt OpenRouter calls for the frozen-news comparison."""

import hashlib
import os
import time
from decimal import Decimal, ROUND_FLOOR

import requests

from llm.selection_experiment import (
    BUDGET, FLASH, LITE, MODELS, USD_SCALE, ExperimentBlocked, ProviderBudget, cost_nanos,
    finish_attempt, reserve_pair,
)
from utils.config_loader import load_config


BASE_URL = "https://openrouter.ai/api/v1"
KEY_ENV = "OPENROUTER_EXPERIMENT_KEY"
PROVIDER = "google-ai-studio"
PROVIDER_NAME = "Google AI Studio"
# USD per million tokens. The matching request also carries these ceilings.
PRICE_CAPS = {LITE: (0.25, 1.50), FLASH: (0.50, 3.00)}


class ExperimentClient:
    def __init__(self, key=None):
        config = load_config().get("llm", {})
        key = (key if key is not None else os.getenv(KEY_ENV, "")).strip()
        production_keys = {config.get("api_key"), os.getenv("OPENROUTER_KEY"), os.getenv("OPENROUTER_API_KEY")}
        if not key or key in production_keys:
            raise ExperimentBlocked(f"Supply a dedicated inference-only key in {KEY_ENV}.")
        self._headers = {"Authorization": f"Bearer {key}"}
        self._fingerprint = hashlib.sha256(key.encode()).hexdigest()
        self._completion_bounds = None

    def _metadata(self, path, *, authorized=False):
        try:
            response = requests.get(
                BASE_URL + path, headers=self._headers if authorized else None,
                timeout=(8, 15), allow_redirects=False,
            )
            if response.status_code != 200:
                raise ExperimentBlocked(f"OpenRouter metadata request returned HTTP {response.status_code}.")
            data = response.json()["data"]
            if not isinstance(data, dict):
                raise ValueError("unexpected metadata")
            return data
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise ExperimentBlocked("OpenRouter metadata could not be verified.") from exc

    def budget(self):
        data = self._metadata("/key", authorized=True)
        try:
            limit = cost_nanos(data["limit"])
            remaining = Decimal(str(data["limit_remaining"])) * USD_SCALE
            valid = (
                0 < limit <= BUDGET and remaining.is_finite() and 0 <= remaining <= limit
                and data["limit_reset"] is None and data["is_management_key"] is False
                and data.get("is_provisioning_key") is not True
                and data["include_byok_in_limit"] is True and cost_nanos(data["byok_usage"]) == 0
            )
            if valid:
                return ProviderBudget(self._fingerprint, limit, int(remaining.to_integral_value(rounding=ROUND_FLOOR)))
        except (KeyError, ArithmeticError, ValueError):
            pass
        raise ExperimentBlocked(
            "The experiment key must have a non-resetting limit of at most $8, count BYOK toward the limit, "
            "and have no BYOK usage."
        )

    def verify_pricing(self):
        bounds = {}
        for model, (prompt_cap, completion_cap) in PRICE_CAPS.items():
            data = self._metadata(f"/models/{model}/endpoints")
            try:
                endpoints = [endpoint for endpoint in data["endpoints"] if endpoint["tag"] == PROVIDER]
                if data["id"] != model or len(endpoints) != 1:
                    raise ValueError("provider changed")
                endpoint = endpoints[0]
                pricing = endpoint["pricing"]
                rates = {
                    part: cost_nanos(Decimal(str(pricing.get(part, default))) * 1_000_000)
                    for part, default in {
                        "prompt": None, "completion": None, "internal_reasoning": pricing.get("completion"),
                        "input_cache_read": 0, "input_cache_write": 0,
                    }.items()
                }
                maximum = endpoint["max_completion_tokens"]
                if (
                    any(rates[part] > cost_nanos(prompt_cap) for part in ("prompt", "input_cache_read", "input_cache_write"))
                    or any(rates[part] > cost_nanos(completion_cap) for part in ("completion", "internal_reasoning"))
                    or cost_nanos(pricing.get("request", 0)) != 0
                    or not isinstance(maximum, int) or not 4096 <= maximum <= 65536
                ):
                    raise ValueError("endpoint exceeds supported ceiling")
                bounds[model] = maximum
            except (KeyError, TypeError, ArithmeticError, ValueError) as exc:
                raise ExperimentBlocked(f"Could not bound the current price and output limit for {model}.") from exc
        self._completion_bounds = bounds

    def reservations(self, packet):
        if self._completion_bounds is None:
            raise ExperimentBlocked("Verify model pricing before reserving inference.")
        messages = packet["messages"]
        if (
            len(messages) != 1 or messages[0].get("role") != "user"
            or not isinstance(messages[0].get("content"), str)
            or not 0 < packet["max_tokens"] <= 4096
        ):
            raise ExperimentBlocked("The frozen request exceeds the experiment's text-only limits.")
        # Byte length bounds text tokens with room for the message wrapper. Reserve the endpoint's
        # full completion limit for reasoning in addition to the requested visible output budget.
        prompt_bound = len(messages[0]["content"].encode("utf-8")) + 512
        return {
            model: cost_nanos((
                Decimal(str(prompt)) * prompt_bound
                + Decimal(str(completion)) * (self._completion_bounds[model] + packet["max_tokens"])
            ) / 1_000_000)
            for model, (prompt, completion) in PRICE_CAPS.items()
        }

    def complete(self, packet, model):
        prompt, completion = PRICE_CAPS[model]
        body = {
            "model": model,
            "messages": packet["messages"],
            "temperature": packet["temperature"],
            "max_tokens": packet["max_tokens"],
            "provider": {
                "only": [PROVIDER], "allow_fallbacks": False, "require_parameters": True,
                "max_price": {"prompt": prompt, "completion": completion, "request": 0},
            },
            "usage": {"include": True},
            "plugins": [],
        }
        start = time.monotonic()
        try:
            response = requests.post(
                BASE_URL + "/chat/completions", headers=self._headers, json=body,
                timeout=(10, 90), allow_redirects=False,
            )
            if response.status_code != 200:
                return None, {"reason": f"http_{response.status_code}"}
            data = response.json()
            choices = data.get("choices") or []
            choice = choices[0] if choices else {}
            usage = data.get("usage") or {}
            details = {
                "generation_id": data.get("id"), "provider": data.get("provider"),
                "reported_model": data.get("model"), "elapsed_ms": round((time.monotonic() - start) * 1000),
                "finish_reason": choice.get("finish_reason"), "raw": (choice.get("message") or {}).get("content"),
                "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
            }
            if usage.get("is_byok") is True:
                return None, {**details, "reason": "byok_usage"}
            if data.get("model") != model or data.get("provider") != PROVIDER_NAME:
                return None, {**details, "reason": "model_or_provider_mismatch"}
            billed = cost_nanos(usage.get("cost"))
            return billed, details
        except (requests.RequestException, ValueError, TypeError, AttributeError, KeyError, IndexError) as exc:
            return None, {"reason": type(exc).__name__}


def run_comparison(stage, limit, *, client=None):
    if limit < 1:
        raise ValueError("At least one pair must be requested.")
    client = client or ExperimentClient()
    client.verify_pricing()
    attempted = 0
    for _ in range(limit):
        packet, reason = reserve_pair(stage, client.reservations, client.budget())
        if packet is None:
            return {"pairs_started": attempted, "stopped": reason}
        attempted += 1
        order = MODELS if packet["id"] % 2 else MODELS[::-1]
        for index, model in enumerate(order):
            billed, details = client.complete(packet, model)
            if billed is None:
                finish_attempt(packet["id"], model, "uncertain", details)
            else:
                finish_attempt(packet["id"], model, "settled", details, billed)
            exceeded = billed is not None and billed > client.reservations(packet)[model]
            if billed is None or exceeded:
                if index == 0:
                    finish_attempt(packet["id"], order[1], "skipped", {"reason": "first_attempt_unverified"})
                if exceeded:
                    raise ExperimentBlocked("OpenRouter reported a charge above the reservation. Experiment stopped.")
                return {"pairs_started": attempted, "stopped": "charge_uncertain_reservation_retained"}
    return {"pairs_started": attempted, "stopped": "requested_pair_limit"}
