"""Configuration, isolated runs, and comparisons without provider-specific code."""

from __future__ import annotations

import copy
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app_config import ACTION_CONFIG
from connectors import CONNECTORS
from errors import InvalidArgumentsError

PACKAGE_ROOT = Path(__file__).resolve().parent
SCENARIOS = (
    "baseline", "chunked", "chunked_with_overlap", "hierarchical_aggregation",
    "extraction_then_generation", "token_optimized", "truncation_recovery",
    "multi_pass", "model_comparison",
)
LOGGER = logging.getLogger("content_generator.experiments")


def read_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InvalidArgumentsError(f"Could not read experiment configuration '{path}': {exc}") from exc
    if not isinstance(value, dict):
        raise InvalidArgumentsError(f"Experiment configuration '{path}' must be a JSON object.")
    return value


def load_scenario(name: str) -> dict[str, Any]:
    if name not in SCENARIOS:
        raise InvalidArgumentsError(f"Unknown experiment '{name}'. Choose from: {', '.join(SCENARIOS)}.")
    return read_config(PACKAGE_ROOT / "configs" / f"{name}.json")


def parse_model_spec(value: str) -> tuple[str, str]:
    """Split once, retaining colons inside model names such as llama3.2:3b."""
    connector, separator, model = value.partition(":")
    if not separator or not model.strip() or connector not in CONNECTORS:
        raise InvalidArgumentsError(f"Invalid model '{value}'; expected supported-provider:model (for example ollama:llama3.2:3b).")
    return connector, model


def options_dict(config: dict[str, Any]) -> dict[str, Any]:
    """Translate the documented JSON format into provider-neutral pipeline options."""
    chunking = config.get("chunking", {})
    generation = config.get("generation", {})
    aggregation = config.get("aggregation", {})
    for name, value in (("chunking", chunking), ("generation", generation), ("aggregation", aggregation)):
        if not isinstance(value, dict):
            raise InvalidArgumentsError(f"Experiment '{name}' must be an object.")
    values = {
        "strategy": config.get("strategy", "multi_pass"),
        "chunk_tokens": chunking.get("target_tokens"),
        "chunk_overlap": chunking.get("overlap_tokens", 0),
        "max_output_tokens": generation.get("max_output_tokens"),
        "temperature": generation.get("temperature"),
        "retries": generation.get("retries"),
        "generation_passes": generation.get("passes", 4),
        "aggregation": aggregation.get("strategy", "hierarchical"),
        "group_size": aggregation.get("group_size", 4),
        "checkpoint": config.get("checkpoint", True),
        "validate": config.get("validate", True),
        "keep_raw": config.get("keep_raw", True),
        "profile_overrides": config.get("profile_overrides", {}),
    }
    if chunking.get("strategy", "semantic") != "semantic":
        raise InvalidArgumentsError("Only semantic chunking (with token fallback) is supported.")
    return values


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")[:80] or "run"


def prepare_config(config: dict[str, Any]) -> dict[str, Any]:
    resolved = copy.deepcopy(config)
    if resolved.get("connector") not in CONNECTORS:
        raise InvalidArgumentsError("An experiment must specify a supported connector.")
    if resolved.get("action") not in ACTION_CONFIG:
        raise InvalidArgumentsError("An experiment must specify a supported action.")
    if not resolved.get("source_file"):
        raise InvalidArgumentsError("An experiment requires --input or source_file in its configuration.")
    if not isinstance(resolved.get("model"), str) or not resolved["model"].strip():
        raise InvalidArgumentsError("An experiment requires an explicit model to make comparisons reproducible.")
    resolved["source_file"] = str(Path(resolved["source_file"]).resolve())
    options_dict(resolved)
    return resolved


def evaluate_criteria(validation: dict[str, Any], metrics: dict[str, Any], criteria: dict[str, Any]) -> dict[str, Any]:
    """Evaluate configured thresholds; missing measurements are never counted as passes."""
    observed = {**metrics, **validation}
    checks: dict[str, Any] = {}
    mapping = {
        "min_coverage": ("coverage", "min"),
        "min_exercise_coverage": ("exercise_coverage", "min"),
        "require_schema_valid": ("schema_valid", "equal"),
        "max_truncated_outputs": ("truncated_outputs", "max"),
        "max_duplicate_ratio": ("duplicate_ratio", "max"),
        "max_latex_errors": ("latex_errors", "max"),
    }
    for criterion, threshold in criteria.items():
        if criterion not in mapping:
            checks[criterion] = {"passed": False, "reason": "Unknown validation criterion"}
            continue
        metric, operator = mapping[criterion]
        actual = observed.get(metric)
        passed = False
        if actual is not None:
            try:
                passed = actual == threshold if operator == "equal" else actual >= threshold if operator == "min" else actual <= threshold
            except TypeError:
                pass
        checks[criterion] = {"metric": metric, "observed": actual, "required": threshold, "passed": passed}
    return {"passed": all(item["passed"] for item in checks.values()), "checks": checks}


def run_experiment(
    config: dict[str, Any],
    *,
    results_dir: Path = PACKAGE_ROOT / "results",
    resume: Path | None = None,
    force: bool = False,
) -> Path:
    """Run one scenario, preserving diagnostics even after provider/validation failures."""
    from cli_runtime import execute_generation
    from pipeline.engine import PipelineOptions

    config = prepare_config(config)
    if resume is not None:
        run_dir = Path(resume).resolve()
        prior = read_config(run_dir / "config.json")
        if prior != config:
            raise InvalidArgumentsError("Resume configuration differs from the saved run. Start a new run to compare different settings.")
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        run_id = f"{stamp}_{_slug(config.get('name', 'custom'))}_{_slug(config['connector'] + '_' + config['model'])}_{uuid.uuid4().hex[:8]}"
        run_dir = Path(results_dir).resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json(run_dir / "config.json", config)
    suffix = config.get("output_extension", ".json")
    if suffix not in {".json", ".md", ".txt", ".csv", ".mmd", ".html", ".svg"}:
        raise InvalidArgumentsError("Unsupported experiment output_extension.")
    output = run_dir / f"aggregate{suffix}"
    options = PipelineOptions(**options_dict(config), work_dir=run_dir / "pipeline", force=force)
    file_handler = logging.FileHandler(run_dir / "generation.log", encoding="utf-8", mode="a")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(file_handler)
    started = time.perf_counter()
    metrics: dict[str, Any] = {}
    validation: dict[str, Any] = {}
    failure: Exception | None = None
    try:
        LOGGER.info("Starting %s with %s:%s; output=%s", config.get("name"), config["connector"], config["model"], output)
        result = execute_generation(
            connector_name=config["connector"], model=config["model"],
            input_path=Path(config["source_file"]), action=config["action"],
            output_path=output, options=options,
        )
        metrics = dict(result.metrics)
        validation = dict(result.validation)
        metrics.setdefault("status", "complete" if validation.get("passed", validation.get("valid", True)) else "validation_failed")
    except Exception as exc:
        failure = exc
        LOGGER.exception("Experiment failed")
        result = getattr(exc, "result", None)
        if result is not None:
            metrics, validation = dict(result.metrics), dict(result.validation)
            if getattr(result, "text", None):
                output.write_text(result.text, encoding="utf-8")
        else:
            # A failed stage may still have persisted useful partial diagnostics.
            for filename, destination in (("metrics.json", metrics), ("validation.json", validation)):
                path = run_dir / "pipeline" / filename
                if path.exists():
                    try:
                        destination.update(read_config(path))
                    except InvalidArgumentsError:
                        pass
        metrics.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        validation.setdefault("valid", False)
        validation.setdefault("passed", False)
    finally:
        metrics.setdefault("runtime_seconds", time.perf_counter() - started)
        metrics["invocation_runtime_seconds"] = time.perf_counter() - started
        metrics.setdefault("estimated_cost", None)
        metrics.setdefault("cost", None)
        metrics["scenario"] = config.get("name", "custom")
        metrics["connector"] = config["connector"]
        metrics["model"] = config["model"]
        metrics["output_bytes"] = output.stat().st_size if output.exists() else 0
        validation["criteria"] = evaluate_criteria(validation, metrics, config.get("validation_criteria", {}))
        if metrics.get("status") == "complete" and not validation["criteria"]["passed"]:
            metrics["status"] = "validation_failed"
        _write_json(run_dir / "metrics.json", metrics)
        _write_json(run_dir / "validation.json", validation)
        LOGGER.info("Run status=%s; diagnostics=%s", metrics.get("status"), run_dir)
        root_logger.removeHandler(file_handler)
        file_handler.close()
    if failure is not None:
        # Failed runs remain comparable; the CLI uses status to return nonzero.
        LOGGER.error("Failure recorded in %s: %s", run_dir, failure)
    return run_dir


def compare_runs(results_dir: Path) -> str:
    """Render a Markdown table from measured results, keeping unknowns explicit."""
    columns = ["Scenario", "Model", "Status", "Coverage", "Exercises", "Truncated", "Duplication", "Tokens", "Cost", "Seconds"]
    rows = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    found = 0
    for path in sorted(Path(results_dir).rglob("metrics.json")):
        if not (path.parent / "config.json").is_file():
            continue
        try:
            config = read_config(path.parent / "config.json")
            metrics = read_config(path)
            validation_path = path.parent / "validation.json"
            validation = read_config(validation_path) if validation_path.exists() else {}
        except InvalidArgumentsError as exc:
            LOGGER.warning("Skipping unreadable run: %s", exc)
            continue
        measured = {**metrics, **validation}
        def percentage(key: str) -> str:
            value = measured.get(key)
            return f"{value:.1%}" if isinstance(value, (int, float)) else "n/a"
        def numeric(value: Any, digits: int = 2) -> str:
            return f"{value:.{digits}f}" if isinstance(value, (int, float)) else "n/a"
        total_tokens = None
        if isinstance(metrics.get("input_tokens"), (int, float)) and isinstance(metrics.get("output_tokens"), (int, float)):
            total_tokens = metrics["input_tokens"] + metrics["output_tokens"]
        cells = [
            config.get("name", "custom"), f"{config.get('connector')}:{config.get('model')}",
            metrics.get("status", "unknown"), percentage("coverage"), percentage("exercise_coverage"),
            str(measured.get("truncated_outputs", "n/a")), percentage("duplicate_ratio"),
            numeric(total_tokens, 0), numeric(metrics.get("estimated_cost", metrics.get("cost")), 4),
            numeric(metrics.get("runtime_seconds")),
        ]
        rows.append("| " + " | ".join(str(cell).replace("|", "\\|").replace("\n", " ") for cell in cells) + " |")
        found += 1
    if not found:
        return f"No experiment runs found under {results_dir}."
    return "\n".join(rows) + "\n\nCoverage/grounding are heuristic measurements; n/a means unavailable, not zero. Costs are estimated only when pricing is supplied."
