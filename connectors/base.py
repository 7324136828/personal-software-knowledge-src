"""Provider-neutral connector contract."""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMConnector(ABC):
    """Common interface implemented by every language-model provider."""

    model: str

    @abstractmethod
    def generate(self, *, system_prompt: str, user_prompt: str) -> str:
        """Generate and return text for a pair of system and user prompts."""

        raise NotImplementedError
