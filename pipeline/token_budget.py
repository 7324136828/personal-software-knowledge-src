"""Dependency-free conservative text estimates and request budget enforcement."""

from __future__ import annotations

import math

from errors import ProviderError
from .model_profiles import ModelProfile


class TokenBudgetError(ProviderError):
    """The request cannot fit its configured model budget."""


def estimate_tokens(text: str, model: str | None = None) -> int:
    """Estimate text tokens conservatively without downloading a tokenizer.

    ASCII receives one token per two characters; non-ASCII receives one per UTF-8
    byte. This intentionally overestimates ordinary prose and multilingual text.
    It is a heuristic, not a model tokenizer: safety margins remain necessary.
    ``model`` is accepted so a future tokenizer can be introduced compatibly.
    """
    del model
    if not text:
        return 0
    ascii_count = sum(character.isascii() for character in text)
    non_ascii_bytes = len(text.encode("utf-8")) - ascii_count
    return math.ceil(ascii_count / 2) + non_ascii_bytes


class TokenBudget:
    # Covers chat roles, separators and provider serialization outside text.
    message_overhead = 32

    def __init__(self, profile: ModelProfile) -> None:
        self.profile = profile

    def _requested(self, requested: int | None) -> int:
        value = self.profile.reserved_output_tokens if requested is None else requested
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise TokenBudgetError("Requested output tokens must be a positive integer.")
        return min(value, self.profile.max_output_tokens)

    def input_tokens(self, system_prompt: str, user_prompt: str) -> int:
        return estimate_tokens(system_prompt) + estimate_tokens(user_prompt) + self.message_overhead

    def output_allowance(self, system_prompt: str, user_prompt: str, requested: int | None = None) -> int:
        requested = self._requested(requested)
        input_tokens = self.input_tokens(system_prompt, user_prompt)
        if (self.profile.max_input_tokens is not None
                and input_tokens > self.profile.max_input_tokens - self.profile.safety_margin):
            raise TokenBudgetError("Prompt exceeds the configured input token budget; reduce the chunk or prompt size.")
        remaining = self.profile.context_window - self.profile.safety_margin - input_tokens
        if remaining < 1:
            raise TokenBudgetError("Prompt exceeds the configured context budget; reduce the chunk or prompt size.")
        return min(requested, remaining)

    def material_allowance(
        self, system_prompt: str, user_prompt: str,
        requested_output: int | None = None, cushion: int = 0,
    ) -> int:
        """Return space for additional source material under every token limit.

        A separate input cap includes prompts and chat framing but excludes
        output. A combined context cap must reserve space for both. A cushion
        keeps room for later instructions and validation feedback.
        """
        if not isinstance(cushion, int) or isinstance(cushion, bool) or cushion < 0:
            raise TokenBudgetError("Token cushion must be a nonnegative integer.")
        output = self._requested(requested_output)
        overhead = self.input_tokens(system_prompt, user_prompt)
        remaining = self.profile.context_window - self.profile.safety_margin - overhead - output - cushion
        if self.profile.max_input_tokens is not None:
            remaining = min(remaining, self.profile.max_input_tokens - self.profile.safety_margin - overhead - cushion)
        return remaining

    def chunk_allowance(self, system_prompt: str, extra_prompt: str = "", requested_output: int | None = None) -> int:
        overhead = self.input_tokens(system_prompt, extra_prompt)
        usable = self.profile.context_window - self.profile.safety_margin
        remaining = min(self.material_allowance(system_prompt, extra_prompt, requested_output),
                        int(usable * self.profile.preferred_input_ratio) - overhead)
        if remaining < 1:
            raise TokenBudgetError("Skill and instruction prompts leave no source budget; increase the verified context limit or simplify the skill.")
        return min(self.profile.recommended_chunk_tokens, remaining)
