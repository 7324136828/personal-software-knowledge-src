"""OpenRouter connector using its OpenAI-compatible chat API."""

from __future__ import annotations

import os
from dataclasses import dataclass

from errors import ConnectorConfigurationError, ProviderError

from .base import LLMConnector


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
            response = OpenAI(**client_options).chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            result = response.choices[0].message.content
        except Exception as exc:
            raise ProviderError(f"OpenRouter request failed: {exc}") from exc

        if not result or not result.strip():
            raise ProviderError("OpenRouter returned an empty model response.")
        return result
