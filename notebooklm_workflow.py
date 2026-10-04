"""Hybrid NotebookLM conversion and source-grounded model generation.

Existing textual exports are converted directly. Missing artifacts and podcasts
use each original Sources document, while visual artifacts supply locally
extracted/OCR text to the configured generation pipeline. Companion formats
render the same canonical generation rather than requesting another response.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from pathlib import Path

from app_config import ACTION_CONFIG
from connectors import create_connector
from document_loader import SUPPORTED_EXTENSIONS, load_document
from errors import GenerationCancelled, InputDocumentError, InvalidArgumentsError, ProviderError
from notebooklm_converter import (
    NotebookLMArtifact, _canonical, _preserve_media,
    artifact_from_metadata, convert_notebooklm_artifact, supported_notebooklm_formats,
)
from notebooklm_metadata import load_notebooklm_source_metadata
from notebooklm_renderers import render_artifact
from output_writer import write_output
from pipeline.engine import PipelineOptions, PipelineResult, run_pipeline
from pipeline.schemas import canonical_action, get_schema, schema_errors
from skill_loader import load_skill

_METADATA_SUFFIX = " metadata.json"
_RUN_CACHE = {}
_WORK_LOCKS = {}


@dataclass(frozen=True)
class NotebookLMSource:
    path: Path
    source_id: str
    dependencies: tuple[Path, ...] = ()
    notebooklm_ids: tuple[str, ...] = ()
    source_name: str = ""

    @property
    def filename(self) -> str:
        if self.source_name:
            return self.source_name
        return self.path.name[:-5] if self.path.suffix.lower() == ".html" else self.path.name


@dataclass(frozen=True)
class NotebookLMTask:
    mode: str
    source: NotebookLMSource
    action: str
    artifact: NotebookLMArtifact | None = None

    @property
    def dependencies(self) -> tuple[Path, ...]:
        values = [self.source.path, *self.source.dependencies]
        if self.artifact is not None:
            values.extend([self.artifact.source, *self.artifact.dependencies])
        return tuple(dict.fromkeys(values))


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise InputDocumentError(f"Could not read NotebookLM metadata '{path.name}': {exc}") from exc


def _bare(value: str) -> str:
    return Path(value.replace("\\", "/")).name


def _roots(input_dir: Path) -> tuple[Path, Path]:
    if input_dir.name.lower() == "artifacts":
        return input_dir, input_dir.parent / "Sources"
    if input_dir.name.lower() == "sources":
        return input_dir.parent / "Artifacts", input_dir
    return input_dir / "Artifacts", input_dir / "Sources"


def _contained(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise InvalidArgumentsError("NotebookLM source and dependency paths must stay inside the study-set package.") from exc


def discover_notebooklm_sources(input_dir: Path, artifacts=(), *, contained: bool = False,
                               package_root: Path | None = None,
                               metadata_mapping: tuple[Path, dict[str, str]] | None = None) -> list[NotebookLMSource]:
    """Find source documents using the required filename-to-UUID mapping."""
    input_dir = Path(input_dir)
    if not input_dir.is_dir():
        raise InputDocumentError(f"NotebookLM input directory does not exist: {input_dir}")
    mapping_path, mapping = metadata_mapping if metadata_mapping is not None else load_notebooklm_source_metadata(
        input_dir, contained=contained, package_root=package_root,
    )
    mapping = {name.casefold(): identifier for name, identifier in mapping.items()}
    _, source_root = _roots(input_dir)
    if not source_root.is_dir():
        return []
    boundary = Path(package_root) if package_root is not None else (
        input_dir.parent if input_dir.name.lower() in {"sources", "artifacts"} else input_dir)
    metadata_by_name = {}
    for metadata_path in sorted(source_root.rglob("*metadata.json")):
        suffix = next((suffix for suffix in (_METADATA_SUFFIX, ".metadata.json")
                       if metadata_path.name.endswith(suffix)), None)
        if suffix is None:
            continue
        if contained:
            _contained(metadata_path, boundary)
        metadata = _json(metadata_path)
        if not isinstance(metadata, dict):
            raise InputDocumentError("NotebookLM source metadata must contain an object.")
        name = metadata.get("title", metadata_path.name[:-len(suffix)])
        if not isinstance(name, str) or not name.strip():
            raise InputDocumentError("NotebookLM source metadata needs its original filename.")
        metadata_by_name[_bare(name).casefold()] = (metadata_path, metadata)
        metadata_by_name[metadata_path.name[:-len(suffix)].casefold()] = (metadata_path, metadata)
    results, seen_ids = [], set()
    for path in sorted(source_root.rglob("*")):
        if not path.is_file() or path.name.endswith((_METADATA_SUFFIX, ".metadata.json")) or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        if contained:
            _contained(path, boundary)
        original = path.name[:-5] if path.suffix.lower() == ".html" else path.name
        source_metadata = metadata_by_name.get(original.casefold(), metadata_by_name.get(path.name.casefold()))
        dependencies = [path, mapping_path]
        metadata_name = original + _METADATA_SUFFIX
        if source_metadata:
            metadata_path, metadata = source_metadata
            dependencies.append(metadata_path)
            metadata_name = metadata_path.name
            title = metadata.get("title")
            if isinstance(title, str) and title.strip():
                original = _bare(title)
        source_id = mapping.get(metadata_name.casefold())
        if source_id is None and source_metadata is None:
            source_id = mapping.get((original + ".metadata.json").casefold())
        if source_id is None:
            raise InputDocumentError(
                f"NotebookLM source '{original}' has no entry for '{metadata_name}' in '{mapping_path}'. "
                "Add that metadata filename with its NotebookLM source UUID before processing."
            )
        if source_id in seen_ids:
            raise InputDocumentError(f"NotebookLM source UUID '{source_id}' is attached to multiple source documents.")
        seen_ids.add(source_id)
        dependencies = tuple(dict.fromkeys(path for path in dependencies if path.is_file()))
        if contained:
            for dependency in dependencies:
                _contained(dependency, boundary)
        results.append(NotebookLMSource(path, source_id, dependencies, (source_id,), original))
    return results


def classify_artifact_mode(artifact: NotebookLMArtifact) -> str:
    """Structured visual sidecars convert; raw visual exports need the model."""
    content = artifact.metadata_path.with_name(artifact.name + ".content.json")
    if content.is_file() or artifact.source.suffix.lower() == ".json":
        return "convert"
    if artifact.action == "create_slides":
        return "ocr"
    if artifact.action == "create_infographics" and artifact.source.suffix.lower() != ".svg":
        return "ocr"
    return "convert"


def artifact_needs_ocr(artifact: NotebookLMArtifact) -> bool:
    return classify_artifact_mode(artifact) == "ocr"


def task_to_dict(task: NotebookLMTask, *, base_dir: Path | None = None) -> dict:
    def path_value(path):
        path = Path(path)
        if base_dir is not None:
            _contained(path, Path(base_dir))
            return path.resolve().relative_to(Path(base_dir).resolve()).as_posix()
        return str(path)
    value = {"mode": task.mode, "action": task.action,
             "source": {"path": path_value(task.source.path), "source_id": task.source.source_id,
                        "source_name": task.source.filename, "dependencies": [path_value(path) for path in task.source.dependencies],
                        "notebooklm_ids": list(task.source.notebooklm_ids)}}
    if task.artifact is not None:
        value["artifact_metadata_path"] = path_value(task.artifact.metadata_path)
        value["artifact_source_names"] = list(task.artifact.source_names)
    return value


def task_from_dict(value: dict, *, base_dir: Path | None = None) -> NotebookLMTask:
    if not isinstance(value, dict) or not isinstance(value.get("source"), dict):
        raise InvalidArgumentsError("Invalid saved NotebookLM task.")
    def path_value(value):
        if not isinstance(value, str) or not value:
            raise InvalidArgumentsError("Invalid saved NotebookLM task path.")
        path = Path(value)
        if base_dir is not None:
            path = Path(base_dir) / path
            _contained(path, Path(base_dir))
        return path
    saved = value["source"]
    source = NotebookLMSource(path_value(saved.get("path")), saved.get("source_id", ""),
                              tuple(path_value(path) for path in saved.get("dependencies", [])),
                              tuple(saved.get("notebooklm_ids", [])), saved.get("source_name", ""))
    artifact = None
    if value.get("artifact_metadata_path"):
        artifact = artifact_from_metadata(path_value(value["artifact_metadata_path"]),
                                          source_names=value.get("artifact_source_names", [source.filename]))
    task = NotebookLMTask(value.get("mode", ""), source, canonical_action(value.get("action", "")), artifact)
    _validate_task(task)
    return task


def _validate_task(task: NotebookLMTask):
    if task.mode not in {"convert", "source", "ocr"} or task.action not in ACTION_CONFIG:
        raise InvalidArgumentsError("Unknown NotebookLM task mode or artifact action.")
    if not isinstance(task.source.source_id, str) or not task.source.source_id:
        raise InputDocumentError("NotebookLM tasks require a recorded source UUID.")
    if _bare(task.source.source_id) != task.source.source_id or any(char in task.source.source_id for char in ':\x00\r\n'):
        raise InputDocumentError("NotebookLM source UUID must be a safe folder identifier.")
    if not task.source.filename or _bare(task.source.filename) != task.source.filename:
        raise InputDocumentError("NotebookLM source_name must be its original bare filename.")
    if task.mode in {"convert", "ocr"} and task.artifact is None:
        raise InvalidArgumentsError("NotebookLM conversion and OCR tasks require an exported artifact.")
    if task.artifact is not None and task.artifact.action != task.action:
        raise InvalidArgumentsError("NotebookLM task action does not match its exported artifact.")
    if task.mode == "ocr" and task.action not in {"create_slides", "create_infographics"}:
        raise InvalidArgumentsError("NotebookLM OCR supports slides and infographics only.")
    if task.action == "create_podcasts" and task.mode == "source" and task.artifact is not None:
        raise InvalidArgumentsError("NotebookLM podcast generation ignores exported audio artifacts.")


def _cancel(check):
    if check is not None and check():
        raise GenerationCancelled("NotebookLM processing was cancelled.")


def _extension(path: Path) -> str:
    return ".wireframe.txt" if path.name.lower().endswith(".wireframe.txt") else path.suffix.lower()


def _fingerprint(task, connector_name, model, options, skill_text, check):
    settings = {field.name: str(getattr(options, field.name)) for field in fields(options)
                if field.name not in {"cancel_check", "force", "work_dir"}}
    dependencies = []
    for path in task.dependencies:
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    _cancel(check)
                    digest.update(block)
            dependencies.append((path.name, digest.hexdigest()))
    value = {"mode": task.mode, "action": task.action, "source_id": task.source.source_id,
             "source_name": task.source.filename, "connector": connector_name, "model": model,
             "settings": settings, "dependencies": sorted(dependencies), "skill": skill_text,
             "schema": get_schema(task.action), "workflow_version": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def _generation_assets(payload: dict, work_dir: Path, metrics: dict, *, partial: bool = False) -> tuple[list[dict], list[str]]:
    from notebooklm_renderers import _safe_svg
    inline = payload.get("assets", [])
    if isinstance(inline, dict):
        inline = [{"id": reference, "svg": content} for reference, content in inline.items()]
    values = {asset.get("id"): asset.get("svg", asset.get("content")) for asset in inline
              if isinstance(asset, dict)} if isinstance(inline, list) else {}
    references = [section.get("value") for section in payload.get("sections", [])
                  if isinstance(section, dict) and section.get("type") == "svg"]
    assets, warnings, seen = [], [], set()
    def reject(message):
        if partial:
            warnings.append(message)
            return
        error = ProviderError(message)
        error.pipeline_metrics = metrics
        raise error
    for reference in sorted(references, key=str):
        if not isinstance(reference, str) or not re.fullmatch(r"assets/[\w-]+\.svg", reference):
            reject("Generated infographic contains an invalid SVG asset reference.")
            continue
        if reference in seen:
            continue
        seen.add(reference)
        content = values.get(reference)
        if not isinstance(content, str):
            path = next((path for path in (work_dir / reference, work_dir / "aggregate" / reference) if path.is_file()), None)
            if path is not None:
                content = path.read_text(encoding="utf-8-sig")
        if not isinstance(content, str):
            reject(f"Generated infographic references missing SVG content '{reference}'.")
            continue
        try:
            assets.append({"id": reference, "svg": _safe_svg(content)})
        except InvalidArgumentsError as exc:
            reject(f"Generated infographic SVG is invalid: {exc}")
    return assets, warnings


@contextmanager
def _work_lock(work_dir: Path, check):
    lock = _WORK_LOCKS.setdefault(str(work_dir.resolve()), threading.Lock())
    while not lock.acquire(timeout=0.1):
        _cancel(check)
    try:
        _cancel(check)
        yield
    finally:
        lock.release()


def execute_notebooklm_task(task: NotebookLMTask, output_path: Path, *, connector_name=None, model=None,
                           api_key=None, options: PipelineOptions | None = None, cancel_check=None) -> PipelineResult:
    options = options or PipelineOptions()
    check = cancel_check or options.cancel_check
    if task.mode == "convert":
        return _execute_notebooklm_task(task, output_path, connector_name=connector_name, model=model,
                                       api_key=api_key, options=options, cancel_check=check)
    work_dir = Path(options.work_dir or Path(output_path).parent / ".notebooklm-work")
    with _work_lock(work_dir, check):
        return _execute_notebooklm_task(task, output_path, connector_name=connector_name, model=model,
                                       api_key=api_key, options=options, cancel_check=check)


def _execute_notebooklm_task(task: NotebookLMTask, output_path: Path, *, connector_name=None, model=None,
                            api_key=None, options: PipelineOptions | None = None, cancel_check=None) -> PipelineResult:
    """Execute a hybrid task with normal pipeline recovery and cancellation."""
    _validate_task(task)
    output_path = Path(output_path)
    options = options or PipelineOptions()
    check = cancel_check or options.cancel_check
    _cancel(check)
    if task.action == "create_podcasts" and task.mode != "source":
        raise InvalidArgumentsError("NotebookLM podcasts must be generated from the original Sources document.")
    if task.mode == "convert":
        artifact = task.artifact
        if task.source.filename and len(artifact.source_names) <= 1:
            artifact = replace(artifact, source_names=(task.source.filename,))
        return convert_notebooklm_artifact(artifact, output_path, cancel_check=check)
    extension = _extension(output_path)
    if extension not in supported_notebooklm_formats(task.action):
        raise InvalidArgumentsError(f"Unsupported NotebookLM output format: {extension}")
    if not connector_name:
        raise InvalidArgumentsError("NotebookLM source/OCR generation requires a configured connector and model.")
    if any(output_path.resolve() == path.resolve() for path in task.dependencies):
        raise InvalidArgumentsError("NotebookLM generation must not overwrite source input files.")
    metadata_path = output_path.with_suffix(".metadata.json")
    if any(metadata_path.resolve() == path.resolve() for path in task.dependencies):
        raise InvalidArgumentsError("NotebookLM generation metadata must not overwrite source input files.")
    for dependency in task.dependencies:
        if dependency.is_dir() and (output_path.resolve().is_relative_to(dependency.resolve()) or
                                    metadata_path.resolve().is_relative_to(dependency.resolve())):
            raise InvalidArgumentsError("NotebookLM generation output must not be inside an exported artifact directory.")
    # Sources HTML is handled by the existing parser, which excludes script/style.
    fallback_visual = task.mode == "ocr" and task.artifact is not None and task.source.path.resolve() == task.artifact.source.resolve()
    original_text = "" if fallback_visual else load_document(task.source.path)
    source_path = Path(task.source.filename)
    work_dir = Path(options.work_dir or output_path.parent / ".notebooklm-work")
    options = replace(options, work_dir=work_dir, cancel_check=check)
    for dependency in task.dependencies:
        if dependency.is_dir() and work_dir.resolve().is_relative_to(dependency.resolve()):
            raise InvalidArgumentsError("NotebookLM generation work directory must not be inside its exported artifact directory.")
    skill = load_skill(ACTION_CONFIG[task.action]["skill"])
    skill_text = skill.text
    if task.action == "create_infographics":
        skill_text += ("\n\nNOTEBOOKLM SINGLE-FILE SVG ASSET CONTRACT\n"
                       "When using a section of type svg, include its SVG drawing in an extra top-level assets array: "
                       "[{\"id\":\"assets/<slug>.svg\",\"svg\":\"<svg ...>...</svg>\"}]. "
                       "Every referenced SVG must have actual declarative SVG content in that array. "
                       "Do not merely name or promise an external file. Alternatively use the other supported section types. "
                       "The application stores these drawings separately and removes the assets field from the canonical artifact JSON.")
    fingerprint = _fingerprint(task, connector_name, model, options, skill_text, check)
    key = (str(work_dir.resolve()), fingerprint)
    canonical_path = work_dir / "notebooklm-canonical.json"
    record_path = work_dir / "notebooklm-run.json"
    memory = _RUN_CACHE.get(key)
    regenerate = options.force and (memory is None or extension in memory.get("formats", set()))
    if not options.checkpoint and memory is not None and extension in memory.get("formats", set()):
        regenerate = True
    record = memory.get("record") if memory is not None and not regenerate else None
    if record is not None and (not record.get("validation", {}).get("valid") or record.get("validation", {}).get("partial")):
        record = None
    if record is None and options.checkpoint and not options.force and record_path.is_file() and canonical_path.is_file():
        candidate = _json(record_path)
        if candidate.get("fingerprint") == fingerprint and candidate.get("validation", {}).get("valid") and not candidate.get("validation", {}).get("partial"):
            record = candidate
    reused = record is not None
    if record is None:
        source_text = original_text
        if task.mode == "ocr":
            from notebooklm_media import extract_ocr_text
            recognized = extract_ocr_text(task.artifact.source, task.action, cancel_check=check)
            if not recognized.strip():
                raise InputDocumentError("No text could be extracted from the NotebookLM visual artifact.")
            source_text = ((f"Original source document: {task.source.filename}\n{original_text}\n\n" if original_text else "") +
                           f"NotebookLM visual artifact: {task.artifact.name}\n"
                           "The following text was extracted locally from the visual artifact. "
                           "Preserve its supported information and use the original source for context.\n" + recognized)
        _cancel(check)
        connector = create_connector(connector_name, model=model, api_key=api_key)
        result = run_pipeline(source_text=source_text, source_path=source_path, skill_text=skill_text,
                              connector=connector, connector_name=connector_name, action=task.action,
                              output_path=canonical_path, options=options)
        _cancel(check)
        try:
            payload = json.loads(result.text)
        except ValueError as exc:
            write_output(canonical_path, result.text)
            raise ProviderError("NotebookLM generation returned invalid canonical JSON; pipeline diagnostics were retained.") from exc
        if not isinstance(payload, dict):
            raise ProviderError("NotebookLM generation must return a canonical artifact object.")
        data = _canonical(payload, task.action)
        validation = copy.deepcopy(result.validation)
        errors = schema_errors(data, get_schema(task.action))
        if errors and not validation.get("partial"):
            validation.update({"valid": False, "partial": True, "output_errors": errors, "reason": "invalid_canonical_generation"})
        if validation.get("valid") is False and not validation.get("partial"):
            validation.update({"partial": True, "reason": validation.get("reason", "generation_validation_failed")})
        metrics = copy.deepcopy(result.metrics)
        if validation.get("partial"):
            metrics.setdefault("failure_exit_code", ProviderError.exit_code)
            if metrics.get("status") in {None, "complete", "completed", "success"}:
                metrics["status"] = "partial"
        assets, asset_warnings = (_generation_assets(payload, work_dir, metrics, partial=bool(validation.get("partial")))
                                  if task.action == "create_infographics" else ([], []))
        if asset_warnings:
            validation.setdefault("warnings", []).extend(asset_warnings)
        record = {"fingerprint": fingerprint, "canonical": data, "original_pipeline_payload": payload,
                  "metrics": metrics, "validation": validation, "work_dir": str(result.work_dir),
                  "connector": connector_name, "model": getattr(connector, "model", model), "mode": task.mode}
        if assets:
            record["assets"] = assets
        if asset_warnings:
            record["asset_warnings"] = asset_warnings
        write_output(canonical_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        write_output(record_path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        _RUN_CACHE[key] = {"record": record, "formats": set()}
    _cancel(check)
    data = record["canonical"]
    assets = record.get("assets", [])
    validation = copy.deepcopy(record["validation"])
    metrics = copy.deepcopy(record["metrics"])
    if reused:
        metrics["generation_provider_calls"] = metrics.get("provider_calls", 0)
        for metric in ("provider_calls", "input_tokens", "output_tokens", "retry_count", "repair_count", "estimated_cost"):
            if metric in metrics:
                metrics[metric] = 0
    metrics.update({"study_set_type": "notebooklm", "notebooklm_mode": task.mode, "canonical_reused": reused,
                    "source_id": task.source.source_id, "source_names": [task.source.filename]})
    if extension == ".json":
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    elif validation.get("partial") and schema_errors(data, get_schema(task.action)):
        text = "# Partial artifact\n\n" + "```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```\n"
    else:
        text = render_artifact({**data, **({"assets": assets} if assets else {})}, task.action, extension)
    metadata = {"study_set_type": "notebooklm", "mode": task.mode, "source_id": task.source.source_id,
                "source_name": task.source.filename, "notebooklm_ids": list(task.source.notebooklm_ids),
                "connector": record["connector"], "model": record["model"], "validation": validation,
                "original_pipeline_payload": record["original_pipeline_payload"]}
    if record.get("asset_warnings"):
        metadata["asset_warnings"] = record["asset_warnings"]
    if task.artifact is not None:
        metadata["original_artifact_metadata"] = task.artifact.metadata
        if task.mode == "ocr":
            metadata["extraction"] = "Local visual text/OCR followed by configured-model canonical generation."
            metadata["original_media"] = _preserve_media(task.artifact, output_path, check)
    for asset in assets:
        destination = output_path.parent / asset["id"]
        if any(destination.resolve() == path.resolve() for path in task.dependencies):
            raise InvalidArgumentsError("Generated SVG assets must not overwrite NotebookLM input files.")
    for asset in assets:
        _cancel(check)
        write_output(output_path.parent / asset["id"], asset["svg"])
    _cancel(check)
    write_output(output_path.with_suffix(".metadata.json"), json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    write_output(output_path, text)
    _RUN_CACHE.setdefault(key, {"record": record, "formats": set()})["formats"].add(extension)
    return PipelineResult(text, metrics, validation, Path(record["work_dir"]))
