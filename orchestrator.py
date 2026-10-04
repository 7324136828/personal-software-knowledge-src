#!/usr/bin/env python3
"""Single command-line entry point for skill-driven content generation."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from app_config import ACTION_CONFIG
from cli_runtime import add_pipeline_arguments, run_cli_generation
from connectors import CONNECTORS


def build_parser() -> argparse.ArgumentParser:
    """Build the application argument parser."""

    parser = argparse.ArgumentParser(
        description="Generate source-grounded artifacts from local documents and skills."
    )
    parser.add_argument(
        "--connector",
        required=True,
        choices=sorted(CONNECTORS),
        help="Language-model provider.",
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        dest="input_path",
        help="Source document to load.",
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=sorted(ACTION_CONFIG),
        help="Artifact-generation action.",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        dest="output_path",
        help="Exact destination for generated content.",
    )
    parser.add_argument(
        "--model",
        help="Optional model override; otherwise use connector environment configuration.",
    )
    add_pipeline_arguments(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse command-line arguments and run the requested action."""

    args = build_parser().parse_args(argv)
    return run_cli_generation(args, action=args.action, input_path=args.input_path, output_path=args.output_path)


if __name__ == "__main__":
    raise SystemExit(main())
