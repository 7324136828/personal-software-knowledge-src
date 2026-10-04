"""Opt-in CLI diagnostics isolated from unrelated application threads."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class _Session:
    directory: Path
    sequence: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


_ACTIVE: ContextVar[_Session | None] = ContextVar("content_generator_diagnostics", default=None)
_LOGGER = logging.getLogger("content_generator")
_LOGGER_LOCK = threading.Lock()
_LOGGER_USERS = 0
_ORIGINAL_LEVEL = _LOGGER.level
_PRIVATE_KEYS = {"headers", "auth", "authentication", "authorization", "proxy_authorization", "api_key", "apikey",
                 "access_token", "refresh_token", "bearer_token", "password", "secret"}


def _without_credentials(value):
    if isinstance(value, dict):
        return {key: _without_credentials(item) for key, item in value.items()
                if str(key).lower().replace("-", "_") not in _PRIVATE_KEYS
                and not str(key).lower().replace("-", "_").endswith("_api_key")}
    if isinstance(value, (list, tuple)):
        return [_without_credentials(item) for item in value]
    return value


class _SessionFilter(logging.Filter):
    def __init__(self, session: _Session):
        super().__init__()
        self.session = session

    def filter(self, record: logging.LogRecord) -> bool:
        return _ACTIVE.get() is self.session


@contextmanager
def verbose_logging(enabled: bool):
    """Yield a diagnostic run folder when enabled, restoring logging afterward."""
    global _LOGGER_USERS, _ORIGINAL_LEVEL
    if not enabled:
        token = _ACTIVE.set(None)
        try:
            yield None
        finally:
            _ACTIVE.reset(token)
        return
    root = Path(os.environ.get("TEMP") or tempfile.gettempdir()) / "personal-software-knowledge-src-log"
    directory = root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    session = _Session(directory)
    handler = logging.FileHandler(directory / "run.log", encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_SessionFilter(session))
    with _LOGGER_LOCK:
        if _LOGGER_USERS == 0:
            _ORIGINAL_LEVEL = _LOGGER.level
        _LOGGER_USERS += 1
        _LOGGER.setLevel(logging.DEBUG)
        _LOGGER.addHandler(handler)
    token = _ACTIVE.set(session)
    try:
        _LOGGER.info("Verbose diagnostics: %s", directory)
        yield directory
    finally:
        _LOGGER.info("Diagnostic logging finished.")
        _ACTIVE.reset(token)
        with _LOGGER_LOCK:
            _LOGGER.removeHandler(handler)
            _LOGGER_USERS -= 1
            if _LOGGER_USERS == 0:
                _LOGGER.setLevel(_ORIGINAL_LEVEL)
        handler.close()


def record_exchange(kind: str, payload: dict | str) -> None:
    """Immediately persist one request, response or diagnostic event if enabled."""
    session = _ACTIVE.get()
    if session is None:
        return
    safe_kind = re.sub(r"[^a-zA-Z0-9_-]", "_", kind)[:80] or "event"
    category = "requests" if "request" in kind else "responses" if "response" in kind else "events"
    with session.lock:
        session.sequence += 1
        directory = session.directory / category
        directory.mkdir(exist_ok=True)
        path = directory / f"{session.sequence:06d}_{safe_kind}.json"
        event = {
            "sequence": session.sequence,
            "at": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "payload": _without_credentials(payload),
        }
        path.write_text(json.dumps(event, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    _LOGGER.debug("Saved %s: %s", kind, path.relative_to(session.directory))
