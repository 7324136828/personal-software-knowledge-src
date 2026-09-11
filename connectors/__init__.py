"""Connector factory and supported-provider registry."""

from __future__ import annotations

from collections.abc import Callable

from errors import InvalidArgumentsError

from .base import LLMConnector


def _openai(model: str | None, api_key: str | None = None) -> LLMConnector:
    from .openai import OpenAIConnector

    return OpenAIConnector(model=model, api_key=api_key)


def _ollama(model: str | None, api_key: str | None = None) -> LLMConnector:
    from .ollama import OllamaConnector

    return OllamaConnector(model=model)


def _claude(model: str | None, api_key: str | None = None) -> LLMConnector:
    from .claude import ClaudeConnector

    return ClaudeConnector(model=model, api_key=api_key)


def _openrouter(model: str | None, api_key: str | None = None) -> LLMConnector:
    from .openrouter import OpenRouterConnector

    return OpenRouterConnector(model=model, api_key=api_key)


CONNECTORS: dict[str, Callable[[str | None, str | None], LLMConnector]] = {
    "anthropic": _claude,
    "openai": _openai,
    "ollama": _ollama,
    "claude": _claude,
    "openrouter": _openrouter,
}


def create_connector(
    name: str, *, model: str | None = None, api_key: str | None = None
) -> LLMConnector:
    """Create a configured connector by its CLI name."""

    normalized_name = name.strip().lower()
    try:
        factory = CONNECTORS[normalized_name]
    except KeyError as exc:
        supported = ", ".join(sorted(CONNECTORS))
        raise InvalidArgumentsError(
            f"Unknown connector '{name}'. Supported connectors: {supported}."
        ) from exc
    return factory(model, api_key)


__all__ = ["CONNECTORS", "LLMConnector", "create_connector"]
