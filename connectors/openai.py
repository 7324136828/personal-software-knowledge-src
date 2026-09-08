"""OpenAI Responses API connector."""

from __future__ import annotations

import os
from dataclasses import dataclass

from errors import ConnectorConfigurationError, ProviderError

from .base import LLMConnector


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
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ConnectorConfigurationError(
                "The 'openai' package is not installed. Run: pip install -r requirements.txt"
            ) from exc

        try:
            response = OpenAI(api_key=self.api_key).responses.create(
                model=self.model,
                instructions=system_prompt,
                input=user_prompt,
            )
            result = response.output_text
        except Exception as exc:
            raise ProviderError(f"OpenAI request failed: {exc}") from exc

        if not result or not result.strip():
            raise ProviderError("OpenAI returned an empty model response.")
        return result
