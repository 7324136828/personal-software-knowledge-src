"""Anthropic Claude connector."""

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
class ClaudeConnector(LLMConnector):
    """Generate text with Anthropic's Messages API."""

    model: str | None = None
    api_key: str | None = None
    max_tokens: int | None = None

    def __post_init__(self) -> None:
        self.model = self.model or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
        self.api_key = self.api_key or os.getenv("ANTHROPIC_API_KEY")
        if self.max_tokens is None:
            raw_max_tokens = os.getenv("ANTHROPIC_MAX_TOKENS", "16000")
            try:
                self.max_tokens = int(raw_max_tokens)
            except ValueError as exc:
                raise ConnectorConfigurationError(
                    "ANTHROPIC_MAX_TOKENS must be an integer."
                ) from exc
        if not self.api_key:
            raise ConnectorConfigurationError(
                "ANTHROPIC_API_KEY is required when --connector claude is used."
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
            from anthropic import Anthropic
        except ImportError as exc:
            raise ConnectorConfigurationError(
                "The 'anthropic' package is not installed. Run: pip install -r requirements.txt"
            ) from exc

        try:
            _, allowance = request_budget(
                "anthropic", self.model, system_prompt, user_prompt,
                max_output_tokens or self.max_tokens, context_window,
            )
            request: dict[str, object] = {
                "model": self.model, "max_tokens": allowance,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            }
            temperature = request_temperature(temperature)
            if temperature is not None:
                request["temperature"] = temperature
            # Claude JSON mode support varies by model/API version. A strict prompt
            # remains portable; the pipeline validates and repairs the result.
            if json_mode:
                request["system"] = system_prompt + "\nReturn valid JSON only, without a Markdown fence."
            response = Anthropic(api_key=self.api_key).messages.create(**request)
            result = "".join(
                block.text for block in response.content if getattr(block, "type", None) == "text"
            )
            usage = value_of(response, "usage", {})
            generated = GenerationResponse(
                text=result, finish_reason=value_of(response, "stop_reason"),
                input_tokens=token_count(usage, "input_tokens"),
                output_tokens=token_count(usage, "output_tokens"),
                raw_metadata={"response_id": value_of(response, "id"),
                              "stop_sequence": value_of(response, "stop_sequence")},
            )
        except Exception as exc:
            raise provider_failure("Anthropic", exc) from exc
        return checked_response(generated, "Anthropic")
