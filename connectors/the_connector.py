"""The Connector's local, OpenAI-compatible model and completion API."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from errors import ConnectorConfigurationError, ProviderError

from .base import (
    GenerationResponse, LLMConnector, checked_response, finite_timeout,
    request_budget, request_temperature, token_count,
)


def connector_base_url(base_url: str | None = None) -> str:
    url = (base_url or os.getenv(
        "THE_CONNECTOR_BASE_URL", "http://127.0.0.1:8301/v1"
    )).rstrip("/")
    return url if url.endswith("/v1") else f"{url}/v1"


def _request(base_url: str, path: str, timeout: float,
             body: dict[str, Any] | None = None) -> dict[str, Any]:
    request = Request(
        f"{base_url}/{path}",
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        error = ProviderError(f"The Connector request failed (HTTP {exc.code}). Check its active configuration and routing settings.")
        error.status_code = exc.code
        exc.close()
        raise error from exc
    except (URLError, OSError) as exc:
        raise ProviderError("Could not reach The Connector. Start its backend and check THE_CONNECTOR_BASE_URL.") from exc
    except (ValueError, UnicodeError) as exc:
        raise ProviderError("The Connector returned invalid JSON.") from exc
    if not isinstance(data, dict) or data.get("error"):
        raise ProviderError("The Connector returned an invalid or error response.")
    return data


def discover_models(base_url: str | None = None, timeout: float | None = None) -> list[dict[str, Any]]:
    """Discover active library IDs, never the underlying provider catalog."""
    data = _request(connector_base_url(base_url), "models",
                    min(finite_timeout(timeout, "THE_CONNECTOR_TIMEOUT", 600.0), 10.0))
    models = data.get("data")
    if not isinstance(models, list) or any(
        not isinstance(model, dict) or not isinstance(model.get("id"), str)
        or not model["id"].strip() for model in models
    ):
        raise ProviderError("The Connector returned an invalid model list.")
    return models


@dataclass
class TheConnector(LLMConnector):
    model: str | None = None
    base_url: str | None = None
    timeout: float | None = None

    def __post_init__(self) -> None:
        self.base_url = connector_base_url(self.base_url)
        self.timeout = finite_timeout(self.timeout, "THE_CONNECTOR_TIMEOUT", 600.0)
        self.model = self.model or os.getenv("THE_CONNECTOR_MODEL")
        if not self.model:
            models = discover_models(self.base_url, self.timeout)
            if not models:
                raise ConnectorConfigurationError(
                    "The Connector has no active models. Save and activate a library configuration first."
                )
            self.model = models[0]["id"]

    def generate(self, *, system_prompt: str, user_prompt: str) -> str:
        return self.generate_response(system_prompt=system_prompt, user_prompt=user_prompt).text

    def generate_response(
        self, *, system_prompt: str, user_prompt: str,
        max_output_tokens: int | None = None, temperature: float | None = None,
        json_mode: bool = False, context_window: int | None = None,
    ) -> GenerationResponse:
        profile, allowance = request_budget(
            "the_connector", self.model, system_prompt, user_prompt,
            max_output_tokens, context_window,
        )
        body: dict[str, Any] = {
            "model": self.model, "stream": False, "max_tokens": allowance,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        temperature = request_temperature(temperature)
        if temperature is not None:
            body["temperature"] = temperature
        # Saved routes can use Claude or reasoning models; keep optional settings
        # absent unless explicitly supported by the operating profile.
        if json_mode and profile.structured_output:
            body["response_format"] = {"type": "json_object"}
        data = _request(self.base_url, "chat/completions", self.timeout, body)
        try:
            choice = data["choices"][0]
            message = choice["message"]
            usage = data.get("usage") or {}
            result = GenerationResponse(
                text=message.get("content"), finish_reason=choice.get("finish_reason"),
                input_tokens=token_count(usage, "prompt_tokens"),
                output_tokens=token_count(usage, "completion_tokens"),
                raw_metadata={"response_id": data.get("id")},
            )
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ProviderError("The Connector returned an invalid completion response.") from exc
        return checked_response(result, "The Connector")
