"""Conservative, configurable *operating* budgets, not provider capacity claims.

Known families deliberately use much less than their advertised context. Unknown
model identifiers are preserved and receive a small safe starting budget. Supply
profile overrides for a deployment's verified limits and pricing.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
import math

from errors import ConnectorConfigurationError


@dataclass(frozen=True)
class ModelProfile:
    context_window: int = 8192
    max_output_tokens: int = 2048
    recommended_chunk_tokens: int = 2400
    preferred_input_ratio: float = 0.55
    reserved_output_tokens: int = 2048
    safety_margin: int = 512
    structured_output: bool = False
    temperature: float | None = None
    retries: int = 2
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None

    def __post_init__(self) -> None:
        for name in ("context_window", "max_output_tokens", "recommended_chunk_tokens", "reserved_output_tokens"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ConnectorConfigurationError(f"Model profile {name} must be a positive integer.")
        for name in ("safety_margin", "retries"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ConnectorConfigurationError(f"Model profile {name} must be a nonnegative integer.")
        if not 0 < self.preferred_input_ratio < 1:
            raise ConnectorConfigurationError("preferred_input_ratio must be between zero and one.")
        if self.safety_margin >= self.context_window:
            raise ConnectorConfigurationError("safety_margin must be smaller than context_window.")
        if self.temperature is not None and (not math.isfinite(self.temperature) or not 0 <= self.temperature <= 2):
            raise ConnectorConfigurationError("temperature must be finite and between zero and two.")
        for name in ("input_cost_per_million", "output_cost_per_million"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ConnectorConfigurationError(f"{name} must be finite and nonnegative.")
        if not isinstance(self.structured_output, bool):
            raise ConnectorConfigurationError("structured_output must be a boolean.")


def get_model_profile(connector: str, model: str, overrides: dict | None = None) -> ModelProfile:
    """Resolve a family operating profile and apply explicit deployment overrides."""
    provider = connector.lower().strip()
    model_name = (model or "").lower().strip()
    if provider == "claude":
        provider = "anthropic"
    if provider == "openrouter" and "/" in model_name:
        provider, model_name = model_name.split("/", 1)
    profile = ModelProfile()
    if provider == "ollama":
        # Passed as num_ctx on every request; independent of server defaults.
        profile = ModelProfile(context_window=8192, max_output_tokens=2048,
                               recommended_chunk_tokens=1600, reserved_output_tokens=2048,
                               structured_output=True, temperature=0.0)
    elif provider == "openai" and model_name.startswith(("gpt-4.1", "gpt-4o")):
        profile = ModelProfile(context_window=128000, max_output_tokens=8192,
                               recommended_chunk_tokens=8000, reserved_output_tokens=8192,
                               safety_margin=4000, structured_output=True, temperature=0.2)
    elif provider == "openai" and model_name.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4")):
        profile = ModelProfile(context_window=128000, max_output_tokens=16000,
                               recommended_chunk_tokens=8000, reserved_output_tokens=12000,
                               safety_margin=4000, structured_output=True, temperature=None)
    elif provider == "anthropic" and model_name.startswith("claude-"):
        # Prompt-only JSON: generic JSON mode cannot provide Anthropic's required
        # concrete strict schema, and older Claude models lack output_config.
        profile = ModelProfile(context_window=32000, max_output_tokens=8192,
                               recommended_chunk_tokens=6000, reserved_output_tokens=8192,
                               safety_margin=2000, structured_output=False, temperature=None)
    if overrides:
        allowed = {field.name for field in fields(ModelProfile)}
        unknown = set(overrides) - allowed
        if unknown:
            raise ConnectorConfigurationError("Unknown model profile fields: " + ", ".join(sorted(unknown)))
        profile = replace(profile, **{key: value for key, value in overrides.items() if value is not None})
    return profile
