"""Content-addressed, atomic stage checkpoints and append-only request evidence."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     default=str).encode("utf-8")).hexdigest()


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class Checkpoint:
    def __init__(self, path: Path, *, enabled: bool = True, force: bool = False):
        self.path, self.enabled, self.force = path, enabled, force
        self.state = {"version": 1, "stages": {}}
        if enabled and path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if loaded.get("version") == 1 and isinstance(loaded.get("stages"), dict):
                    self.state = loaded
            except (ValueError, OSError, AttributeError):
                # Never trust a broken checkpoint; keep it for diagnosis.
                atomic_text(path.with_name(path.name + ".corrupt." + uuid.uuid4().hex),
                            path.read_text(encoding="utf-8", errors="replace"))

    def get(self, key: str, fingerprint: str) -> Any | None:
        if not self.enabled or self.force:
            return None
        record = self.state["stages"].get(key, {})
        if record.get("status") != "complete" or record.get("fingerprint") != fingerprint:
            return None
        try:
            path = Path(record["path"])
            payload = json.loads(path.read_text(encoding="utf-8"))
            if digest(payload) == record["checksum"]:
                return payload
        except (OSError, ValueError, KeyError):
            pass
        return None

    def save(self, key: str, fingerprint: str, path: Path, payload: Any) -> None:
        atomic_json(path, payload)
        self._record(key, {"status": "complete", "fingerprint": fingerprint,
                           "path": str(path.resolve()), "checksum": digest(payload)})

    def fail(self, key: str, fingerprint: str, error: str) -> None:
        self._record(key, {"status": "failed", "fingerprint": fingerprint, "error": error})

    def _record(self, key: str, record: dict) -> None:
        if self.enabled:
            record["updated_at"] = datetime.now(timezone.utc).isoformat()
            self.state["stages"][key] = record
            atomic_json(self.path, self.state)


def evidence_prefix(directory: Path, stage: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory / (stage.replace("/", "_").replace(":", "_") + "_" +
                        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") +
                        "_" + uuid.uuid4().hex[:8])
