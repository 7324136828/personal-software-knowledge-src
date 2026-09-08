"""Shared orchestration used by the main CLI and direct action CLIs."""

from __future__ import annotations

import argparse
import importlib
import logging
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

from app_config import ACTION_CONFIG
from connectors import CONNECTORS, create_connector
from document_loader import load_document
from errors import ApplicationError, InvalidArgumentsError
from output_writer import write_output
from skill_loader import load_skill

LOGGER = logging.getLogger("content_generator")


class GenerateFunction(Protocol):
    """Signature implemented by each action module."""

    def __call__(
        self,
        source_text: str,
        source_path: Path,
        skill_text: str,
        connector: object,
        output_path: Path | None = None,
    ) -> str: ...


def _load_generate_function(module_name: str) -> GenerateFunction:
    try:
        module: ModuleType = importlib.import_module(module_name)
    except ImportError as exc:
        raise InvalidArgumentsError(
            f"Could not import action module '{module_name}': {exc}"
        ) from exc
    generate = getattr(module, "generate", None)
    if not callable(generate):
        raise InvalidArgumentsError(
            f"Action module '{module_name}' does not expose a callable generate()."
        )
    return cast(GenerateFunction, generate)


def execute_generation(
    *,
    connector_name: str,
    input_path: Path,
    action: str,
    output_path: Path,
    model: str | None = None,
) -> None:
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
    generator = _load_generate_function(action_config["module"])

    LOGGER.info("Generating...")
    result = generator(
        source_text,
        input_path,
        skill.text,
        connector,
        output_path,
    )
    write_output(output_path, result)
    LOGGER.info("Output written to %s", output_path)


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
