"""Ollama local HTTP connector."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from errors import ConnectorConfigurationError, ProviderError

from .base import LLMConnector


@dataclass
class OllamaConnector(LLMConnector):
    """Generate text through an Ollama server's local chat endpoint."""

    model: str | None = None
    base_url: str | None = None
    timeout: float | None = None

    def __post_init__(self) -> None:
        self.model = self.model or os.getenv("OLLAMA_MODEL", "llama3.2")
        self.base_url = (self.base_url or os.getenv(
            "OLLAMA_BASE_URL", "http://localhost:11434"
        )).rstrip("/")
        if self.timeout is None:
            raw_timeout = os.getenv("OLLAMA_TIMEOUT", "600")
            try:
                self.timeout = float(raw_timeout)
            except ValueError as exc:
                raise ConnectorConfigurationError("OLLAMA_TIMEOUT must be numeric.") from exc

    def generate(self, *, system_prompt: str, user_prompt: str) -> str:
        payload = json.dumps(
            {
                "model": self.model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
        ).encode("utf-8")
        request = Request(
            f"{self.base_url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                response_data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ProviderError(f"Ollama returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise ProviderError(
                f"Could not connect to Ollama at {self.base_url}: {exc.reason}"
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderError(f"Invalid response from Ollama: {exc}") from exc

        if error_message := response_data.get("error"):
            raise ProviderError(f"Ollama request failed: {error_message}")
        result = response_data.get("message", {}).get("content")
        if not isinstance(result, str) or not result.strip():
            raise ProviderError("Ollama returned an empty model response.")
        return result
