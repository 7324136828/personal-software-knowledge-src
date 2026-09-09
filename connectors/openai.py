"""OpenAI Responses API connector."""

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
class OpenAIConnector(LLMConnector):
    """Generate text with the OpenAI Responses API."""

    model: str | None = None
    api_key: str | None = None

    def __post_init__(self) -> None:
        self.model = self.model or os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
        self.api_key = self.api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ConnectorConfigurationError(
                "OPENAI_API_KEY is required when --connector openai is used."
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
                "The 'openai' package is not installed. Run: pip install -r requirements.txt"
            ) from exc

        try:
            profile, allowance = request_budget(
                "openai", self.model, system_prompt, user_prompt,
                max_output_tokens, context_window,
            )
            request: dict[str, object] = {
                "model": self.model, "instructions": system_prompt,
                "input": user_prompt, "max_output_tokens": allowance,
            }
            temperature = request_temperature(temperature)
            if temperature is not None:
                request["temperature"] = temperature
            if json_mode and profile.structured_output:
                request["text"] = {"format": {"type": "json_object"}}
            response = OpenAI(api_key=self.api_key).responses.create(**request)
            result = value_of(response, "output_text", "")
            usage = value_of(response, "usage", {})
            incomplete = value_of(response, "incomplete_details", {})
            reason = value_of(incomplete, "reason")
            if not reason and value_of(response, "status") == "incomplete":
                reason = "incomplete"
            generated = GenerationResponse(
                text=result,
                finish_reason=reason or value_of(response, "status"),
                input_tokens=token_count(usage, "input_tokens"),
                output_tokens=token_count(usage, "output_tokens"),
                raw_metadata={"response_id": value_of(response, "id"),
                              "status": value_of(response, "status")},
            )
        except Exception as exc:
            raise provider_failure("OpenAI", exc) from exc
        return checked_response(generated, "OpenAI")
