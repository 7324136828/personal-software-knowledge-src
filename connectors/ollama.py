"""Ollama local HTTP connector."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from errors import ProviderError

from .base import (
    GenerationResponse,
    LLMConnector,
    checked_response,
    finite_timeout,
    request_budget,
    request_temperature,
    token_count,
)


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
        self.timeout = finite_timeout(self.timeout, "OLLAMA_TIMEOUT", 600.0)

    def generate(self, *, system_prompt: str, user_prompt: str) -> str:
        return self.generate_response(
            system_prompt=system_prompt, user_prompt=user_prompt
        ).text

    def generate_response(
        self, *, system_prompt: str, user_prompt: str,
        max_output_tokens: int | None = None, temperature: float | None = None,
        json_mode: bool = False, context_window: int | None = None,
    ) -> GenerationResponse:
        profile, allowance = request_budget(
            "ollama", self.model, system_prompt, user_prompt,
            max_output_tokens, context_window,
        )
        options: dict[str, object] = {
            "num_ctx": context_window or profile.context_window,
            "num_predict": allowance,
        }
        temperature = request_temperature(temperature)
        if temperature is not None:
            options["temperature"] = temperature
        request_body: dict[str, object] = {
            "model": self.model,
            "stream": False,
            "options": options,
            "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
        }
        if json_mode:
            request_body["format"] = "json"
        payload = json.dumps(request_body).encode("utf-8")
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
            error = ProviderError(f"Ollama request failed (HTTP {exc.code}).")
            error.status_code = exc.code
            raise error from exc
        except URLError as exc:
            raise ProviderError(
                f"Could not connect to Ollama at {self.base_url}: {exc.reason}"
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderError(f"Invalid response from Ollama: {exc}") from exc

        if response_data.get("error"):
            raise ProviderError("Ollama returned an error response.")
        result = response_data.get("message", {}).get("content")
        generated = GenerationResponse(
            text=result,
            finish_reason=response_data.get("done_reason") or (None if response_data.get("done") else "incomplete"),
            input_tokens=token_count(response_data, "prompt_eval_count"),
            output_tokens=token_count(response_data, "eval_count"),
            raw_metadata={"total_duration": response_data.get("total_duration"),
                          "load_duration": response_data.get("load_duration")},
        )
        return checked_response(generated, "Ollama")
