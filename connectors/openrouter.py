"""OpenRouter connector using its OpenAI-compatible chat API."""

from __future__ import annotations

import os
from dataclasses import dataclass

from errors import ConnectorConfigurationError

from .base import (
    GenerationResponse,
    LLMConnector,
    checked_response,
    provider_failure,
    request_budget,
    request_temperature,
    token_count,
    value_of,
)


@dataclass
class OpenRouterConnector(LLMConnector):
    """Generate text through OpenRouter's OpenAI-compatible endpoint."""

    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None

    def __post_init__(self) -> None:
        self.model = self.model or os.getenv(
            "OPENROUTER_MODEL", "openai/gpt-4.1-mini"
        )
        self.api_key = self.api_key or os.getenv("OPENROUTER_API_KEY")
        self.base_url = self.base_url or os.getenv(
            "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
        )
        if not self.api_key:
            raise ConnectorConfigurationError(
                "OPENROUTER_API_KEY is required when --connector openrouter is used."
            )

    def generate(self, *, system_prompt: str, user_prompt: str) -> str:
        return self.generate_response(
            system_prompt=system_prompt, user_prompt=user_prompt
        ).text

    def generate_response(
        self, *, system_prompt: str, user_prompt: str,
        max_output_tokens: int | None = None, temperature: float | None = None,
        json_mode: bool = False, context_window: int | None = None,
    ) -> GenerationResponse:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ConnectorConfigurationError(
                "The 'openai' package is required for OpenRouter. "
                "Run: pip install -r requirements.txt"
            ) from exc

        headers = {}
        if referer := os.getenv("OPENROUTER_HTTP_REFERER"):
            headers["HTTP-Referer"] = referer
        if title := os.getenv("OPENROUTER_APP_NAME"):
            headers["X-Title"] = title

        try:
            client_options: dict[str, object] = {
                "api_key": self.api_key,
                "base_url": self.base_url,
            }
            if headers:
                client_options["default_headers"] = headers
            profile, allowance = request_budget(
                "openrouter", self.model, system_prompt, user_prompt,
                max_output_tokens, context_window,
            )
            request: dict[str, object] = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": allowance,
            }
            temperature = request_temperature(temperature)
            if temperature is not None:
                request["temperature"] = temperature
            if json_mode and profile.structured_output:
                request["response_format"] = {"type": "json_object"}
            response = OpenAI(**client_options).chat.completions.create(**request)
            choice = response.choices[0]
            result = value_of(value_of(choice, "message", {}), "content", "")
            usage = value_of(response, "usage", {})
            generated = GenerationResponse(
                text=result, finish_reason=value_of(choice, "finish_reason"),
                input_tokens=token_count(usage, "prompt_tokens"),
                output_tokens=token_count(usage, "completion_tokens"),
                raw_metadata={"response_id": value_of(response, "id"),
                              "native_finish_reason": value_of(choice, "native_finish_reason")},
            )
        except Exception as exc:
            raise provider_failure("OpenRouter", exc) from exc
        return checked_response(generated, "OpenRouter")
