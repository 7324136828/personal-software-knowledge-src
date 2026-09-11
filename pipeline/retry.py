"""Provider recovery policy shared by every pipeline request."""
from __future__ import annotations

import time
from collections.abc import Callable

from errors import GenerationCancelled, ProviderError


class GenerationFailure(ProviderError):
    def __init__(
        self, message: str, *, truncated: bool = False, payload_rejected: bool = False
    ):
        super().__init__(message)
        self.truncated = truncated
        self.payload_rejected = payload_rejected


def retryable(error: Exception) -> bool:
    status = getattr(error, "status_code", None)
    message = str(error).lower()
    if status in (401, 403) or any(x in message for x in (
        "invalid api key", "authentication", "unauthorized", "permission denied")):
        return False
    # ProviderError is the connector-neutral wrapper for an HTTP/API failure.  This
    # deliberately includes 400 and 404: deployments and model routes can be
    # temporarily inconsistent while rolling out. Authentication/authorization
    # failures above remain terminal because waiting cannot repair credentials.
    return isinstance(error, ProviderError) or status in (400, 404, 408, 409, 422, 429, 500, 502, 503, 504) or any(x in message for x in (
        "timeout", "timed out", "rate limit", "429", "temporar", "connection", "overloaded"))


def budget_rejection(error: Exception) -> bool:
    return any(x in str(error).lower() for x in (
        "context", "token limit", "too many tokens", "maximum tokens", "too long", "budget"))


def payload_rejection(error: Exception) -> bool:
    """Whether a provider rejected the submitted request payload itself."""

    return getattr(error, "status_code", None) in (400, 413, 422)


def backoff_delay(attempt: int, maximum: float = 240.0) -> float:
    """Return 1s, 2s, 4s, ... capped at four minutes."""

    # The cap is reached at attempt 8, so bounding the exponent also avoids
    # constructing enormous integers during a very long outage.
    return min(float(2 ** min(max(0, attempt), 8)), maximum)


def backoff(
    attempt: int,
    maximum: float = 240.0,
    cancel_check: Callable[[], bool] | None = None,
) -> float:
    """Sleep for the capped exponential delay and return it for observability."""

    delay = backoff_delay(attempt, maximum)
    if cancel_check is None:
        time.sleep(delay)
        return delay
    deadline = time.monotonic() + delay
    while True:
        if cancel_check():
            raise GenerationCancelled("Conversion was discarded by the user.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.25, remaining))
    return delay
