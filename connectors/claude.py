"""Anthropic Claude connector."""

from __future__ import annotations

import os
from dataclasses import dataclass

from errors import ConnectorConfigurationError, ProviderError

from .base import LLMConnector


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
            raw_max_tokens = os.getenv("ANTHROPIC_MAX_TOKENS", "8192")
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
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise ConnectorConfigurationError(
                "The 'anthropic' package is not installed. Run: pip install -r requirements.txt"
            ) from exc

        try:
            response = Anthropic(api_key=self.api_key).messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            result = "".join(
                block.text for block in response.content if getattr(block, "type", None) == "text"
            )
        except Exception as exc:
            raise ProviderError(f"Anthropic request failed: {exc}") from exc

        if not result or not result.strip():
            raise ProviderError("Anthropic returned an empty model response.")
        return result
