"""The Connector's local, OpenAI-compatible model and completion API."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import quote, urlsplit

from diagnostic_logging import record_exchange
from errors import ConnectorConfigurationError, ProviderError
from pipeline.model_profiles import ModelProfile, get_model_profile
from pipeline.token_budget import TokenBudget

from .base import (
    GenerationResponse, LLMConnector, checked_response, finite_timeout,
    request_temperature, token_count,
)


LOGGER = logging.getLogger("content_generator.the_connector")


def _positive_integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def connector_base_url(base_url: str | None = None) -> str:
    url = (base_url or os.getenv(
        "THE_CONNECTOR_BASE_URL", "http://127.0.0.1:8301/v1"
    )).rstrip("/")
    return url if url.endswith("/v1") else f"{url}/v1"


def _request(base_url: str, path: str, timeout: float,
             body: dict[str, Any] | None = None, *,
             log_response_body: bool = True, allow_list: bool = False) -> dict[str, Any] | list:
    request = Request(
        f"{base_url}/{path}",
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    destination = urlsplit(request.full_url)
    hostname = destination.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    try:
        port = destination.port
    except ValueError:
        port = None
    origin = f"{destination.scheme}://{hostname}" + (f":{port}" if port is not None else "")
    exchange = {"connector": "the_connector", "method": request.get_method(),
                "origin": origin, "path": destination.path}
    record_exchange("connector_request", {**exchange, "timeout_seconds": timeout, "body": body})
    started = time.monotonic()
    response_text = None
    try:
        with urlopen(request, timeout=timeout) as response:
            response_text = response.read().decode("utf-8")
            data = json.loads(response_text)
            record_exchange("connector_response", {
                **exchange, "status": getattr(response, "status", None),
                **({"body": data} if log_response_body else {}),
                "elapsed_seconds": time.monotonic() - started,
            })
    except HTTPError as exc:
        try:
            error_text = exc.read().decode("utf-8", errors="replace")
            try:
                error_body = json.loads(error_text)
            except ValueError:
                error_body = error_text
        except OSError:
            error_body = None
        record_exchange("connector_response", {**exchange, "status": exc.code,
                                               **({"body": error_body} if log_response_body else {}),
                                                "elapsed_seconds": time.monotonic() - started})
        record_exchange("connector_failure", {**exchange, "status": exc.code, "error_type": type(exc).__name__,
                                               "elapsed_seconds": time.monotonic() - started})
        error = ProviderError(f"The Connector request failed (HTTP {exc.code}). Check its active configuration and routing settings.")
        error.status_code = exc.code
        exc.close()
        raise error from exc
    except (URLError, OSError) as exc:
        record_exchange("connector_failure", {**exchange, "error_type": type(exc).__name__,
                                               "elapsed_seconds": time.monotonic() - started})
        raise ProviderError("Could not reach The Connector. Start its backend and check THE_CONNECTOR_BASE_URL.") from exc
    except (ValueError, UnicodeError) as exc:
        record_exchange("connector_failure", {**exchange, "error_type": type(exc).__name__,
                                               **({"response_text": response_text} if log_response_body else {}),
                                               "elapsed_seconds": time.monotonic() - started})
        raise ProviderError("The Connector returned invalid JSON.") from exc
    if allow_list and isinstance(data, list):
        return data
    if not isinstance(data, dict) or data.get("error"):
        raise ProviderError("The Connector returned an invalid or error response.")
    return data


def discover_models(base_url: str | None = None, timeout: float | None = None) -> list[dict[str, Any]]:
    """Discover active library IDs, never the underlying provider catalog."""
    data = _request(connector_base_url(base_url), "models",
                    min(finite_timeout(timeout, "THE_CONNECTOR_TIMEOUT", 600.0), 10.0))
    models = data.get("data")
    if not isinstance(models, list) or any(
        not isinstance(model, dict) or not isinstance(model.get("id"), str)
        or not model["id"].strip() for model in models
    ):
        raise ProviderError("The Connector returned an invalid model list.")
    return models


@dataclass
class TheConnector(LLMConnector):
    model: str | None = None
    base_url: str | None = None
    timeout: float | None = None
    _catalog: list[dict[str, Any]] | None = field(default=None, init=False, repr=False)
    _limits: dict[str, int] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.base_url = connector_base_url(self.base_url)
        self.timeout = finite_timeout(self.timeout, "THE_CONNECTOR_TIMEOUT", 600.0)
        self.model = self.model or os.getenv("THE_CONNECTOR_MODEL")
        if not self.model:
            models = discover_models(self.base_url, self.timeout)
            self._catalog = models
            if not models:
                raise ConnectorConfigurationError(
                    "The Connector has no active models. Save and activate a library configuration first."
                )
            self.model = models[0]["id"]

    def _configuration_limits(self) -> dict[str, int]:
        """Read only budgets for the selected library route, once per instance."""

        if self._limits is not None:
            return self._limits
        self._limits = {}
        timeout = min(self.timeout, 10.0)
        if self._catalog is None:
            try:
                self._catalog = discover_models(self.base_url, timeout)
            except ProviderError:
                self._catalog = []
        selected = next((item for item in self._catalog if item.get("id") == self.model), {})
        name = selected.get("name")
        if not isinstance(name, str) or not name.strip():
            name = self.model
        # The configuration API lives beside /v1, including its /api/v1 alias.
        root = self.base_url
        if root.endswith("/api/v1"):
            root = root[:-7]
        elif root.endswith("/v1"):
            root = root[:-3]
        configuration = {}
        try:
            configuration = _request(root, "api/configuration/detail/" + quote(name, safe=""),
                                     timeout, log_response_body=False)
        except ProviderError as exc:
            if getattr(exc, "status_code", None) in {404, 409}:
                # Older servers and duplicate display names can still resolve
                # the exact stable model ID through the configuration library.
                try:
                    records = _request(root, "api/configs", timeout,
                                       log_response_body=False, allow_list=True)
                    if isinstance(records, list):
                        matches = [item for item in records if isinstance(item, dict)
                                   and item.get("model_id") == self.model]
                        if len(matches) == 1 and isinstance(matches[0].get("config"), dict):
                            configuration = matches[0]["config"]
                except ProviderError:
                    pass
            LOGGER.debug("Configuration token-limit lookup unavailable (%s); using available limits or local defaults.",
                         getattr(exc, "status_code", type(exc).__name__))
        for name in ("gross_max_input_token", "gross_max_output_token"):
            if value := _positive_integer(configuration.get(name)):
                self._limits[name] = value
        if capacity := _positive_integer(selected.get("context_length")):
            self._limits["context_length"] = capacity
        record_exchange("connector_model_limits", {"connector": "the_connector", "model": self.model,
                                                    "limits": self._limits})
        return self._limits

    def get_model_profile(self, overrides: dict | None = None) -> ModelProfile:
        """Combine remote request caps with local fallback operating budgets."""

        overrides = overrides or {}
        profile = get_model_profile("the_connector", self.model, overrides)
        limits = self._configuration_limits()
        input_cap = limits.get("gross_max_input_token")
        output_cap = limits.get("gross_max_output_token")
        if output_cap is not None:
            if overrides.get("max_output_tokens") is not None:
                output_cap = min(output_cap, profile.max_output_tokens)
            reserved = (profile.reserved_output_tokens if overrides.get("reserved_output_tokens") is not None or input_cap is None
                        else output_cap)
            profile = replace(profile, max_output_tokens=output_cap,
                              reserved_output_tokens=min(reserved, output_cap))
        if input_cap is not None:
            # Gross caps describe separate request allowances, not model capacity.
            # Use their sum as an operating budget and honor advertised capacity.
            context = input_cap + limits.get("gross_max_output_token", profile.reserved_output_tokens)
            if "context_length" in limits:
                context = min(context, limits["context_length"])
            profile = replace(profile, context_window=context, max_input_tokens=input_cap,
                              safety_margin=min(profile.safety_margin, max(0, context // 10)))
        LOGGER.debug("Resolved token budgets: context_window=%s max_input_tokens=%s max_output_tokens=%s",
                     profile.context_window, profile.max_input_tokens, profile.max_output_tokens)
        return profile

    def generate(self, *, system_prompt: str, user_prompt: str) -> str:
        return self.generate_response(system_prompt=system_prompt, user_prompt=user_prompt).text

    def generate_response(
        self, *, system_prompt: str, user_prompt: str,
        max_output_tokens: int | None = None, temperature: float | None = None,
        json_mode: bool = False, context_window: int | None = None,
    ) -> GenerationResponse:
        overrides = {}
        if context_window is not None:
            overrides["context_window"] = context_window
            overrides["safety_margin"] = min(512, max(0, context_window // 10))
        if max_output_tokens is not None:
            overrides["max_output_tokens"] = max_output_tokens
        profile = self.get_model_profile(overrides)
        allowance = TokenBudget(profile).output_allowance(system_prompt, user_prompt, max_output_tokens)
        body: dict[str, Any] = {
            "model": self.model, "stream": False, "max_tokens": allowance,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        temperature = request_temperature(temperature)
        if temperature is not None:
            body["temperature"] = temperature
        # Saved routes can use Claude or reasoning models; keep optional settings
        # absent unless explicitly supported by the operating profile.
        if json_mode and profile.structured_output:
            body["response_format"] = {"type": "json_object"}
        data = _request(self.base_url, "chat/completions", self.timeout, body)
        try:
            choice = data["choices"][0]
            message = choice["message"]
            usage = data.get("usage") or {}
            result = GenerationResponse(
                text=message.get("content"), finish_reason=choice.get("finish_reason"),
                input_tokens=token_count(usage, "prompt_tokens"),
                output_tokens=token_count(usage, "completion_tokens"),
                raw_metadata={"response_id": data.get("id")},
            )
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ProviderError("The Connector returned an invalid completion response.") from exc
        return checked_response(result, "The Connector")
