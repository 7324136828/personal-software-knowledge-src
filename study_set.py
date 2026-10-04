#!/usr/bin/env python3
"""Persistent study-set prompt with configuration-driven batch generation."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from app_config import ACTION_CONFIG
from cli_runtime import add_pipeline_arguments, options_from_args, run_cli_generation
from connectors import CONNECTORS
from errors import ApplicationError, InputDocumentError, InvalidArgumentsError
from package_paths import relative_package_parts
from skills.file_naming_convention.script.main import current_timestamp as _timestamp

if TYPE_CHECKING:
    from notebooklm_converter import NotebookLMArtifact
    from notebooklm_workflow import NotebookLMTask


CONFIG_NAME = "study-set-config.json"
PROMPT_COMMANDS = ("ls", "cd", "generate", "exit")
STUDY_TYPES = {
    "datatable": "create_datatables",
    "flashcard": "create_flashcards",
    "infographic": "create_infographics",
    "mindmap": "create_mindmaps",
    "podcast": "create_podcasts",
    "qanda": "create_qandas",
    "quiz": "create_quizzes",
    "report": "create_reports",
    "slide": "create_slides",
}
_TYPE_ALIASES = {
    **STUDY_TYPES,
    **{action: action for action in ACTION_CONFIG},
    **{action.removeprefix("create_"): action for action in ACTION_CONFIG},
    "data-table": "create_datatables", "data-tables": "create_datatables",
    "mind-map": "create_mindmaps", "mind-maps": "create_mindmaps",
    "podcast-script": "create_podcasts", "qa": "create_qandas", "q&a": "create_qandas",
}


_CANONICAL_TYPES = {action: name for name, action in STUDY_TYPES.items()}
_FORMATS = {
    "datatable": {"json", "csv"},
    "flashcard": {"json", "txt"},
    "infographic": {"json", "md", "html", "svg"},
    "mindmap": {"json", "md", "mmd"},
    "podcast": {"json", "md"},
    "qanda": {"json"},
    "quiz": {"json"},
    "report": {"json", "md", "html"},
    "slide": {"json", "md"},
}

_NOTEBOOKLM_DEFAULT_FORMATS = {
    "create_datatables": ["json", "csv"],
    "create_flashcards": ["json", "txt"],
    "create_infographics": ["json", "md", "html", "svg", "wireframe.txt"],
    "create_mindmaps": ["json", "md", "mmd"],
    "create_podcasts": ["json", "md"],
    "create_qandas": ["json"],
    "create_quizzes": ["json"],
    "create_reports": ["json", "md", "html"],
    "create_slides": ["json", "md"],
}


def _notebooklm_filename(action: str, timestamp: str, index: int) -> str:
    if action == "create_podcasts":
        return f"{timestamp}_episode{index}"
    prefix = {"create_datatables": "", "create_qandas": "",
              "create_flashcards": "flashcards_", "create_infographics": "infographic_",
              "create_mindmaps": "mindmap_", "create_quizzes": "quiz_",
              "create_reports": "report_", "create_slides": "slides_"}[action]
    return prefix + timestamp + (f"_{index + 1}" if index else "")


@dataclass
class GenerationJob:
    args: argparse.Namespace
    source: Path
    action: str
    output: Path
    input_type: str = "document"
    notebooklm_artifact: NotebookLMArtifact | None = None
    notebooklm_task: NotebookLMTask | None = None


class StudySetCompleter(Completer):
    """Complete commands and cd directories relative to the current prompt folder."""

    def get_completions(self, document: Document, complete_event):
        line = document.text_before_cursor.lstrip()
        if not any(character.isspace() for character in line):
            if not document.text_after_cursor:
                for command in PROMPT_COMMANDS:
                    if command.startswith(line.lower()):
                        yield Completion(command, start_position=-len(line))
            return
        command = line.split(maxsplit=1)[0]
        if command.lower() != "cd":
            return
        argument = line[len(command):].lstrip()
        quote = argument[0] if argument.startswith(('"', "'")) else ""
        fragment = argument[1:] if quote else argument
        if quote and fragment.endswith(quote):
            fragment = fragment[:-1]
        # Leave an existing closing quote after the cursor in place.
        suffix = document.text_after_cursor
        if suffix and suffix != quote:
            return
        directory, prefix = os.path.split(fragment)
        path_prefix = fragment[:len(fragment) - len(prefix)]
        separator = os.sep
        if "/" in fragment or "\\" in fragment:
            separator = "/" if fragment.rfind("/") > fragment.rfind("\\") else "\\"
        try:
            children = sorted(Path(directory or ".").expanduser().iterdir(),
                              key=lambda item: item.name.casefold())
            for child in children:
                if not child.is_dir() or not child.name.casefold().startswith(prefix.casefold()):
                    continue
                value = path_prefix + child.name + separator
                completion_quote = quote or ('"' if any(character.isspace() for character in value) else "")
                if completion_quote:
                    value = completion_quote + value + ("" if suffix else completion_quote)
                yield Completion(value, start_position=-len(argument), display=child.name + separator)
        except (OSError, ValueError):
            return


def _command_reader():
    """Use a Windows-capable line editor for terminals and plain input for pipes."""

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return input
    return PromptSession(completer=StudySetCompleter(), complete_while_typing=False).prompt


def normalize_study_type(value: str) -> str:
    """Accept friendly artifact names and the existing create_* action names."""

    try:
        return _TYPE_ALIASES[value.strip().lower()]
    except KeyError as exc:
        raise argparse.ArgumentTypeError("Unknown study-set type. Choose: " + ", ".join(STUDY_TYPES)) from exc


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgumentsError(f"{field} must be a nonempty string.")
    return value


def _strings(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise InvalidArgumentsError(f"{field} must be a nonempty list.")
    return [_text(item, field) for item in value]


def _path(value: object, field: str, directory: Path, *, contained: bool = False) -> Path:
    text = _text(value, field)
    if contained:
        path = directory.joinpath(*relative_package_parts(text, allow_parent=True)).resolve()
        if not path.is_relative_to(directory.resolve()):
            raise InvalidArgumentsError(f"{field} must stay inside the study-set package.")
        return path
    return (directory / Path(text).expanduser()).resolve()


def _pipeline_settings(value: object, field: str) -> dict:
    if not isinstance(value, dict):
        raise InvalidArgumentsError(f"{field} must be an object.")
    booleans = {"checkpoint", "validate", "keep_raw", "force"}
    integers = {"chunk_tokens", "chunk_overlap", "max_output_tokens", "group_size", "retries",
                "generation_passes", "context_window", "profile_max_output_tokens"}
    strings = {"strategy", "aggregation", "work_dir", "experiment", "model_profile"}
    for name, setting in value.items():
        if name in booleans:
            valid = isinstance(setting, bool)
        elif name in integers:
            valid = isinstance(setting, int) and not isinstance(setting, bool)
        elif name == "temperature":
            valid = isinstance(setting, (int, float)) and not isinstance(setting, bool)
        elif name in strings:
            valid = isinstance(setting, str) and bool(setting.strip())
        else:
            raise InvalidArgumentsError(f"Unknown {field} option '{name}'.")
        if not valid:
            raise InvalidArgumentsError(f"Invalid value for {field}.{name}.")
        if name in {"context_window", "profile_max_output_tokens"} and setting < 1:
            raise InvalidArgumentsError(f"{field}.{name} must be positive.")
    return value


def _entry_pipeline(config: dict, label: str = "") -> dict:
    """Accept a token context fallback beside pipeline options at either level."""

    prefix = label + "." if label else ""
    settings = dict(_pipeline_settings(config.get("pipeline", {}), prefix + "pipeline"))
    if "context_window" in config:
        value = config["context_window"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise InvalidArgumentsError(f"{prefix}context_window must be a positive integer.")
        settings.setdefault("context_window", value)
    return settings


def _input_type(value: object, field: str) -> str:
    kind = _text(value, field).strip().lower()
    if kind not in {"document", "notebooklm"}:
        raise InvalidArgumentsError(f"{field} must be 'document' or 'notebooklm'.")
    return kind


def _notebooklm_tasks(input_dir: Path, actions: list[str], directory: Path, *, contained: bool):
    """Associate exports with their source UUIDs and fill each source's missing types."""
    from notebooklm_converter import _source_ids, discover_notebooklm_artifacts
    from notebooklm_metadata import load_notebooklm_source_metadata, notebooklm_root
    from notebooklm_workflow import (
        NotebookLMSource, NotebookLMTask, classify_artifact_mode, discover_notebooklm_sources,
    )

    metadata_mapping = load_notebooklm_source_metadata(
        input_dir, contained=contained, package_root=directory if contained else None,
    )
    mapping_path, source_metadata = metadata_mapping
    names_by_id = {}
    for metadata_name, identifier in source_metadata.items():
        suffix = " metadata.json" if metadata_name.endswith(" metadata.json") else ".metadata.json"
        names_by_id[identifier] = metadata_name[:-len(suffix)]
    ids_by_name = {name.casefold(): identifier for identifier, name in names_by_id.items()}

    # Podcasts are authored from original sources; their audio exports are not inputs.
    import_actions = [action for action in actions if action != "create_podcasts"]
    artifact_input = notebooklm_root(input_dir) if input_dir.name.casefold() == "sources" else input_dir
    artifacts = (discover_notebooklm_artifacts(
        artifact_input, import_actions, contained=contained, package_root=directory if contained else None,
        allow_empty=True, warn_missing=False,
    ) if import_actions else [])
    sources = discover_notebooklm_sources(
        input_dir, artifacts, contained=contained, package_root=directory if contained else None,
        metadata_mapping=metadata_mapping,
    )
    grouped = {source.source_id: [] for source in sources}
    by_id = {source.source_id: source for source in sources}
    for artifact in artifacts:
        ids = list(dict.fromkeys(_source_ids(artifact.metadata)))
        if not ids:
            ids = list(dict.fromkeys(ids_by_name[name.casefold()] for name in artifact.source_names
                                     if name.casefold() in ids_by_name))
        unmapped_ids = set(ids) - names_by_id.keys()
        if unmapped_ids:
            raise InputDocumentError(
                f"NotebookLM artifact '{artifact.name}' references source UUIDs missing from '{mapping_path}': "
                + ", ".join(sorted(unmapped_ids))
                + ". Add their source metadata filenames and UUIDs before processing."
            )
        missing_ids = set(ids) - by_id.keys()
        if sources and missing_ids:
            raise InputDocumentError(
                f"NotebookLM artifact '{artifact.name}' references source UUIDs without exported Sources documents: "
                + ", ".join(sorted(missing_ids)) + ". Include those source exports to build their study-set bundles."
            )
        matches = ([source for source in sources if source.source_id in ids] if ids else
                   [source for source in sources if source.source_name.casefold() in
                    {name.casefold() for name in artifact.source_names}])
        if not matches and len(sources) == 1 and not ids and not artifact.source_names:
            matches = sources
        if ids:
            artifact = replace(artifact, source_names=tuple(
                by_id[identifier].source_name if identifier in by_id else names_by_id[identifier]
                for identifier in ids
            ))
        if not matches:
            if sources:
                raise InputDocumentError(
                    f"NotebookLM artifact '{artifact.name}' cannot be associated with an exported source UUID. "
                    "Keep its source references, original Sources exports, and matching entries in sources.metadata.json."
                )
            if not ids:
                raise InputDocumentError(f"NotebookLM artifact '{artifact.name}' has no source UUID for its output bundle.")
            for source_id in ids:
                source_name = names_by_id[source_id]
                source = by_id.setdefault(source_id, NotebookLMSource(
                    path=artifact.source, source_id=source_id, source_name=source_name,
                    dependencies=tuple(dict.fromkeys((*artifact.dependencies, mapping_path))),
                    notebooklm_ids=(source_id,),
                ))
                grouped.setdefault(source_id, [])
                matches.append(source)
        for source in matches:
            grouped[source.source_id].append(artifact)
    tasks = []
    actual_ids = {source.source_id for source in sources}
    for source_id, source in sorted(by_id.items()):
        parts = relative_package_parts(source_id)
        if len(parts) != 1:
            raise InvalidArgumentsError("NotebookLM source UUID must be a single safe output-folder name.")
        for action in actions:
            selected = [artifact for artifact in grouped[source_id] if artifact.action == action]
            if action == "create_podcasts" or not selected:
                if source_id not in actual_ids:
                    raise InputDocumentError(
                        f"NotebookLM {action} needs the original Sources document for source UUID '{source_id}'. "
                        "Include the exported Sources folder to generate missing artifacts."
                    )
                tasks.append(NotebookLMTask("source", source, action))
            else:
                tasks.extend(NotebookLMTask(classify_artifact_mode(artifact), source, action, artifact)
                             for artifact in selected)
    if not tasks:
        raise InputDocumentError("No NotebookLM source documents or requested artifacts were found.")
    return tasks


def _generation_jobs(directory: Path, *, contained: bool = False,
                     max_jobs: int | None = None) -> list[GenerationJob]:
    """Validate the whole batch before writing artifacts or making provider calls."""

    config_path = directory / CONFIG_NAME
    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise InputDocumentError("study set config was not found") from exc
    except (OSError, ValueError) as exc:
        raise InvalidArgumentsError(f"Could not read {CONFIG_NAME}: {exc}") from exc
    if not isinstance(config, dict):
        raise InvalidArgumentsError(f"{CONFIG_NAME} must contain a JSON object.")
    global_type = _input_type(config.get("type", "document"), "type")
    verbose = config.get("verbose", False)
    if not isinstance(verbose, bool):
        raise InvalidArgumentsError("verbose must be a boolean.")
    entries = config.get("files")
    if not isinstance(entries, list) or not entries:
        raise InvalidArgumentsError("files must be a nonempty list.")
    if max_jobs is not None and len(entries) > max_jobs:
        raise InvalidArgumentsError(f"Study-set packages may contain at most {max_jobs} files entries.")
    # Document batches retain their model validation; NotebookLM resolves its
    # provider settings after identifying which tasks need generation.
    has_documents = any(isinstance(entry, dict) and
                        _input_type(entry.get("type", global_type), f"files[{index}].type") == "document"
                        for index, entry in enumerate(entries))
    global_model = (_text(config["model"], "model").strip()
                    if has_documents and "model" in config else None)
    global_pipeline = _entry_pipeline(config)
    pipeline_parser = argparse.ArgumentParser(add_help=False)
    add_pipeline_arguments(pipeline_parser)
    defaults = pipeline_parser.parse_args([])
    jobs: list[GenerationJob] = []
    destinations: dict[Path, tuple[Path, str]] = {}
    notebooklm_timestamp = None
    notebooklm_names: dict[tuple[Path, Path, str], str] = {}
    notebooklm_counts: dict[tuple[Path, str], int] = {}
    for index, entry in enumerate(entries):
        label = f"files[{index}]"
        if not isinstance(entry, dict):
            raise InvalidArgumentsError(f"{label} must be an object.")
        input_type = _input_type(entry.get("type", global_type), f"{label}.type")
        input_dir = _path(entry.get("input"), f"{label}.input", directory, contained=contained)
        output_dir = _path(entry.get("output"), f"{label}.output", directory, contained=contained)
        if not input_dir.is_dir():
            raise InputDocumentError(f"Input folder was not found: {input_dir}")
        patterns = []
        if input_type == "document":
            patterns = entry.get("inputPattern")
            if isinstance(patterns, str):
                patterns = [patterns]
            patterns = _strings(patterns, f"{label}.inputPattern")
            if max_jobs is not None and len(patterns) > max_jobs:
                raise InvalidArgumentsError(f"{label}.inputPattern has too many patterns.")
        actions = []
        for study_type in _strings(entry.get("types"), f"{label}.types"):
            try:
                action = normalize_study_type(study_type)
            except argparse.ArgumentTypeError as exc:
                raise InvalidArgumentsError(str(exc)) from exc
            if action not in actions:
                actions.append(action)
        formats = list(dict.fromkeys(
            item.strip().lower().lstrip(".")
            for item in _strings(entry.get("formats", ["json"]), f"{label}.formats")
        ))
        if input_type == "document":
            for action in actions:
                study_type = _CANONICAL_TYPES[action]
                for extension in formats:
                    if extension not in _FORMATS[study_type]:
                        raise InvalidArgumentsError(f"Unsupported format '{extension}' for {study_type}.")
        connector, model = "notebooklm", None
        if input_type == "document":
            connector = _text(entry.get("connector", config.get("connector", "the_connector")), f"{label}.connector")
            if connector not in CONNECTORS:
                raise InvalidArgumentsError(f"Unknown connector '{connector}'.")
            model = _text(entry.get("model", global_model), f"{label}.model").strip()
        args = copy.copy(defaults)
        settings = {**global_pipeline, **_entry_pipeline(entry, label)}
        for name, value in settings.items():
            setattr(args, name, value)
        for name in ("work_dir", "model_profile"):
            if getattr(args, name) is not None:
                setattr(args, name, _path(getattr(args, name), name, directory, contained=contained))
        args.connector, args.model, args.verbose = connector, model, verbose
        options = options_from_args(args) if input_type == "document" else None
        if contained and input_type == "document":
            from pipeline.model_profiles import get_model_profile
            try:
                get_model_profile(connector, model, options.profile_overrides)
            except (TypeError, ValueError) as exc:
                raise InvalidArgumentsError("Invalid model profile in study-set package.") from exc
        if input_type == "notebooklm":
            from notebooklm_converter import supported_notebooklm_formats

            tasks = _notebooklm_tasks(input_dir, actions, directory, contained=contained)
            for action in actions:
                selected_formats = formats if "formats" in entry else _NOTEBOOKLM_DEFAULT_FORMATS[action]
                for extension in selected_formats:
                    if "." + extension not in supported_notebooklm_formats(action):
                        raise InvalidArgumentsError(
                            f"Unsupported NotebookLM format '{extension}' for {_CANONICAL_TYPES[action]}."
                        )
            if any(task.mode != "convert" for task in tasks):
                args.connector = _text(entry.get("connector", config.get("connector", "the_connector")), f"{label}.connector")
                if args.connector not in CONNECTORS:
                    raise InvalidArgumentsError(f"Unknown connector '{args.connector}'.")
                args.model = _text(entry.get("model", config.get("model")), f"{label}.model").strip()
                generation_options = options_from_args(args)
                if contained:
                    from pipeline.model_profiles import get_model_profile
                    try:
                        get_model_profile(args.connector, args.model, generation_options.profile_overrides)
                    except (TypeError, ValueError) as exc:
                        raise InvalidArgumentsError("Invalid model profile in study-set package.") from exc
            if notebooklm_timestamp is None:
                notebooklm_timestamp = _timestamp()
            for task in tasks:
                artifact = task.artifact
                source = artifact.source if artifact is not None else task.source.path
                category = output_dir / task.source.source_id / task.action.removeprefix("create_")
                identity_source = artifact.metadata_path if artifact is not None else task.source.path
                name_key = (identity_source, category, task.action)
                if name_key not in notebooklm_names:
                    counter_key = (category, task.action)
                    count = notebooklm_counts.get(counter_key, 0)
                    notebooklm_names[name_key] = _notebooklm_filename(task.action, notebooklm_timestamp, count)
                    notebooklm_counts[counter_key] = count + 1
                task_formats = formats if "formats" in entry else _NOTEBOOKLM_DEFAULT_FORMATS[task.action]
                work_root = args.work_dir or directory / "tmp" / "study-sets"
                key = hashlib.sha256(f"{identity_source}\n{category}\n{task.action}".encode("utf-8")).hexdigest()[:12]
                for extension in task_formats:
                    output = category / f"{notebooklm_names[name_key]}.{extension}"
                    identity = (identity_source, task.action)
                    if output in destinations:
                        if destinations[output] != identity:
                            raise InvalidArgumentsError(f"Input files would overwrite the same output: {output}")
                        continue
                    destinations[output] = identity
                    job_args = copy.copy(args)
                    job_args.output_format = extension
                    job_args.work_dir = work_root / f"{task.source.source_id}_{key}" / task.action.removeprefix("create_")
                    if max_jobs is not None and len(jobs) >= max_jobs:
                        raise InvalidArgumentsError(f"Study-set package exceeds the {max_jobs} job limit.")
                    jobs.append(GenerationJob(job_args, source, task.action, output, input_type, artifact, task))
            continue
        sources: set[Path] = set()
        for pattern in patterns:
            pattern = pattern.replace("\\", "/")
            if Path(pattern).is_absolute() or ".." in pattern.split("/"):
                raise InvalidArgumentsError(f"{label}.inputPattern must stay under its input folder.")
            if contained and (":" in pattern or pattern.startswith("/")):
                raise InvalidArgumentsError(f"{label}.inputPattern must stay under its input folder.")
            try:
                sources.update(path.resolve() for path in input_dir.glob(pattern) if path.is_file())
            except (OSError, ValueError, NotImplementedError) as exc:
                raise InvalidArgumentsError(f"Could not match input pattern '{pattern}': {exc}") from exc
        if not sources:
            raise InputDocumentError(f"No input files matched {patterns!r} in {input_dir}.")
        for source in sorted(sources):
            if contained and not source.is_relative_to(input_dir):
                raise InvalidArgumentsError(f"Input file must stay under its input folder: {source}")
            for action in actions:
                study_type = _CANONICAL_TYPES[action]
                for extension in formats:
                    stem = source.stem
                    output = output_dir / stem / study_type / f"{action.removeprefix('create_')}.{extension}"
                    identity = (source, action)
                    if output in destinations:
                        if destinations[output] != identity:
                            raise InvalidArgumentsError(f"Input files would overwrite the same output: {output}")
                        continue
                    destinations[output] = identity
                    job_args = copy.copy(args)
                    job_args.output_format = extension
                    work_root = args.work_dir or directory / "tmp" / "study-sets"
                    key = hashlib.sha256(f"{source}\n{output}".encode("utf-8")).hexdigest()[:12]
                    job_args.work_dir = work_root / f"{stem}_{key}" / study_type / extension
                    if max_jobs is not None and len(jobs) >= max_jobs:
                        raise InvalidArgumentsError(f"Study-set package exceeds the {max_jobs} job limit.")
                    jobs.append(GenerationJob(job_args, source, action, output))
    # Keep originals intact even when CLI outputs are configured under input.
    notebooklm_sources = {path for job in jobs if job.notebooklm_task is not None
                          for path in job.notebooklm_task.dependencies}
    for job in jobs:
        if job.input_type == "notebooklm" and (
                job.output in notebooklm_sources or
                any(source.is_dir() and source in job.output.parents for source in notebooklm_sources)):
            raise InvalidArgumentsError("NotebookLM output must not overwrite its input export.")
    return jobs


def plan_study_sets(directory: Path, *, contained: bool = False,
                    max_jobs: int | None = None) -> list[GenerationJob]:
    """Plan the same configuration-driven jobs for the CLI and web imports."""

    return _generation_jobs(Path(directory).resolve(), contained=contained, max_jobs=max_jobs)


def generate_study_sets(directory: Path) -> int:
    """Read the current folder's config and generate every matching file/type."""

    try:
        jobs = plan_study_sets(directory)
    except ApplicationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return exc.exit_code
    except OSError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    exit_code = 0
    for job in jobs:
        if job.input_type == "notebooklm":
            from diagnostic_logging import verbose_logging
            from notebooklm_workflow import execute_notebooklm_task
            try:
                with verbose_logging(job.args.verbose):
                    outcome = execute_notebooklm_task(
                        job.notebooklm_task, job.output, connector_name=job.args.connector,
                        model=job.args.model,
                        options=options_from_args(job.args) if job.notebooklm_task.mode != "convert" else None,
                    )
                result = ((outcome.metrics.get("failure_exit_code", 1) or 1)
                          if outcome.validation.get("partial") else 0)
                if outcome.validation.get("partial"):
                    print(f"Error: generation incomplete; partial output retained at {job.output}.", file=sys.stderr)
            except ApplicationError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                result = exc.exit_code
            except OSError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                result = 1
        else:
            result = run_cli_generation(job.args, action=job.action, input_path=job.source, output_path=job.output)
        if result == 0:
            print(job.output)
        elif exit_code == 0:
            exit_code = result
    return exit_code


def run_prompt() -> int:
    """Run the supported commands without invoking a system shell."""

    print("Study set prompt. Commands: ls, cd <path>, generate, exit. Tab completes commands and directories.")
    read_command = _command_reader()
    while True:
        try:
            line = read_command(f"{Path.cwd()}> ").strip()
            if not line:
                continue
            parts = line.split(maxsplit=1)
            command = parts[0].lower()
            argument = parts[1].strip() if len(parts) == 2 else ""
            if command not in PROMPT_COMMANDS:
                raise InvalidArgumentsError(f"Unknown command '{parts[0]}'. Supported commands: {', '.join(PROMPT_COMMANDS)}.")
            if command != "cd" and argument:
                raise InvalidArgumentsError(f"{command} does not accept arguments.")
            if command == "exit":
                return 0
            if command == "ls":
                for path in sorted(Path.cwd().iterdir(), key=lambda item: item.name.casefold()):
                    print(path.name + ("/" if path.is_dir() else ""))
            elif command == "cd":
                if not argument:
                    raise InvalidArgumentsError("Usage: cd <path>")
                if argument.startswith(('"', "'")):
                    if len(argument) < 2 or argument[-1] != argument[0]:
                        raise InvalidArgumentsError("cd path has an unmatched quote.")
                    argument = argument[1:-1]
                if not argument:
                    raise InvalidArgumentsError("Usage: cd <path>")
                os.chdir(Path(argument).expanduser())
            elif command == "generate":
                generate_study_sets(Path.cwd())
        except EOFError:
            return 0
        except KeyboardInterrupt:
            print("\nInterrupted. Use exit to close the prompt.")
        except (ApplicationError, OSError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open the study-set prompt (ls, cd, generate, exit).")
    parser.parse_args(argv)
    return run_prompt()


if __name__ == "__main__":
    raise SystemExit(main())
