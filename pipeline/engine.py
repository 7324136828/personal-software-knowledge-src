"""Provider-neutral, resumable map/validate/reduce generation pipeline.

The engine owns chunking, budgets, recovery, checkpoints, provenance and
aggregation.  Action modules remain small compatibility wrappers and connectors
only translate provider request/response formats.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from action_base import build_generation_prompts
from connectors.base import GenerationResponse, LLMConnector
from errors import InvalidArgumentsError, ProviderError

from .aggregator import hierarchical_aggregate, merge_artifacts, provenance_index
from .checkpoint import Checkpoint, atomic_json, atomic_text, digest, evidence_prefix
from .chunker import Chunk, chunk_source, source_inventory
from .extractor import EXTRACTION_SYSTEM, extraction_prompt, validate_extraction
from .model_profiles import ModelProfile, get_model_profile
from .retry import GenerationFailure, backoff, budget_rejection, retryable
from .schemas import get_schema
from .token_budget import TokenBudget, estimate_tokens
from .validator import validate_coverage, validate_output

LOGGER = logging.getLogger("content_generator.pipeline")

_STRATEGIES = {"baseline", "chunked", "extraction_then_generation", "multi_pass"}
_AGGREGATIONS = {"deterministic", "hierarchical"}
_DERIVED_EXTENSIONS = {
    "create_datatables": {".csv"},
    "create_flashcards": {".txt"},
    "create_infographics": {".md", ".html", ".svg", ".wireframe.txt"},
    "create_mindmaps": {".md", ".mmd"},
    "create_podcasts": {".md"},
    "create_qandas": set(),
    "create_quizzes": set(),
    "create_reports": {".md", ".html"},
    "create_slides": {".md"},
}


@dataclass
class PipelineOptions:
    """All provider-independent controls for one generation run."""

    strategy: str = "chunked"
    chunk_tokens: int | None = None
    chunk_overlap: int = 0
    max_output_tokens: int | None = None
    aggregation: str = "hierarchical"
    group_size: int = 4
    retries: int | None = None
    generation_passes: int = 4
    temperature: float | None = None
    checkpoint: bool = True
    force: bool = False
    validate: bool = True
    keep_raw: bool = True
    work_dir: Path | None = None
    profile_overrides: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.strategy not in _STRATEGIES:
            raise InvalidArgumentsError(f"Unknown pipeline strategy '{self.strategy}'.")
        if self.aggregation not in _AGGREGATIONS:
            raise InvalidArgumentsError(f"Unknown aggregation strategy '{self.aggregation}'.")
        for name in ("chunk_tokens", "max_output_tokens", "retries"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < (0 if name == "retries" else 1)):
                raise InvalidArgumentsError(f"{name} has an invalid value.")
        if not isinstance(self.chunk_overlap, int) or isinstance(self.chunk_overlap, bool) or self.chunk_overlap < 0:
            raise InvalidArgumentsError("chunk_overlap must be a nonnegative integer.")
        if not isinstance(self.group_size, int) or isinstance(self.group_size, bool) or self.group_size < 2:
            raise InvalidArgumentsError("group_size must be at least 2.")
        if not isinstance(self.generation_passes, int) or isinstance(self.generation_passes, bool) or not 1 <= self.generation_passes <= 5:
            raise InvalidArgumentsError("generation_passes must be between 1 and 5.")
        if self.temperature is not None and not 0 <= self.temperature <= 2:
            raise InvalidArgumentsError("temperature must be between 0 and 2.")
        for name in ("checkpoint", "force", "validate", "keep_raw"):
            if not isinstance(getattr(self, name), bool):
                raise InvalidArgumentsError(f"{name} must be a boolean.")
        if not isinstance(self.profile_overrides, dict):
            raise InvalidArgumentsError("profile_overrides must be an object.")
        if self.work_dir is not None:
            self.work_dir = Path(self.work_dir)


@dataclass
class PipelineResult:
    text: str
    metrics: dict[str, Any]
    validation: dict[str, Any]
    work_dir: Path


def _response(value: GenerationResponse | str) -> GenerationResponse:
    return value if isinstance(value, GenerationResponse) else GenerationResponse(str(value))


def _safe_error(error: Exception) -> str:
    """Avoid persisting SDK exceptions that can echo URLs, keys, or prompts."""

    if isinstance(error, GenerationFailure):
        return str(error)
    return f"{type(error).__name__}: request failed"


class _Run:
    def __init__(
        self, *, source_text: str, source_path: Path, skill_text: str,
        connector: LLMConnector, connector_name: str, action: str,
        output_path: Path, options: PipelineOptions,
    ) -> None:
        self.source_text = source_text
        self.source_path = Path(source_path)
        self.skill_text = skill_text
        self.connector = connector
        self.connector_name = connector_name
        self.action = action
        self.output_path = Path(output_path)
        self.options = options
        self.profile = get_model_profile(connector_name, connector.model, options.profile_overrides)
        self.budget = TokenBudget(self.profile)
        try:
            self.output_path.resolve().relative_to(Path.cwd().resolve())
            work_root = Path.cwd() / "tmp"
        except ValueError:
            # Library callers writing to an external temporary directory should
            # keep their diagnostics beside that output rather than polluting CWD.
            work_root = self.output_path.parent
        default_work = work_root / self.source_path.stem / action.removeprefix("create_")
        self.work_dir = (options.work_dir or default_work).resolve()
        self.chunks_dir = self.work_dir / "chunks"
        self.intermediate_dir = self.work_dir / ("artifacts" if options.strategy == "chunked" else "knowledge_and_artifacts")
        self.raw_dir = self.work_dir / "raw"
        self.aggregate_dir = self.work_dir / "aggregate"
        self.checkpoint = Checkpoint(self.work_dir / "checkpoint.json", enabled=options.checkpoint, force=options.force)
        self.started = time.perf_counter()
        self.metrics: dict[str, Any] = {
            "connector": connector_name, "model": connector.model, "strategy": options.strategy,
            "input_tokens": 0, "output_tokens": 0, "provider_calls": 0,
            "truncated_outputs": 0, "retry_count": 0, "repair_count": 0,
            "recovery_events": [], "chunk_count": 0,
        }

    @property
    def extension(self) -> str:
        return self.output_path.suffix.lower() or ".txt"

    @property
    def uses_json(self) -> bool:
        """Unknown extensions use the skill's canonical JSON representation."""

        if self.extension == ".json":
            return True
        compound = ".wireframe.txt" if self.output_path.name.lower().endswith(".wireframe.txt") else self.extension
        return compound not in _DERIVED_EXTENSIONS.get(self.action, set())

    @property
    def retries(self) -> int:
        return self.profile.retries if self.options.retries is None else self.options.retries

    def request(
        self, *, stage: str, system_prompt: str, user_prompt: str,
        json_mode: bool, requested_output: int | None = None,
    ) -> GenerationResponse:
        """Make one budgeted request and preserve append-only, secret-free evidence."""

        allowance = self.budget.output_allowance(
            system_prompt, user_prompt, requested_output or self.options.max_output_tokens
        )
        request_record = {
            "stage": stage, "connector": self.connector_name, "model": self.connector.model,
            "system_prompt": system_prompt, "user_prompt": user_prompt,
            "parameters": {"max_output_tokens": allowance,
                           "temperature": self.options.temperature if self.options.temperature is not None else self.profile.temperature,
                           "json_mode": json_mode, "context_window": self.profile.context_window},
        }
        prefix = evidence_prefix(self.raw_dir, stage) if self.options.keep_raw else None
        if prefix:
            atomic_json(prefix.with_name(prefix.name + "_request.json"), request_record)
        self.metrics["provider_calls"] += 1
        estimated_input = self.budget.input_tokens(system_prompt, user_prompt)
        try:
            if hasattr(self.connector, "generate_response"):
                generated = self.connector.generate_response(
                    system_prompt=system_prompt, user_prompt=user_prompt,
                    max_output_tokens=allowance,
                    temperature=request_record["parameters"]["temperature"],
                    json_mode=json_mode, context_window=self.profile.context_window,
                )
            else:
                # Third-party/legacy connector duck types remain compatible.
                generated = self.connector.generate(
                    system_prompt=system_prompt, user_prompt=user_prompt
                )
            result = _response(generated)
        except Exception as exc:
            if prefix:
                atomic_json(prefix.with_name(prefix.name + "_response.json"),
                            {"error_type": type(exc).__name__, "error": _safe_error(exc)})
            raise
        if prefix:
            atomic_text(prefix.with_name(prefix.name + "_response.txt"), result.text)
            atomic_json(prefix.with_name(prefix.name + "_metadata.json"), {
                "finish_reason": result.finish_reason, "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens, "metadata": result.raw_metadata,
            })
        self.metrics["input_tokens"] += result.input_tokens if result.input_tokens is not None else estimated_input
        self.metrics["output_tokens"] += result.output_tokens if result.output_tokens is not None else estimate_tokens(result.text)
        if result.truncated:
            self.metrics["truncated_outputs"] += 1
        return result

    def call_with_retries(self, **request: Any) -> GenerationResponse:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                return self.request(**request)
            except Exception as exc:
                last = exc
                if attempt >= self.retries or budget_rejection(exc) or not retryable(exc):
                    raise
                self.metrics["retry_count"] += 1
                self.metrics["recovery_events"].append({
                    "stage": request["stage"], "strategy": "provider_retry", "attempt": attempt + 1,
                    "error_type": type(exc).__name__,
                })
                backoff(attempt)
        raise last or ProviderError("Generation failed without an error.")

    def make_chunks(self) -> list[Chunk]:
        empty_system, empty_user = build_generation_prompts(
            action=self.action, artifact_name=self.action.removeprefix("create_").replace("_", " "),
            source_text="", source_path=self.source_path, skill_text=self.skill_text,
            output_path=Path("chunk.json" if self.uses_json else "chunk" + self.extension),
        )
        if self.uses_json:
            empty_user += "\nRequired canonical schema:\n" + json.dumps(get_schema(self.action), ensure_ascii=False)
        available = self.budget.chunk_allowance(empty_system, empty_user, self.options.max_output_tokens)
        overlap = self.options.chunk_overlap
        target = min(self.options.chunk_tokens or self.profile.recommended_chunk_tokens,
                     max(1, available - overlap))
        chunks = chunk_source(self.source_text, self.source_path.name, target, overlap, estimate_tokens)
        if any(chunk.oversized and chunk.estimated_input_tokens > available for chunk in chunks):
            bad = next(chunk for chunk in chunks if chunk.oversized and chunk.estimated_input_tokens > available)
            raise ProviderError(
                f"Protected {bad.atomic_kind or 'source'} block in {bad.chunk_id} needs approximately "
                f"{bad.estimated_input_tokens} tokens but only {available} fit. Use a larger verified "
                "context profile or split that source block explicitly."
            )
        self.chunks_dir.mkdir(parents=True, exist_ok=True)
        for chunk in chunks:
            atomic_text(self.chunks_dir / f"{chunk.chunk_id}.txt", chunk.text)
            metadata = chunk.to_dict()
            metadata.pop("text", None)
            metadata.pop("overlap_text", None)
            metadata["overlap_tokens"] = estimate_tokens(chunk.overlap_text)
            atomic_json(self.chunks_dir / f"{chunk.chunk_id}.json", metadata)
        atomic_json(self.work_dir / "source_inventory.json", source_inventory(self.source_text))
        self.metrics["chunk_count"] = len(chunks)
        self.metrics["target_chunk_tokens"] = target
        return chunks

    def extraction(self, chunk: Chunk) -> dict:
        key = f"{chunk.chunk_id}:extraction"
        fingerprint = digest({"text": chunk.text, "model": self.connector.model,
                              "prompt": EXTRACTION_SYSTEM, "profile": asdict(self.profile)})
        path = self.intermediate_dir / "extractions" / f"{chunk.chunk_id}.json"
        if cached := self.checkpoint.get(key, fingerprint):
            return cached
        prompt = extraction_prompt(chunk.overlap_text + chunk.text, chunk.source, chunk.section)
        errors: list[str] = []
        for attempt in range(self.retries + 1):
            response = self.call_with_retries(
                stage=f"{chunk.chunk_id}_extract_{attempt}", system_prompt=EXTRACTION_SYSTEM,
                user_prompt=prompt + ("\nPrevious validation errors:\n- " + "\n- ".join(errors) if errors else ""),
                json_mode=True,
            )
            checked = validate_extraction(response.text, chunk.overlap_text + chunk.text)
            if checked["valid"] and not response.truncated:
                data = checked["data"]
                data["_provenance"] = self.provenance(chunk, "extraction")
                self.checkpoint.save(key, fingerprint, path, data)
                return data
            errors = checked["errors"] + (["provider reported truncation"] if response.truncated else [])
            self.metrics["retry_count"] += 1
            self.metrics["recovery_events"].append({
                "stage": key, "strategy": "repair_extraction", "attempt": attempt + 1,
                "errors": errors[:8],
            })
        self.checkpoint.fail(key, fingerprint, "; ".join(errors[:8]))
        raise GenerationFailure(f"Knowledge extraction failed for {chunk.chunk_id}: {'; '.join(errors[:3])}", truncated=True)

    def provenance(self, chunk: Chunk, stage: str) -> dict[str, Any]:
        return {
            "source": chunk.source, "chunk_id": chunk.chunk_id, "sequence": chunk.sequence,
            "section": chunk.section, "start": chunk.start, "end": chunk.end,
            "model": self.connector.model, "connector": self.connector_name, "stage": stage,
            "generation_parameters": {
                "temperature": self.options.temperature if self.options.temperature is not None else self.profile.temperature,
                "max_output_tokens": self.options.max_output_tokens or self.profile.reserved_output_tokens,
            },
        }

    def generation_prompts(self, chunk: Chunk, knowledge: dict | None = None) -> tuple[str, str]:
        source = chunk.overlap_text + chunk.text
        if knowledge is not None:
            source = (
                "NORMALIZED KNOWLEDGE INVENTORY (each quote is source evidence)\n"
                + json.dumps(knowledge, ensure_ascii=False, separators=(",", ":"))
                + "\nEND KNOWLEDGE INVENTORY"
            )
        system, user = build_generation_prompts(
            action=self.action,
            artifact_name=self.action.removeprefix("create_").replace("_", " "),
            source_text=source, source_path=self.source_path, skill_text=self.skill_text,
            output_path=Path(f"{chunk.chunk_id}{'.json' if self.uses_json else self.extension}"),
        )
        user += (
            "\nCHUNK PROVENANCE\n"
            f"Chunk: {chunk.chunk_id}; sequence: {chunk.sequence}; section: {chunk.section}.\n"
            "Generate only material supported by this chunk. Include the original source filename "
            "in every schema field that asks for source IDs. Preserve exercise labels verbatim."
        )
        if self.uses_json:
            user += "\nReturn exactly one JSON object. Required canonical schema:\n" + json.dumps(get_schema(self.action), ensure_ascii=False)
        return system, user

    def repair_json(self, chunk: Chunk, current: str, errors: list[str], attempt: int) -> GenerationResponse:
        system = (
            "Repair one generated JSON artifact. Return exactly one JSON object and no fence. "
            "Preserve every correct item, formula, exercise, answer and source reference. "
            "Only fix the listed validation failures; never summarize away content."
        )
        user = (
            f"Action: {self.action}\nSource: {chunk.source}\nChunk: {chunk.chunk_id}\n"
            "Schema:\n" + json.dumps(get_schema(self.action), ensure_ascii=False) +
            "\nValidation failures:\n- " + "\n- ".join(errors[:20]) +
            "\nArtifact to repair:\n" + current
        )
        return self.call_with_retries(
            stage=f"{chunk.chunk_id}_repair_{attempt}", system_prompt=system,
            user_prompt=user, json_mode=True,
        )

    def generate_chunk(self, chunk: Chunk, knowledge: dict | None) -> dict | str:
        key = f"{chunk.chunk_id}:generation"
        fingerprint = digest({
            "text": chunk.text, "overlap": chunk.overlap_text, "knowledge": knowledge,
            "action": self.action, "skill": self.skill_text, "model": self.connector.model,
            "profile": asdict(self.profile), "extension": self.extension,
        })
        suffix = ".json" if self.uses_json else self.extension
        path = self.intermediate_dir / "generated" / f"{chunk.chunk_id}{suffix}"
        if cached := self.checkpoint.get(key, fingerprint):
            return cached["data"]
        system, user = self.generation_prompts(chunk, knowledge)
        response = self.call_with_retries(
            stage=f"{chunk.chunk_id}_generate", system_prompt=system, user_prompt=user,
            json_mode=self.uses_json,
        )
        checked = validate_output(response.text, self.action, ".json" if self.uses_json else self.extension, response.finish_reason)

        # Text continuation is safe to append. JSON is repaired as a whole because a
        # continuation fragment alone is not a valid or reliably joinable document.
        if checked["truncated"] and not self.uses_json:
            continuation = self.call_with_retries(
                stage=f"{chunk.chunk_id}_continuation", system_prompt=system,
                user_prompt=("Continue the artifact exactly where this partial output stopped. "
                             "Do not repeat earlier content. Return continuation text only.\n\nPARTIAL OUTPUT\n" + response.text),
                json_mode=False,
            )
            response = GenerationResponse(response.text + continuation.text,
                                          continuation.finish_reason,
                                          output_tokens=(response.output_tokens or 0) + (continuation.output_tokens or 0))
            checked = validate_output(response.text, self.action, self.extension, response.finish_reason)
            self.metrics["recovery_events"].append({"stage": key, "strategy": "continuation"})

        attempts = 0
        while not checked["valid"] and attempts < self.retries and self.uses_json:
            attempts += 1
            self.metrics["repair_count"] += 1
            strategy = "json_repair" if not checked["truncated"] else "regenerate_truncated_json"
            self.metrics["recovery_events"].append({"stage": key, "strategy": strategy, "attempt": attempts})
            response = self.repair_json(chunk, checked.get("text") or response.text, checked["errors"], attempts)
            checked = validate_output(response.text, self.action, ".json", response.finish_reason)
        if not checked["valid"]:
            self.checkpoint.fail(key, fingerprint, "; ".join(checked["errors"][:8]))
            raise GenerationFailure(
                f"Invalid output for {chunk.chunk_id}: {'; '.join(checked['errors'][:3])}",
                truncated=checked["truncated"],
            )
        value: dict | str = checked["data"] if self.uses_json else checked["text"]
        payload = {"data": value, "provenance": self.provenance(chunk, "generation"),
                   "repairs": checked["repairs"]}
        self.checkpoint.save(key, fingerprint, path.with_suffix(path.suffix + ".checkpoint.json"), payload)
        if self.uses_json:
            atomic_json(path, value)
        else:
            atomic_text(path, str(value))
        return value

    def coverage_repair(self, chunk: Chunk, artifact: dict) -> dict:
        """Run a bounded section-specific enrichment pass and merge without loss."""

        if self.options.generation_passes < 3 or not self.uses_json:
            return artifact
        key = f"{chunk.chunk_id}:coverage_repair"
        fingerprint = digest({"artifact": artifact, "source": chunk.text,
                              "action": self.action, "model": self.connector.model})
        path = self.intermediate_dir / "repaired" / f"{chunk.chunk_id}.json"
        if cached := self.checkpoint.get(key, fingerprint):
            return cached
        coverage = validate_coverage(chunk.text, artifact, self.action, self.source_path.name)
        missing = {
            "sections": coverage["missing_sections"],
            "exercises": coverage["missing_exercises"],
            "formulas": coverage["missing_formulas"],
        }
        if not any(missing.values()):
            self.checkpoint.save(key, fingerprint, path, artifact)
            return artifact
        system = (
            "Add only missing source-grounded material to an existing JSON artifact. Return one "
            "complete JSON object. Preserve every existing item byte-for-byte where possible, "
            "preserve LaTeX, source order, exercise labels and answers, and obey the schema."
        )
        user = (
            f"Action: {self.action}\nSchema:\n{json.dumps(get_schema(self.action), ensure_ascii=False)}\n"
            f"Missing lexical inventory:\n{json.dumps(missing, ensure_ascii=False)}\n"
            f"Source chunk:\n{chunk.text}\nExisting artifact:\n{json.dumps(artifact, ensure_ascii=False)}"
        )
        try:
            response = self.call_with_retries(
                stage=f"{chunk.chunk_id}_coverage_repair", system_prompt=system,
                user_prompt=user, json_mode=True,
            )
            checked = validate_output(response.text, self.action, ".json", response.finish_reason)
            if checked["valid"]:
                self.metrics["repair_count"] += 1
                self.metrics["recovery_events"].append({"stage": chunk.chunk_id, "strategy": "section_specific_retry"})
                merged = merge_artifacts([artifact, checked["data"]], self.action)
                if not validate_output(json.dumps(merged, ensure_ascii=False), self.action, ".json")["valid"]:
                    raise GenerationFailure("Coverage repair produced an invalid merged artifact.")
                self.checkpoint.save(key, fingerprint, path, merged)
                return merged
        except Exception as exc:
            LOGGER.warning("Coverage repair for %s was not accepted: %s", chunk.chunk_id, type(exc).__name__)
        self.checkpoint.save(key, fingerprint, path, artifact)
        return artifact

    def aggregate_text(self, values: list[str]) -> str:
        """Tree-reduce text while removing exact repeated blocks or CSV rows."""

        if len(values) == 1:
            return values[0]

        def merge(group: list[str]) -> str:
            if self.extension == ".csv":
                rows, seen = [], set()
                for index, value in enumerate(group):
                    lines = [line for line in value.splitlines() if line.strip()]
                    for number, line in enumerate(lines):
                        if index and number == 0 and rows and line == rows[0]:
                            continue
                        if line not in seen:
                            seen.add(line)
                            rows.append(line)
                return "\n".join(rows) + ("\n" if rows else "")
            blocks, seen = [], set()
            for value in group:
                for block in [part.strip() for part in value.split("\n\n") if part.strip()]:
                    normalized = " ".join(block.split())
                    if normalized not in seen:
                        seen.add(normalized)
                        blocks.append(block)
            return "\n\n".join(blocks) + ("\n" if blocks else "")

        nodes, level = values, 0
        while len(nodes) > 1:
            parents = []
            for number, start in enumerate(range(0, len(nodes), self.options.group_size), 1):
                parent = merge(nodes[start:start + self.options.group_size])
                atomic_text(self.aggregate_dir / f"level_{level:02d}_node_{number:04d}{self.extension}", parent)
                parents.append(parent)
            nodes, level = parents, level + 1
        return merge(nodes)

    def run(self) -> PipelineResult:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(self.work_dir / "run_config.json", {
            "source": str(self.source_path), "output": str(self.output_path), "action": self.action,
            "connector": self.connector_name, "model": self.connector.model,
            "options": {**asdict(self.options), "work_dir": str(self.work_dir)},
            "model_profile": asdict(self.profile),
        })

        if self.options.strategy == "baseline":
            system, user = build_generation_prompts(
                action=self.action, artifact_name=self.action.removeprefix("create_").replace("_", " "),
                source_text=self.source_text, source_path=self.source_path,
                skill_text=self.skill_text, output_path=self.output_path,
            )
            response = self.call_with_retries(
                stage="baseline", system_prompt=system, user_prompt=user,
                json_mode=self.uses_json,
            )
            checked = validate_output(response.text, self.action, ".json" if self.uses_json else self.extension, response.finish_reason)
            if not checked["valid"]:
                raise GenerationFailure("Baseline output failed validation: " + "; ".join(checked["errors"][:3]), truncated=checked["truncated"])
            final_value = checked["data"] if self.uses_json else checked["text"]
            chunks: list[Chunk] = []
            values: list[dict | str] = [final_value]
        else:
            chunks = self.make_chunks()
            values = []
            processed_chunks: list[Chunk] = []

            def process(chunk: Chunk, depth: int = 0) -> None:
                try:
                    knowledge = self.extraction(chunk) if self.options.strategy in {"extraction_then_generation", "multi_pass"} else None
                    value = self.generate_chunk(chunk, knowledge)
                except Exception as exc:
                    can_split = (
                        depth < 6 and estimate_tokens(chunk.text) > 256 and
                        (getattr(exc, "truncated", False) or budget_rejection(exc))
                    )
                    if not can_split:
                        raise
                    target = max(128, estimate_tokens(chunk.text) // 2)
                    children = chunk_source(chunk.text, chunk.source, target, 0, estimate_tokens)
                    if len(children) < 2:
                        raise
                    self.metrics["recovery_events"].append({
                        "stage": chunk.chunk_id, "strategy": "smaller_chunk",
                        "depth": depth + 1, "children": len(children),
                    })
                    for number, child in enumerate(children, 1):
                        adjusted = replace(
                            child, chunk_id=f"{chunk.chunk_id}_{number:02d}",
                            sequence=len(processed_chunks) + 1,
                            start=chunk.start + child.start, end=chunk.start + child.end,
                        )
                        atomic_text(self.chunks_dir / f"{adjusted.chunk_id}.txt", adjusted.text)
                        metadata = adjusted.to_dict()
                        metadata.pop("text", None)
                        metadata.pop("overlap_text", None)
                        atomic_json(self.chunks_dir / f"{adjusted.chunk_id}.json", metadata)
                        process(adjusted, depth + 1)
                    return
                if isinstance(value, dict) and self.options.strategy == "multi_pass":
                    value = self.coverage_repair(chunk, value)
                    atomic_json(self.intermediate_dir / "generated" / f"{chunk.chunk_id}.json", value)
                processed_chunks.append(chunk)
                values.append(value)

            for chunk in chunks:
                process(chunk)
            chunks = processed_chunks
            self.metrics["chunk_count"] = len(chunks)
            if self.uses_json:
                objects = [value for value in values if isinstance(value, dict)]
                final_value = (hierarchical_aggregate(objects, self.action, self.aggregate_dir, self.options.group_size)
                               if self.options.aggregation == "hierarchical" else merge_artifacts(objects, self.action))
                atomic_json(self.aggregate_dir / "aggregate.json", final_value)
                atomic_json(self.aggregate_dir / "provenance.json", provenance_index(
                    final_value, objects, [self.provenance(chunk, "generation") for chunk in chunks]
                ))
            else:
                final_value = self.aggregate_text([str(value) for value in values])
                atomic_text(self.aggregate_dir / ("aggregate" + self.extension), final_value)

        final_text = (json.dumps(final_value, ensure_ascii=False, indent=2) + "\n"
                      if self.uses_json else str(final_value))
        validation = (validate_coverage(self.source_text, final_value, self.action, self.source_path.name)
                      if self.options.validate else {"valid": True, "validation_skipped": True})
        final_check = validate_output(final_text, self.action, ".json" if self.uses_json else self.extension)
        validation.update({
            "valid": final_check["valid"], "output_errors": final_check["errors"],
            "truncated": final_check["truncated"], "passed": final_check["valid"],
        })
        self.metrics.update({
            "runtime_seconds": time.perf_counter() - self.started,
            "output_bytes": len(final_text.encode("utf-8")),
            "estimated_cost": self.estimated_cost(),
            "status": "complete" if final_check["valid"] else "validation_failed",
        })
        atomic_json(self.work_dir / "metrics.json", self.metrics)
        atomic_json(self.work_dir / "validation.json", validation)
        return PipelineResult(final_text, self.metrics, validation, self.work_dir)

    def estimated_cost(self) -> float | None:
        if self.profile.input_cost_per_million is None or self.profile.output_cost_per_million is None:
            return None
        return ((self.metrics["input_tokens"] * self.profile.input_cost_per_million
                 + self.metrics["output_tokens"] * self.profile.output_cost_per_million) / 1_000_000)


def run_pipeline(
    *, source_text: str, source_path: Path, skill_text: str, connector: LLMConnector,
    connector_name: str, action: str, output_path: Path,
    options: PipelineOptions | None = None,
) -> PipelineResult:
    """Execute the selected strategy and return the final aggregate plus diagnostics."""

    return _Run(
        source_text=source_text, source_path=source_path, skill_text=skill_text,
        connector=connector, connector_name=connector_name, action=action,
        output_path=output_path, options=options or PipelineOptions(),
    ).run()
