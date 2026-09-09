"""Shared orchestration used by the main CLI and direct action CLIs."""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from pathlib import Path

from app_config import ACTION_CONFIG
from connectors import CONNECTORS, create_connector
from document_loader import load_document
from errors import ApplicationError, InvalidArgumentsError
from output_writer import write_output
from pipeline.engine import PipelineOptions, PipelineResult, run_pipeline
from skill_loader import load_skill

LOGGER = logging.getLogger("content_generator")


def execute_generation(
    *,
    connector_name: str,
    input_path: Path,
    action: str,
    output_path: Path,
    model: str | None = None,
    options: PipelineOptions | None = None,
) -> PipelineResult:
    """Run one validated load, generate, and write operation."""

    try:
        action_config = ACTION_CONFIG[action]
    except KeyError as exc:
        supported = ", ".join(sorted(ACTION_CONFIG))
        raise InvalidArgumentsError(
            f"Unknown action '{action}'. Supported actions: {supported}."
        ) from exc
    if connector_name not in CONNECTORS:
        supported = ", ".join(sorted(CONNECTORS))
        raise InvalidArgumentsError(
            f"Unknown connector '{connector_name}'. Supported connectors: {supported}."
        )

    input_path = Path(input_path)
    output_path = Path(output_path)
    LOGGER.info("Connector: %s", connector_name)
    LOGGER.info("Action: %s", action)
    LOGGER.info("Input: %s", input_path)

    source_text = load_document(input_path)
    skill = load_skill(action_config["skill"])
    LOGGER.info("Skill: %s", skill.path)

    connector = create_connector(connector_name, model=model)
    LOGGER.info("Model: %s", connector.model)
    LOGGER.info("Generating...")
    result = run_pipeline(
        source_text=source_text,
        source_path=input_path,
        skill_text=skill.text,
        connector=connector,
        connector_name=connector_name,
        action=action,
        output_path=output_path,
        options=options,
    )
    write_output(output_path, result.text)
    LOGGER.info("Output written to %s", output_path)
    return result


def add_pipeline_arguments(parser: argparse.ArgumentParser) -> None:
    """Expose identical pipeline controls on every generation entry point."""

    parser.add_argument("--strategy", choices=("baseline", "chunked", "extraction_then_generation", "multi_pass"))
    parser.add_argument("--chunk-tokens", type=int, help="Desired source tokens per semantic chunk, bounded by the model profile.")
    parser.add_argument("--chunk-overlap", type=int, help="Maximum tokens of preceding context; provenance remains attached to the original chunk.")
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--aggregation", choices=("deterministic", "hierarchical"))
    parser.add_argument("--group-size", type=int)
    parser.add_argument("--retries", type=int)
    parser.add_argument("--generation-passes", type=int)
    parser.add_argument("--temperature", type=float)
    for name in ("checkpoint", "validate", "keep-raw"):
        parser.add_argument(f"--{name}", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--force", action="store_true", help="Regenerate completed stages while retaining prior raw responses.")
    parser.add_argument("--work-dir", type=Path, help="Explicit intermediate/checkpoint directory for this run.")
    parser.add_argument("--experiment", help="Apply a named experiment preset; explicit flags override it.")
    parser.add_argument("--model-profile", type=Path, help="JSON object of model-profile overrides, useful for unrecognized/local models.")
    parser.add_argument("--context-window", type=int, help="Override model context-window tokens.")
    parser.add_argument("--profile-max-output-tokens", type=int, help="Override the model's hard output-token ceiling.")


def options_from_args(args: argparse.Namespace) -> PipelineOptions:
    """Combine an optional experiment preset with explicit CLI controls."""

    from experiments.runner import options_dict, load_scenario

    values = options_dict(load_scenario(args.experiment)) if args.experiment else {}
    for name in ("strategy", "chunk_tokens", "chunk_overlap", "max_output_tokens", "aggregation", "group_size", "retries", "generation_passes", "temperature", "checkpoint", "validate", "keep_raw", "work_dir"):
        value = getattr(args, name, None)
        if value is not None:
            values[name] = value
    values["force"] = args.force
    overrides = dict(values.get("profile_overrides", {}))
    if args.model_profile:
        try:
            supplied = json.loads(args.model_profile.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InvalidArgumentsError(f"Could not read model profile '{args.model_profile}': {exc}") from exc
        if not isinstance(supplied, dict):
            raise InvalidArgumentsError("--model-profile must contain a JSON object.")
        overrides.update(supplied)
    if args.context_window is not None:
        overrides["context_window"] = args.context_window
        overrides.setdefault("safety_margin", min(512, max(0, args.context_window // 10)))
    if args.profile_max_output_tokens is not None:
        overrides["max_output_tokens"] = args.profile_max_output_tokens
    values["profile_overrides"] = overrides
    return PipelineOptions(**values)


def configure_logging() -> None:
    """Configure concise command-line status logging once."""

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")


def build_action_parser(action: str) -> argparse.ArgumentParser:
    """Create the shared parser for a directly executable action module."""

    parser = argparse.ArgumentParser(
        description=f"Run {action} using an interchangeable LLM connector."
    )
    parser.add_argument("--connector", required=True, choices=sorted(CONNECTORS))
    parser.add_argument("--input", required=True, type=Path, dest="input_path")
    parser.add_argument("--output", required=True, type=Path, dest="output_path")
    parser.add_argument("--model", help="Override the connector's configured model.")
    add_pipeline_arguments(parser)
    return parser


def run_action_cli(action: str, argv: Sequence[str] | None = None) -> int:
    """Run one action's standalone CLI without duplicating orchestration logic."""

    configure_logging()
    parser = build_action_parser(action)
    args = parser.parse_args(argv)
    try:
        execute_generation(
            connector_name=args.connector,
            input_path=args.input_path,
            action=action,
            output_path=args.output_path,
            model=args.model,
            options=options_from_args(args),
        )
    except ApplicationError as exc:
        LOGGER.error("%s", exc)
        return exc.exit_code
    except KeyboardInterrupt:
        LOGGER.error("Generation interrupted.")
        return 1
    except Exception:
        LOGGER.exception("Unexpected application failure")
        return 1
    return 0
