#!/usr/bin/env python3
"""Checkpoint-aware batch runner for every configured learning-set action.

``main.py`` reads provider settings from ``config.yaml``, resolves ``${ENV_VAR}``
placeholders from the process environment, and invokes ``orchestrator.py`` once per
unfinished input/action pair. A source is marked complete only after every action exits
successfully.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from app_config import ACTION_CONFIG
from connectors import CONNECTORS
from document_loader import SUPPORTED_EXTENSIONS

LOGGER = logging.getLogger("batch_generator")
_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class BatchConfigurationError(ValueError):
    """Raised when the batch configuration or checkpoint is unusable."""


def resolve_environment_placeholders(value: Any, environment: Mapping[str, str]) -> Any:
    """Recursively replace ``${NAME}`` strings with values from ``environment``.

    Missing variables are configuration errors rather than empty substitutions so a
    provider request is never made with an accidentally missing credential.
    """

    if isinstance(value, dict):
        return {
            key: resolve_environment_placeholders(item, environment)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [resolve_environment_placeholders(item, environment) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        variable_name = match.group(1)
        try:
            return environment[variable_name]
        except KeyError as exc:
            raise BatchConfigurationError(
                f"Environment variable '{variable_name}' is required by config.yaml."
            ) from exc

    return _ENV_PLACEHOLDER.sub(replace, value)


def load_config(path: Path) -> dict[str, Any]:
    """Read YAML configuration without resolving unused provider credentials."""

    if not path.is_file():
        raise BatchConfigurationError(f"Configuration file does not exist: {path}")
    try:
        import yaml
    except ImportError as exc:
        raise BatchConfigurationError(
            "YAML configuration requires PyYAML. Run: pip install -r requirements.txt"
        ) from exc

    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BatchConfigurationError(f"Could not parse configuration '{path}': {exc}") from exc
    if not isinstance(parsed, dict):
        raise BatchConfigurationError("config.yaml must contain a top-level mapping.")
    return parsed


def configured_provider(config: Mapping[str, Any]) -> str:
    """Get and validate the default provider from either supported config spelling."""

    provider_section = config.get("provider")
    value: Any = None
    if isinstance(provider_section, Mapping):
        value = provider_section.get("default")
    # Support the user's prose spelling, ``default.provider``, as well.
    if value is None:
        default_section = config.get("default")
        if isinstance(default_section, Mapping):
            value = default_section.get("provider")
    if not isinstance(value, str) or not value.strip():
        raise BatchConfigurationError(
            "config.yaml must define provider.default, for example: provider: {default: ollama}."
        )

    provider = value.strip().lower()
    if provider not in CONNECTORS:
        supported = ", ".join(sorted(CONNECTORS))
        raise BatchConfigurationError(
            f"Unsupported default provider '{provider}'. Supported providers: {supported}."
        )
    return provider


def connector_environment(config: Mapping[str, Any], provider: str) -> dict[str, str]:
    """Translate the selected provider's YAML settings into connector variables."""

    config_section_name = "anthropic" if provider in {"anthropic", "claude"} else provider
    section = config.get(config_section_name, {})
    if not isinstance(section, Mapping):
        raise BatchConfigurationError(
            f"The '{config_section_name}' configuration section must be a mapping."
        )

    variable_maps: dict[str, dict[str, str]] = {
        "openai": {"api_key": "OPENAI_API_KEY", "model": "OPENAI_MODEL"},
        "anthropic": {"api_key": "ANTHROPIC_API_KEY", "model": "ANTHROPIC_MODEL"},
        "claude": {"api_key": "ANTHROPIC_API_KEY", "model": "ANTHROPIC_MODEL"},
        "openrouter": {
            "api_key": "OPENROUTER_API_KEY",
            "model": "OPENROUTER_MODEL",
            "url": "OPENROUTER_BASE_URL",
        },
        "ollama": {
            "url": "OLLAMA_BASE_URL",
            "model": "OLLAMA_MODEL",
            "timeout": "OLLAMA_TIMEOUT",
        },
    }
    exported: dict[str, str] = {}
    for config_key, environment_key in variable_maps[provider].items():
        value = section.get(config_key)
        if value is None:
            continue
        if not isinstance(value, (str, int, float)):
            raise BatchConfigurationError(
                f"{config_section_name}.{config_key} must be a scalar value."
            )
        exported[environment_key] = str(value)
    return exported


def load_checkpoint(path: Path) -> dict[str, Any]:
    """Load the small checkpoint format, accepting an absent checkpoint as empty."""

    if not path.exists():
        return {"completed": [], "in_progress": []}
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BatchConfigurationError(f"Could not parse checkpoint '{path}': {exc}") from exc
    if not isinstance(checkpoint, dict):
        raise BatchConfigurationError("The checkpoint must contain a JSON object.")
    for field in ("completed", "in_progress"):
        value = checkpoint.setdefault(field, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise BatchConfigurationError(f"Checkpoint field '{field}' must be a list of strings.")
    return checkpoint


def save_checkpoint(path: Path, checkpoint: Mapping[str, Any]) -> None:
    """Atomically write checkpoint JSON so interrupted runs retain valid state."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary_path.write_text(
            json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="",
        )
        temporary_path.replace(path)
    except OSError as exc:
        raise BatchConfigurationError(f"Could not save checkpoint '{path}': {exc}") from exc


def checkpoint_identifiers(source: Path, input_directory: Path, project_root: Path) -> set[str]:
    """Return compatible basename, input-relative, and project-relative checkpoint keys."""

    identifiers = {source.name}
    try:
        identifiers.add(source.relative_to(input_directory).as_posix())
    except ValueError:
        pass
    try:
        identifiers.add(source.relative_to(project_root).as_posix())
    except ValueError:
        pass
    return identifiers


def discover_unfinished_inputs(
    input_directory: Path, checkpoint: Mapping[str, Any], project_root: Path
) -> list[Path]:
    """Find supported source files not listed as completed in the checkpoint."""

    if not input_directory.is_dir():
        raise BatchConfigurationError(f"Input directory does not exist: {input_directory}")
    completed = set(checkpoint.get("completed", []))
    candidates = sorted(
        (
            path
            for path in input_directory.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        ),
        key=lambda path: path.as_posix().lower(),
    )
    return [
        source
        for source in candidates
        if not checkpoint_identifiers(source, input_directory, project_root) & completed
    ]


def run_timestamp() -> str:
    """Return a timestamp shared by all actions for one source dataset."""

    return datetime.now().strftime("%Y%m%d%H%M%S")


def output_path_for(output_directory: Path, action: str, timestamp: str) -> Path:
    """Build a collision-safe explicit output path for one source/action pair."""

    return output_directory / f"output_{action}_{timestamp}.txt"


def run_batch(
    *,
    config: Mapping[str, Any],
    checkpoint_path: Path,
    input_directory: Path,
    output_directory: Path,
    actions: Sequence[str],
    dry_run: bool = False,
) -> int:
    """Run all requested actions for unfinished source documents.

    Returns zero when every selected source completed successfully, otherwise the first
    failing subprocess exit code (or one for an abnormal termination).
    """

    project_root = Path(__file__).resolve().parent
    provider_config = dict(config)
    for provider_key in ("provider", "default"):
        value = provider_config.get(provider_key)
        if value is not None:
            provider_config[provider_key] = resolve_environment_placeholders(value, os.environ)
    provider = configured_provider(provider_config)

    config_section_name = "anthropic" if provider in {"anthropic", "claude"} else provider
    resolved_config = dict(config)
    selected_section = resolved_config.get(config_section_name, {})
    resolved_config[config_section_name] = resolve_environment_placeholders(
        selected_section, os.environ
    )
    child_environment = os.environ.copy()
    child_environment.update(connector_environment(resolved_config, provider))
    checkpoint = load_checkpoint(checkpoint_path)
    sources = discover_unfinished_inputs(input_directory, checkpoint, project_root)

    if not sources:
        LOGGER.info("No unfinished supported input files found.")
        return 0

    for source in sources:
        source_key = source.name
        in_progress = checkpoint["in_progress"]
        if not dry_run:
            if source_key not in in_progress:
                in_progress.append(source_key)
            save_checkpoint(checkpoint_path, checkpoint)

        timestamp = run_timestamp()
        for action in actions:
            output_path = output_path_for(output_directory, action, timestamp)
            command = [
                sys.executable,
                str(project_root / "orchestrator.py"),
                "--connector",
                provider,
                "--input",
                str(source),
                "--action",
                action,
                "--output",
                str(output_path),
            ]
            LOGGER.info("Running %s for %s -> %s", action, source.name, output_path)
            if dry_run:
                LOGGER.info("Dry run: %s", subprocess.list2cmdline(command))
                continue
            try:
                completed_process = subprocess.run(command, env=child_environment, check=False)
            except OSError as exc:
                LOGGER.error("Could not start %s for %s: %s", action, source.name, exc)
                return 1
            if completed_process.returncode != 0:
                LOGGER.error(
                    "%s failed for %s with exit code %s.",
                    action,
                    source.name,
                    completed_process.returncode,
                )
                return completed_process.returncode or 1

        if dry_run:
            continue
        checkpoint["in_progress"] = [item for item in checkpoint["in_progress"] if item != source_key]
        if source_key not in checkpoint["completed"]:
            checkpoint["completed"].append(source_key)
        save_checkpoint(checkpoint_path, checkpoint)
        LOGGER.info("Completed every action for %s", source.name)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the batch-runner command-line parser."""

    parser = argparse.ArgumentParser(
        description="Run every learning-set action for unfinished input files."
    )
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--checkpoint", type=Path, default=Path(".checkpoint.json"))
    parser.add_argument("--input-dir", type=Path, default=Path("input"))
    parser.add_argument("--output-dir", type=Path, default=Path("tmp"))
    parser.add_argument(
        "--actions",
        nargs="+",
        choices=sorted(ACTION_CONFIG),
        default=sorted(ACTION_CONFIG),
        help="Optional subset of actions; defaults to all actions.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print commands without invoking them.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Load configuration and run the batch process."""

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        return run_batch(
            config=config,
            checkpoint_path=args.checkpoint,
            input_directory=args.input_dir,
            output_directory=args.output_dir,
            actions=args.actions,
            dry_run=args.dry_run,
        )
    except BatchConfigurationError as exc:
        LOGGER.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        LOGGER.error("Batch generation interrupted.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
