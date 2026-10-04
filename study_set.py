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
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from app_config import ACTION_CONFIG
from cli_runtime import add_pipeline_arguments, options_from_args, run_cli_generation
from connectors import CONNECTORS
from errors import ApplicationError, InputDocumentError, InvalidArgumentsError
from package_paths import relative_package_parts


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


@dataclass
class GenerationJob:
    args: argparse.Namespace
    source: Path
    action: str
    output: Path


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
    global_model = _text(config["model"], "model").strip() if "model" in config else None
    verbose = config.get("verbose", False)
    if not isinstance(verbose, bool):
        raise InvalidArgumentsError("verbose must be a boolean.")
    entries = config.get("files")
    if not isinstance(entries, list) or not entries:
        raise InvalidArgumentsError("files must be a nonempty list.")
    if max_jobs is not None and len(entries) > max_jobs:
        raise InvalidArgumentsError(f"Study-set packages may contain at most {max_jobs} files entries.")
    global_pipeline = _entry_pipeline(config)
    pipeline_parser = argparse.ArgumentParser(add_help=False)
    add_pipeline_arguments(pipeline_parser)
    defaults = pipeline_parser.parse_args([])
    jobs: list[GenerationJob] = []
    destinations: dict[Path, tuple[Path, str]] = {}
    for index, entry in enumerate(entries):
        label = f"files[{index}]"
        if not isinstance(entry, dict):
            raise InvalidArgumentsError(f"{label} must be an object.")
        input_dir = _path(entry.get("input"), f"{label}.input", directory, contained=contained)
        output_dir = _path(entry.get("output"), f"{label}.output", directory, contained=contained)
        if not input_dir.is_dir():
            raise InputDocumentError(f"Input folder was not found: {input_dir}")
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
        for action in actions:
            study_type = _CANONICAL_TYPES[action]
            for extension in formats:
                if extension not in _FORMATS[study_type]:
                    raise InvalidArgumentsError(f"Unsupported format '{extension}' for {study_type}.")
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
        options = options_from_args(args)
        if contained:
            from pipeline.model_profiles import get_model_profile
            try:
                get_model_profile(connector, model, options.profile_overrides)
            except (TypeError, ValueError) as exc:
                raise InvalidArgumentsError("Invalid model profile in study-set package.") from exc
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
                    output = output_dir / source.stem / study_type / f"{action.removeprefix('create_')}.{extension}"
                    identity = (source, action)
                    if output in destinations:
                        if destinations[output] != identity:
                            raise InvalidArgumentsError(f"Input files would overwrite the same output: {output}")
                        continue
                    destinations[output] = identity
                    job_args = copy.copy(args)
                    work_root = args.work_dir or directory / "tmp" / "study-sets"
                    key = hashlib.sha256(f"{source}\n{output}".encode("utf-8")).hexdigest()[:12]
                    job_args.work_dir = work_root / f"{source.stem}_{key}" / study_type / extension
                    if max_jobs is not None and len(jobs) >= max_jobs:
                        raise InvalidArgumentsError(f"Study-set package exceeds the {max_jobs} job limit.")
                    jobs.append(GenerationJob(job_args, source, action, output))
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
