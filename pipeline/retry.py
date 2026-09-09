"""Bounded recovery policy; no unbounded SDK or application retry loops."""
from __future__ import annotations

import time
from errors import ProviderError


class GenerationFailure(ProviderError):
    def __init__(self, message: str, *, truncated: bool = False):
        super().__init__(message)
        self.truncated = truncated


def retryable(error: Exception) -> bool:
    status = getattr(error, "status_code", None)
    message = str(error).lower()
    if status in (401, 403, 404) or any(x in message for x in (
        "invalid api key", "authentication", "unauthorized", "permission denied", "model not found")):
        return False
    return status in (408, 409, 429, 500, 502, 503, 504) or any(x in message for x in (
        "timeout", "timed out", "rate limit", "429", "temporar", "connection", "overloaded"))


def budget_rejection(error: Exception) -> bool:
    return any(x in str(error).lower() for x in (
        "context", "token limit", "too many tokens", "maximum tokens", "too long", "budget"))


def backoff(attempt: int) -> None:
    time.sleep(min(0.5 * 2 ** attempt, 8.0))
