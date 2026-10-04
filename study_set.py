#!/usr/bin/env python3
"""Generate one study artifact from a file, with optional verbose diagnostics."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from app_config import ACTION_CONFIG
from cli_runtime import add_pipeline_arguments, run_cli_generation
from connectors import CONNECTORS


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


def normalize_study_type(value: str) -> str:
    """Accept friendly artifact names and the existing create_* action names."""

    try:
        return _TYPE_ALIASES[value.strip().lower()]
    except KeyError as exc:
        raise argparse.ArgumentTypeError("Unknown study-set type. Choose: " + ", ".join(STUDY_TYPES)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a study artifact from a source file. Defaults to The Connector.",
        epilog='Examples: study_set.py "input/book.txt" podcast -v; study_set.py -i book.pdf -t quiz -o quiz.json',
    )
    parser.add_argument("input_path", nargs="?", type=Path, help="Source file (or use --input).")
    parser.add_argument("study_type", nargs="?", type=normalize_study_type,
                        help="Artifact type: " + ", ".join(STUDY_TYPES) + " (plural and create_* names also accepted).")
    parser.add_argument("-i", "--input", type=Path, dest="input_option", help="Source file instead of the positional file.")
    parser.add_argument("-t", "--type", "--study-set-type", type=normalize_study_type, dest="type_option",
                        help="Study-set type instead of the positional type.")
    parser.add_argument("-o", "--output", type=Path, dest="output_path",
                        help="Exact result filename; defaults to output/<source-name>/<type>.json.")
    parser.add_argument("--connector", default="the_connector", choices=sorted(CONNECTORS))
    parser.add_argument("--model", help="Model or active library configuration ID; otherwise use the connector default.")
    add_pipeline_arguments(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.input_option is not None and args.input_path is not None and args.study_type is None and args.type_option is None:
        # With --input, a sole positional argument names the artifact type.
        try:
            args.study_type = normalize_study_type(str(args.input_path))
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))
        args.input_path = None
    if args.input_path is not None and args.input_option is not None:
        parser.error("Specify the source file once, either positionally or with --input.")
    if args.study_type is not None and args.type_option is not None:
        parser.error("Specify the study-set type once, either positionally or with --type.")
    source = args.input_option if args.input_option is not None else args.input_path
    action = args.type_option if args.type_option is not None else args.study_type
    if source is None or action is None:
        parser.error("A source file and study-set type are required.")
    output = (args.output_path if args.output_path is not None else
              Path("output") / source.stem / (action.removeprefix("create_") + ".json"))
    exit_code = run_cli_generation(args, action=action, input_path=source, output_path=output)
    if exit_code == 0:
        print(output.resolve())
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
