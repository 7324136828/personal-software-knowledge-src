#!/usr/bin/env python3
"""Run and compare reproducible generation experiments."""

from __future__ import annotations

import argparse
import copy
import json
import logging
from collections.abc import Sequence
from pathlib import Path

from app_config import ACTION_CONFIG
from cli_runtime import configure_logging
from connectors import CONNECTORS
from errors import ApplicationError, InvalidArgumentsError
from experiments.runner import PACKAGE_ROOT, SCENARIOS, compare_runs, load_scenario, parse_model_spec, prepare_config, read_config, run_experiment
from runtime_environment import load_environment

LOGGER = logging.getLogger("content_generator.experiments")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--list", action="store_true", help="List the nine built-in scenarios.")
    modes.add_argument("--compare", type=Path, metavar="RESULTS_DIRECTORY")
    parser.add_argument("--scenario", choices=SCENARIOS)
    parser.add_argument("--config", type=Path, help="Use a custom JSON scenario configuration.")
    parser.add_argument("--connector", choices=sorted(CONNECTORS))
    parser.add_argument("--model")
    parser.add_argument("--models", nargs="+", help="Provider:model pairs; model names may themselves contain colons.")
    parser.add_argument("--input", type=Path, dest="input_path")
    parser.add_argument("--action", choices=sorted(ACTION_CONFIG))
    parser.add_argument("--results-dir", type=Path, default=PACKAGE_ROOT / "results")
    parser.add_argument("--resume", type=Path, help="Resume one exact run directory using its saved configuration.")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print resolved configurations without creating runs or calling providers.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    load_environment()
    configure_logging()
    args = build_parser().parse_args(argv)
    if args.list:
        print("Available scenarios:\n" + "\n".join(f"{index}. {name}" for index, name in enumerate(SCENARIOS, 1)))
        return 0
    if args.compare:
        print(compare_runs(args.compare))
        return 0
    try:
        if args.resume and any((args.config, args.scenario, args.models, args.connector,
                                args.model, args.input_path, args.action)):
            raise InvalidArgumentsError("--resume uses the exact saved configuration; do not combine it with configuration overrides.")
        if args.config and args.scenario:
            raise InvalidArgumentsError("Choose either --config or --scenario.")
        if args.models and (args.connector or args.model):
            raise InvalidArgumentsError("Use --models or the single --connector/--model pair.")
        if args.resume:
            config = read_config(args.resume / "config.json")
        elif args.config:
            config = read_config(args.config)
        else:
            config = load_scenario(args.scenario or "multi_pass")
        for argument, key in (("connector", "connector"), ("model", "model"), ("input_path", "source_file"), ("action", "action")):
            value = getattr(args, argument)
            if value is not None:
                config[key] = str(value)
        model_pairs = [parse_model_spec(value) for value in args.models] if args.models else [(config.get("connector"), config.get("model"))]
        configs = []
        for connector, model in model_pairs:
            selected = copy.deepcopy(config)
            selected.update(connector=connector, model=model)
            configs.append(prepare_config(selected))
        if args.dry_run:
            print(json.dumps(configs, indent=2, ensure_ascii=False))
            return 0
        failed = False
        for config in configs:
            run_dir = run_experiment(config, results_dir=args.results_dir, resume=args.resume, force=args.force)
            print(run_dir)
            metrics = read_config(run_dir / "metrics.json")
            failed = failed or metrics.get("status") != "complete"
        return 1 if failed else 0
    except ApplicationError as exc:
        LOGGER.error("%s", exc)
        return exc.exit_code
    except KeyboardInterrupt:
        LOGGER.error("Experiment interrupted; rerun with --resume RUN_DIRECTORY.")
        return 1
    except Exception:
        LOGGER.exception("Unexpected experiment failure")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
