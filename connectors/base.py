"""Provider-neutral connector contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import math
import os
from typing import Any

from errors import ConnectorConfigurationError, ProviderError


@dataclass
class GenerationResponse:
    """Text plus provider evidence needed for validation, recovery and accounting."""

    text: str
    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def truncated(self) -> bool:
        return self.finish_reason in {
            "length", "max_tokens", "max_output_tokens", "model_context_window_exceeded",
            "context_length_exceeded", "incomplete",
        }


class LLMConnector(ABC):
    """Common interface implemented by every language-model provider."""

    model: str

    @abstractmethod
    def generate(self, *, system_prompt: str, user_prompt: str) -> str:
        """Generate and return text for a pair of system and user prompts."""

        raise NotImplementedError

    def generate_response(
        self, *, system_prompt: str, user_prompt: str,
        max_output_tokens: int | None = None, temperature: float | None = None,
        json_mode: bool = False, context_window: int | None = None,
    ) -> GenerationResponse:
        """Adapt legacy connectors; metadata and bounded requests need an override."""
        return GenerationResponse(self.generate(system_prompt=system_prompt, user_prompt=user_prompt))


def value_of(item: Any, key: str, default: Any = None) -> Any:
    """Accept SDK objects and HTTP dictionaries without storing either wholesale."""
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def token_count(item: Any, key: str) -> int | None:
    value = value_of(item, key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def finite_timeout(value: float | None, environment: str, default: float = 180.0) -> float:
    try:
        result = float(os.getenv(environment, str(default)) if value is None else value)
    except (ValueError, TypeError) as exc:
        raise ConnectorConfigurationError(f"{environment} must be numeric.") from exc
    if not math.isfinite(result) or result <= 0:
        raise ConnectorConfigurationError(f"{environment} must be positive and finite.")
    return result


def request_budget(provider: str, model: str, system_prompt: str, user_prompt: str,
                   max_output_tokens: int | None, context_window: int | None):
    from pipeline.model_profiles import get_model_profile
    from pipeline.token_budget import TokenBudget
    overrides = {}
    if context_window is not None:
        overrides["context_window"] = context_window
        overrides["safety_margin"] = min(512, max(0, context_window // 10))
    if max_output_tokens is not None:
        overrides["max_output_tokens"] = max_output_tokens
    profile = get_model_profile(provider, model, overrides)
    return profile, TokenBudget(profile).output_allowance(system_prompt, user_prompt, max_output_tokens)


def request_temperature(value: float | None) -> float | None:
    if value is not None and (not math.isfinite(value) or not 0 <= value <= 2):
        raise ConnectorConfigurationError("temperature must be finite and between zero and two.")
    return value


def checked_response(response: GenerationResponse, provider: str) -> GenerationResponse:
    if not isinstance(response.text, str):
        raise ProviderError(f"{provider} returned a non-text response.")
    if not response.text.strip() and not response.truncated:
        raise ProviderError(f"{provider} returned an empty model response.")
    return response


def provider_failure(provider: str, exc: Exception) -> ProviderError:
    """Do not echo SDK errors: they can include credentials, URLs or prompt bodies."""
    if isinstance(exc, ProviderError):
        return exc
    status = getattr(exc, "status_code", None)
    suffix = f" (HTTP {status})" if isinstance(status, int) else ""
    error = ProviderError(f"{provider} request failed{suffix}: {type(exc).__name__}.")
    if isinstance(status, int):
        error.status_code = status
    return error
