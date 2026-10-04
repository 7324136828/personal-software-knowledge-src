#!/usr/bin/env python3
"""FastAPI backend server for skill-driven content generation.

Provides REST endpoints to upload/paste documents (PDF, DOCX, TXT, MD, etc.),
run content generation actions with persistent history, resume saved work, and
retrieve the resulting output artifacts.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.background import BackgroundTask

from app_config import ACTION_CONFIG
from cli_runtime import execute_generation
from connectors import CONNECTORS
from document_loader import SUPPORTED_EXTENSIONS
from errors import ApplicationError, GenerationCancelled
from pipeline.engine import PipelineOptions

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
LOGGER = logging.getLogger("server")

app = FastAPI(
    title="Personal Software Knowledge Content Generator API",
    description="Backend API for converting documents into structured learning artifacts using LLM skills.",
    version="1.0.0",
)

# Enable CORS for frontend development server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ROOT_DIR = Path(__file__).resolve().parent
HISTORY_DIR = ROOT_DIR / "conversion_history"
ACTIVE_CONVERSIONS: set[str] = set()
CANCEL_EVENTS: dict[str, threading.Event] = {}
DISPATCHED_CONVERSIONS: set[str] = set()
QUEUED_API_KEYS: dict[str, str] = {}
HISTORY_LOCK = threading.RLock()
QUEUE_WAKE = threading.Event()
QUEUE_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="conversion")
QUEUE_DISPATCHER_STARTED = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conversion_dir(conversion_id: str) -> Path:
    """Return a contained conversion directory for a UUID-hex identifier."""

    if len(conversion_id) != 32 or any(char not in "0123456789abcdef" for char in conversion_id):
        raise HTTPException(status_code=404, detail="Conversion was not found.")
    return HISTORY_DIR / conversion_id


def _metadata_path(conversion_id: str) -> Path:
    return _conversion_dir(conversion_id) / "conversion.json"


def _write_record(record: dict[str, Any]) -> None:
    """Atomically persist conversion metadata without ever storing credentials."""

    conversion_dir = _conversion_dir(record["id"])
    conversion_dir.mkdir(parents=True, exist_ok=True)
    destination = conversion_dir / "conversion.json"
    temporary = destination.with_suffix(f".json.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, destination)


def _append_log(record: dict[str, Any], message: str) -> None:
    entries = list(record.get("log") or [])
    entries.append({"at": _now(), "message": message})
    record["log"] = entries[-50:]


def _read_record(conversion_id: str) -> dict[str, Any]:
    path = _metadata_path(conversion_id)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=404, detail="Conversion was not found.") from exc
    if not isinstance(record, dict) or record.get("id") != conversion_id:
        raise HTTPException(status_code=404, detail="Conversion was not found.")
    return record


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    conversion_id = record["id"]
    status = record.get("status", "failed")
    with HISTORY_LOCK:
        active = conversion_id in ACTIVE_CONVERSIONS or conversion_id in DISPATCHED_CONVERSIONS
    return {
        "id": conversion_id,
        "status": status,
        "active": active,
        "can_continue": status in {"in_progress", "failed"} and not active,
        "can_discard": status != "discarding",
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "completed_at": record.get("completed_at"),
        "input_filename": record.get("input_filename"),
        "action": record.get("action"),
        "action_title": record.get("action_title"),
        "connector": record.get("connector"),
        "model": record.get("model"),
        "output_format": record.get("output_format"),
        "output_filename": record.get("output_filename"),
        "metrics": record.get("metrics"),
        "error": record.get("error"),
        "api_key_was_supplied": bool(record.get("api_key_was_supplied")),
        "source_group_id": record.get("source_group_id") or f"legacy:{record.get('input_filename', conversion_id)}",
        "priority": record.get("priority", 0),
        "log": record.get("log") or [],
        "run_attempts": record.get("run_attempts", 0),
        "retry_count": (record.get("metrics") or {}).get("retry_count", 0),
        "repair_count": (record.get("metrics") or {}).get("repair_count", 0),
        "download_url": (
            f"/api/history/{conversion_id}/download" if status == "completed" else None
        ),
    }


def _records_on_disk() -> list[dict[str, Any]]:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    records = []
    for directory in HISTORY_DIR.iterdir():
        if directory.is_dir():
            try:
                records.append(_read_record(directory.name))
            except HTTPException:
                continue
    return records


def _run_queued(conversion_id: str) -> None:
    try:
        api_key = QUEUED_API_KEYS.pop(conversion_id, None)
        _run_conversion(conversion_id, api_key)
    except Exception as exc:
        LOGGER.warning("Queued conversion %s stopped: %s", conversion_id, type(exc).__name__)
    finally:
        with HISTORY_LOCK:
            DISPATCHED_CONVERSIONS.discard(conversion_id)
        QUEUE_WAKE.set()


def _queue_dispatch_loop() -> None:
    while True:
        QUEUE_WAKE.wait(timeout=1.0)
        QUEUE_WAKE.clear()
        with HISTORY_LOCK:
            capacity = 3 - len(ACTIVE_CONVERSIONS) - len(DISPATCHED_CONVERSIONS)
            if capacity <= 0:
                continue
            queued = [
                record for record in _records_on_disk()
                if record.get("status") == "queued"
                and record["id"] not in ACTIVE_CONVERSIONS
                and record["id"] not in DISPATCHED_CONVERSIONS
            ]
            queued.sort(key=lambda record: (record.get("priority", 0), record.get("created_at", "")))
            selected = queued[:capacity]
            for record in selected:
                DISPATCHED_CONVERSIONS.add(record["id"])
        for record in selected:
            QUEUE_EXECUTOR.submit(_run_queued, record["id"])


def _ensure_queue_dispatcher() -> None:
    global QUEUE_DISPATCHER_STARTED
    with HISTORY_LOCK:
        if not QUEUE_DISPATCHER_STARTED:
            threading.Thread(target=_queue_dispatch_loop, daemon=True, name="queue-dispatcher").start()
            QUEUE_DISPATCHER_STARTED = True
    QUEUE_WAKE.set()

ACTION_METADATA = {
    "create_datatables": {
        "title": "Data Tables",
        "description": "Extract structured data tables, metrics, and comparisons.",
        "default_ext": "json",
        "supported_exts": ["json", "csv", "txt"],
    },
    "create_flashcards": {
        "title": "Flashcards",
        "description": "Create study flashcard decks with front/back cues.",
        "default_ext": "json",
        "supported_exts": ["json", "txt"],
    },
    "create_infographics": {
        "title": "Infographics",
        "description": "Design infographic specs, visual breakdowns, and hierarchies.",
        "default_ext": "json",
        "supported_exts": ["json", "txt"],
    },
    "create_mindmaps": {
        "title": "Mind Maps",
        "description": "Generate concept hierarchies in Markdown or Mermaid format.",
        "default_ext": "mmd",
        "supported_exts": ["mmd", "md", "json", "txt"],
    },
    "create_podcasts": {
        "title": "Podcast Scripts",
        "description": "Generate one conversational podcast per study set, up to 20 minutes.",
        "default_ext": "txt",
        "supported_exts": ["txt", "json", "md"],
    },
    "create_qandas": {
        "title": "Q & A Pairs",
        "description": "Generate comprehensive question-and-answer pairs.",
        "default_ext": "json",
        "supported_exts": ["json", "txt"],
    },
    "create_quizzes": {
        "title": "Quizzes",
        "description": "Create multiple-choice and conceptual quizzes with explanations.",
        "default_ext": "json",
        "supported_exts": ["json", "txt"],
    },
    "create_reports": {
        "title": "Executive Reports",
        "description": "Synthesize thorough analytical reports and summaries.",
        "default_ext": "md",
        "supported_exts": ["md", "txt", "json"],
    },
    "create_slides": {
        "title": "Slide Decks",
        "description": "Generate presentation slide content with bullets and speaker notes.",
        "default_ext": "md",
        "supported_exts": ["md", "json", "txt"],
    },
}

CONNECTOR_METADATA = {
    "the_connector": {
        "label": "The Connector (Local)",
        "default_model": os.getenv("THE_CONNECTOR_MODEL", ""),
        "env_configured": True,
        "requires_key": False,
        "discover_models": True,
    },
    "openai": {
        "label": "OpenAI",
        "default_model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        "env_configured": bool(os.getenv("OPENAI_API_KEY")),
        "requires_key": True,
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "default_model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
        "env_configured": bool(os.getenv("ANTHROPIC_API_KEY")),
        "requires_key": True,
    },
    "claude": {
        "label": "Claude (Alias)",
        "default_model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
        "env_configured": bool(os.getenv("ANTHROPIC_API_KEY")),
        "requires_key": True,
    },
    "openrouter": {
        "label": "OpenRouter",
        "default_model": os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini"),
        "env_configured": bool(os.getenv("OPENROUTER_API_KEY")),
        "requires_key": True,
    },
    "ollama": {
        "label": "Ollama (Local)",
        "default_model": os.getenv("OLLAMA_MODEL", "llama3.2"),
        "env_configured": True,  # local server, no key needed
        "requires_key": False,
    },
}


@app.get("/api/health")
def health_check() -> dict[str, str]:
    """Return service health status."""
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    """Return available generation actions, connectors, and supported extensions."""
    return {
        "actions": ACTION_METADATA,
        "connectors": CONNECTOR_METADATA,
        "default_connector": "the_connector",
        "supported_extensions": sorted(list(SUPPORTED_EXTENSIONS)),
        "system_temp_dir": tempfile.gettempdir(),
        "history_dir": str(HISTORY_DIR),
    }


@app.get("/api/connectors/{connector}/models")
def get_connector_models(connector: str) -> dict[str, Any]:
    """Proxy local model discovery so browsers on the LAN reach the gateway."""
    if connector != "the_connector":
        raise HTTPException(status_code=400, detail="Model discovery is available for the_connector.")
    from connectors.the_connector import discover_models

    try:
        return {"models": discover_models(), "default_model": os.getenv("THE_CONNECTOR_MODEL", "")}
    except ApplicationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _options_from_record(record: dict[str, Any], conversion_dir: Path) -> PipelineOptions:
    values = dict(record.get("options") or {})
    values["work_dir"] = conversion_dir / "work"
    values["cancel_check"] = CANCEL_EVENTS[record["id"]].is_set
    return PipelineOptions(**values)


def _conversion_response(record: dict[str, Any]) -> dict[str, Any]:
    conversion_dir = _conversion_dir(record["id"])
    output_path = conversion_dir / record["output_path"]
    if not output_path.is_file():
        raise HTTPException(status_code=500, detail="Output file was not produced by generator.")
    return {
        "success": True,
        "session_id": record["id"],
        "output_filename": record["output_filename"],
        "download_url": f"/api/download/{record['id']}/{record['output_filename']}",
        "zip_download_url": f"/api/history/{record['id']}/download",
        "temp_folder": str(conversion_dir),
        "output_text": output_path.read_text(encoding="utf-8", errors="replace"),
        "input_filename": record["input_filename"],
        "action": record["action"],
        "action_title": record["action_title"],
        "output_format": record["output_format"],
        "metrics": record.get("metrics") or {},
    }


def _run_conversion(conversion_id: str, api_key: str | None = None) -> dict[str, Any]:
    """Run or resume a conversion using its persistent source and checkpoint."""

    conversion_dir = _conversion_dir(conversion_id)
    with HISTORY_LOCK:
        if conversion_id in ACTIVE_CONVERSIONS:
            raise HTTPException(status_code=409, detail="Conversion is already running.")
        record = _read_record(conversion_id)
        if record.get("status") not in {"queued", "in_progress", "failed"}:
            raise HTTPException(status_code=409, detail="Only unfinished conversions can be continued.")
        DISPATCHED_CONVERSIONS.discard(conversion_id)
        ACTIVE_CONVERSIONS.add(conversion_id)
        CANCEL_EVENTS[conversion_id] = threading.Event()
        record.update({
            "status": "in_progress", "error": None, "updated_at": _now(),
            "run_attempts": record.get("run_attempts", 0) + 1,
        })
        _append_log(record, f"Attempt {record['run_attempts']} started.")
        _write_record(record)

    discard_after_run = False
    try:
        result = execute_generation(
            connector_name=record["connector"],
            input_path=conversion_dir / record["input_path"],
            action=record["action"],
            output_path=conversion_dir / record["output_path"],
            model=record.get("model") or None,
            api_key=api_key.strip() if api_key and api_key.strip() else None,
            options=_options_from_record(record, conversion_dir),
        )
        with HISTORY_LOCK:
            current = _read_record(conversion_id)
            if current.get("status") == "discarding":
                discard_after_run = True
            else:
                current.update(
                    {
                        "status": "completed",
                        "updated_at": _now(),
                        "completed_at": _now(),
                        "error": None,
                        "metrics": result.metrics,
                    }
                )
                _append_log(
                    current,
                    f"Completed with {result.metrics.get('retry_count', 0)} provider retries "
                    f"and {result.metrics.get('repair_count', 0)} validation retries.",
                )
                _write_record(current)
                record = current
    except Exception as exc:
        with HISTORY_LOCK:
            try:
                current = _read_record(conversion_id)
            except HTTPException:
                current = None
            if current and current.get("status") == "discarding":
                discard_after_run = True
            elif current:
                message = str(exc) if isinstance(exc, (ApplicationError, HTTPException)) else "Unexpected conversion failure."
                metrics = getattr(exc, "pipeline_metrics", {})
                current.update({
                    "status": "failed", "updated_at": _now(), "error": message,
                    "metrics": metrics,
                })
                _append_log(
                    current,
                    f"Attempt {current.get('run_attempts', 1)} failed after "
                    f"{metrics.get('retry_count', 0)} provider retries and "
                    f"{metrics.get('repair_count', 0)} validation retries: {message}",
                )
                _write_record(current)
        raise
    finally:
        with HISTORY_LOCK:
            ACTIVE_CONVERSIONS.discard(conversion_id)
            CANCEL_EVENTS.pop(conversion_id, None)
        if discard_after_run and conversion_dir.is_dir():
            shutil.rmtree(conversion_dir)
        QUEUE_WAKE.set()

    if discard_after_run:
        raise HTTPException(status_code=410, detail="Conversion was discarded.")
    return _conversion_response(record)


@app.post("/api/convert")
async def convert_document(
    file: UploadFile | None = File(None),
    pasted_text: str | None = Form(None),
    input_filename: str | None = Form(None),
    action: str = Form(...),
    connector: str = Form(...),
    model: str | None = Form(None),
    output_format: str | None = Form(None),
    api_key: str | None = Form(None),
    strategy: str | None = Form(None),
    chunk_tokens: int | None = Form(None),
    temperature: float | None = Form(None),
    retries: int | None = Form(None),
    enqueue: bool = Form(False),
    source_group_id: str | None = Form(None),
) -> dict[str, Any]:
    """Persist a document conversion and return its completed output."""
    if action not in ACTION_CONFIG:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported action '{action}'. Valid choices: {sorted(ACTION_CONFIG)}",
        )

    norm_connector = connector.strip().lower()
    if norm_connector not in CONNECTORS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported connector '{connector}'. Valid choices: {sorted(CONNECTORS)}",
        )

    # Create an isolated, persistent history folder before generation starts.
    session_id = uuid.uuid4().hex
    group_id = source_group_id or session_id
    if len(group_id) != 32 or any(char not in "0123456789abcdef" for char in group_id):
        raise HTTPException(status_code=400, detail="Invalid source group identifier.")
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    conversion_dir = _conversion_dir(session_id)
    input_dir = conversion_dir / "input"
    output_dir = conversion_dir / "output"

    try:
        # Save the input inside the durable conversion record.
        input_path: Path
        if file is not None and file.filename:
            clean_filename = Path(file.filename).name
            content = await file.read()
            if not content:
                raise HTTPException(status_code=400, detail="Uploaded file is empty.")
            input_dir.mkdir(parents=True, exist_ok=True)
            input_path = input_dir / clean_filename
            input_path.write_bytes(content)
            LOGGER.info("Saved uploaded file (%d bytes) to %s", len(content), input_path)
        elif pasted_text and pasted_text.strip():
            ext = ".txt"
            if input_filename and Path(input_filename).suffix.lower() in SUPPORTED_EXTENSIONS:
                ext = Path(input_filename).suffix.lower()
            orig_name = Path(input_filename).name if input_filename else f"pasted_document{ext}"
            input_dir.mkdir(parents=True, exist_ok=True)
            input_path = input_dir / orig_name
            input_path.write_text(pasted_text, encoding="utf-8")
            LOGGER.info("Saved pasted text (%d chars) to %s", len(pasted_text), input_path)
        else:
            raise HTTPException(
                status_code=400,
                detail="No file or pasted text provided. Please select a PDF/document or paste text.",
            )

        # Determine and validate the output file path.
        action_meta = ACTION_METADATA.get(action, {})
        selected_ext = output_format.strip().lstrip(".") if output_format else action_meta.get("default_ext", "txt")
        if not selected_ext or len(selected_ext) > 32 or any(
            not (char.isalnum() or char in "-_.") for char in selected_ext
        ):
            raise HTTPException(status_code=400, detail="Output format contains invalid characters.")
        output_filename = f"{action}_{timestamp_str}.{selected_ext}"
        output_dir.mkdir(parents=True, exist_ok=True)

        options_kwargs: dict[str, Any] = {}
        if strategy:
            options_kwargs["strategy"] = strategy
        if chunk_tokens is not None and chunk_tokens > 0:
            options_kwargs["chunk_tokens"] = chunk_tokens
        if temperature is not None:
            options_kwargs["temperature"] = temperature
        if retries is not None:
            options_kwargs["retries"] = retries

        # Validate options before writing the record, but persist only JSON-safe values.
        PipelineOptions(**options_kwargs)
        created_at = _now()
        record = {
            "version": 1,
            "id": session_id,
            "status": "queued" if enqueue else "in_progress",
            "created_at": created_at,
            "updated_at": created_at,
            "completed_at": None,
            "input_filename": input_path.name,
            "input_path": str(input_path.relative_to(conversion_dir)),
            "action": action,
            "action_title": action_meta.get("title", action),
            "connector": norm_connector,
            "model": model.strip() if model else None,
            "output_format": selected_ext,
            "output_filename": output_filename,
            "output_path": str((output_dir / output_filename).relative_to(conversion_dir)),
            "options": options_kwargs,
            "api_key_was_supplied": bool(api_key and api_key.strip()),
            "error": None,
            "metrics": None,
            "source_group_id": group_id,
            "priority": time.time_ns(),
            "run_attempts": 0,
            "log": [{"at": created_at, "message": "Added to the conversion queue." if enqueue else "Conversion created."}],
        }
        _write_record(record)
        LOGGER.info("Created persistent conversion %s in %s", session_id, conversion_dir)

        if enqueue:
            if api_key and api_key.strip():
                QUEUED_API_KEYS[session_id] = api_key.strip()
            _ensure_queue_dispatcher()
            return {"success": True, "queued": True, "session_id": session_id}
        return await run_in_threadpool(_run_conversion, session_id, api_key)

    except HTTPException:
        if conversion_dir.is_dir() and not (conversion_dir / "conversion.json").exists():
            shutil.rmtree(conversion_dir)
        raise
    except ApplicationError as exc:
        if conversion_dir.is_dir() and not (conversion_dir / "conversion.json").exists():
            shutil.rmtree(conversion_dir)
        LOGGER.warning("Application error during generation: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        if conversion_dir.is_dir() and not (conversion_dir / "conversion.json").exists():
            shutil.rmtree(conversion_dir)
        LOGGER.exception("Unexpected error during document conversion")
        raise HTTPException(status_code=500, detail="Conversion failed unexpectedly.") from exc


@app.get("/api/history")
def list_conversion_history() -> dict[str, list[dict[str, Any]]]:
    """List durable conversions newest first, including unfinished work."""

    records: list[dict[str, Any]] = []
    disk_records = _records_on_disk()
    if any(record.get("status") == "queued" for record in disk_records):
        _ensure_queue_dispatcher()
    for record in disk_records:
        directory = _conversion_dir(record["id"])
        if record.get("status") == "discarding" and directory.name not in ACTIVE_CONVERSIONS:
            shutil.rmtree(directory)
            continue
        records.append(_public_record(record))
    records.sort(key=lambda item: (item.get("priority", 0), item.get("created_at") or ""))
    return {"conversions": records}


@app.post("/api/history/{conversion_id}/continue")
async def continue_conversion(
    conversion_id: str,
    api_key: str | None = Form(None),
    chunk_characters: int | None = Form(None),
) -> dict[str, Any]:
    """Resume an interrupted or failed conversion from its saved checkpoint."""

    try:
        if chunk_characters is not None:
            if chunk_characters < 256:
                raise HTTPException(status_code=400, detail="Chunk separation must be at least 256 characters.")
            with HISTORY_LOCK:
                record = _read_record(conversion_id)
                options = dict(record.get("options") or {})
                options["chunk_characters"] = chunk_characters
                options.pop("chunk_tokens", None)
                record["options"] = options
                _append_log(record, f"Retry requested with {chunk_characters} characters per chunk.")
                _write_record(record)
        return await run_in_threadpool(_run_conversion, conversion_id, api_key)
    except HTTPException:
        raise
    except GenerationCancelled as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except ApplicationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        LOGGER.exception("Unexpected error while continuing conversion %s", conversion_id)
        raise HTTPException(status_code=500, detail="Conversion failed unexpectedly.") from exc


@app.delete("/api/history/{conversion_id}")
def discard_conversion(conversion_id: str) -> dict[str, Any]:
    """Discard saved work immediately, or as soon as an active run stops."""

    conversion_dir = _conversion_dir(conversion_id)
    with HISTORY_LOCK:
        record = _read_record(conversion_id)
        if conversion_id in ACTIVE_CONVERSIONS:
            record.update({"status": "discarding", "updated_at": _now()})
            _write_record(record)
            CANCEL_EVENTS[conversion_id].set()
            return {"discarded": False, "discarding": True}
        QUEUED_API_KEYS.pop(conversion_id, None)
        shutil.rmtree(conversion_dir)
    QUEUE_WAKE.set()
    return {"discarded": True, "discarding": False}


@app.delete("/api/history/groups/{group_id}")
def cancel_file_group(group_id: str) -> dict[str, Any]:
    """Cancel every queued or active artifact belonging to one source file."""

    affected = 0
    for record in _records_on_disk():
        record_group = record.get("source_group_id") or f"legacy:{record.get('input_filename', record['id'])}"
        if record_group != group_id or record.get("status") == "completed":
            continue
        try:
            discard_conversion(record["id"])
            affected += 1
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
    return {"affected": affected}


@app.put("/api/history/order")
def reorder_file_groups(payload: dict[str, list[str]]) -> dict[str, bool]:
    """Persist file-level queue priority in the drag-and-drop order supplied."""

    group_ids = payload.get("group_ids")
    if not isinstance(group_ids, list) or any(not isinstance(item, str) for item in group_ids):
        raise HTTPException(status_code=400, detail="group_ids must be a list of identifiers.")
    priorities = {group_id: index for index, group_id in enumerate(group_ids)}
    with HISTORY_LOCK:
        for record in _records_on_disk():
            group_id = record.get("source_group_id") or f"legacy:{record.get('input_filename', record['id'])}"
            if group_id in priorities and record.get("status") == "queued":
                record["priority"] = priorities[group_id]
                _append_log(record, f"File priority changed to position {priorities[group_id] + 1}.")
                _write_record(record)
    QUEUE_WAKE.set()
    return {"success": True}


@app.get("/api/download/{session_id}/{filename}")
def download_output_file(session_id: str, filename: str) -> FileResponse:
    """Download a generated output file from persistent history."""

    record = _read_record(session_id)
    output_path = _conversion_dir(session_id) / record["output_path"]
    if record.get("status") != "completed" or not output_path.is_file() or record["output_filename"] != filename:
        raise HTTPException(status_code=404, detail="Target output file does not exist.")

    # Determine media type based on extension
    media_types = {
        ".json": "application/json",
        ".csv": "text/csv",
        ".md": "text/markdown",
        ".mmd": "text/plain",
        ".txt": "text/plain",
        ".pdf": "application/pdf",
    }
    media_type = media_types.get(output_path.suffix.lower(), "application/octet-stream")

    return FileResponse(
        path=output_path,
        filename=filename,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/history/groups/{group_id}/download")
def download_study_set_archive(group_id: str) -> FileResponse:
    """Snapshot the completed artifacts of a study set while other jobs continue."""

    archive_path = None
    try:
        # Keep completed files stable while copying them; each request has its own
        # ZIP so simultaneous downloads cannot overwrite one another.
        with HISTORY_LOCK:
            records = [record for record in _records_on_disk() if (
                record.get("source_group_id") or f"legacy:{record.get('input_filename', record['id'])}"
            ) == group_id]
            if not records:
                raise HTTPException(status_code=404, detail="Study set was not found.")
            records.sort(key=lambda record: (record.get("action", ""), record["id"]))
            completed = [record for record in records if record.get("status") == "completed"
                         and (_conversion_dir(record["id"]) / record["output_path"]).is_file()]
            if not completed:
                raise HTTPException(status_code=409, detail="No completed artifacts are available to download yet.")
            included_ids = {record["id"] for record in completed}
            partial = len(completed) < len(records)
            manifest = {
                "source_group_id": group_id,
                "input_filename": records[0]["input_filename"],
                "exported_at": _now(), "partial": partial,
                "completed_artifacts": len(completed), "total_artifacts": len(records),
                "artifacts": [{
                    "id": record["id"], "action": record["action"],
                    "status": record.get("status"), "included": record["id"] in included_ids,
                    "output_filename": record.get("output_filename"),
                } for record in records],
            }
            with tempfile.NamedTemporaryFile(prefix="study-set-", suffix=".zip", delete=False) as temporary:
                archive_path = Path(temporary.name)
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
                source_record = completed[0]
                source_path = _conversion_dir(source_record["id"]) / source_record["input_path"]
                if source_path.is_file():
                    archive.write(source_path, f"source/{source_record['input_filename']}")
                for record in completed:
                    conversion_dir = _conversion_dir(record["id"])
                    prefix = f"artifacts/{record['action']}-{record['id']}"
                    archive.write(conversion_dir / record["output_path"], f"{prefix}/{record['output_filename']}")
                    archive.writestr(f"{prefix}/conversion.json", json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        safe_stem = Path(records[0]["input_filename"]).stem or "study-set"
        suffix = "-partial" if partial else ""
        return FileResponse(
            path=archive_path, filename=f"{safe_stem}-study-set{suffix}.zip",
            media_type="application/zip",
            background=BackgroundTask(archive_path.unlink, missing_ok=True),
        )
    except Exception:
        if archive_path is not None:
            archive_path.unlink(missing_ok=True)
        raise


@app.get("/api/history/{conversion_id}/download")
def download_conversion_archive(conversion_id: str) -> FileResponse:
    """Download a completed conversion's source, output, and metadata as ZIP."""

    record = _read_record(conversion_id)
    if record.get("status") != "completed":
        raise HTTPException(status_code=409, detail="Only completed conversions can be downloaded.")
    conversion_dir = _conversion_dir(conversion_id)
    archive_path = conversion_dir / f"{conversion_id}.zip"
    temporary = archive_path.with_suffix(f".zip.{uuid.uuid4().hex}.tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(conversion_dir / record["input_path"], f"source/{record['input_filename']}")
        archive.write(conversion_dir / record["output_path"], f"output/{record['output_filename']}")
        archive.write(conversion_dir / "conversion.json", "conversion.json")
    os.replace(temporary, archive_path)
    safe_stem = Path(record["input_filename"]).stem or "conversion"
    return FileResponse(
        path=archive_path,
        filename=f"{safe_stem}-{record['action']}.zip",
        media_type="application/zip",
    )


# Serve frontend production build if available
frontend_dist = Path(__file__).resolve().parent / "frontend" / "dist"
if frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    LOGGER.info("Starting API server on http://%s:%d", host, port)
    uvicorn.run("server:app", host=host, port=port, reload=True)
