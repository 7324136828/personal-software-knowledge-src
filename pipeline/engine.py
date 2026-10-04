"""Provider-neutral, resumable map/validate/reduce generation pipeline.

The engine owns chunking, budgets, recovery, checkpoints, provenance and
aggregation.  Action modules remain small compatibility wrappers and connectors
only translate provider request/response formats.
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from action_base import build_generation_prompts
from connectors.base import GenerationResponse, LLMConnector
from diagnostic_logging import record_exchange
from errors import GenerationCancelled, InvalidArgumentsError, ProviderError
from podcast_policy import podcast_runtime

from .aggregator import hierarchical_aggregate, merge_artifacts, provenance_index
from .checkpoint import Checkpoint, atomic_json, atomic_text, digest, evidence_prefix
from .chunker import Chunk, chunk_source, source_inventory
from .extractor import EXTRACTION_SYSTEM, extraction_prompt, validate_extraction
from .model_profiles import ModelProfile, get_model_profile
from .retry import (
    GenerationFailure, backoff, backoff_delay, budget_rejection, payload_rejection,
    retryable,
)
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
_PAYLOAD_ADAPTATION_ATTEMPTS = 4
_MIN_ADAPTED_OUTPUT_TOKENS = 512
_MIN_ADAPTED_PROMPT_CHARACTERS = 1024
_MIN_VALIDATION_RETRY_SOURCE_CHARACTERS = 256
_VALIDATION_RETRY_TRUNCATION_RANGE = (0.05, 0.20)


@dataclass
class PipelineOptions:
    """All provider-independent controls for one generation run."""

    strategy: str = "chunked"
    chunk_tokens: int | None = None
    chunk_characters: int | None = None
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
    cancel_check: Callable[[], bool] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.strategy not in _STRATEGIES:
            raise InvalidArgumentsError(f"Unknown pipeline strategy '{self.strategy}'.")
        if self.aggregation not in _AGGREGATIONS:
            raise InvalidArgumentsError(f"Unknown aggregation strategy '{self.aggregation}'.")
        for name in ("chunk_tokens", "chunk_characters", "max_output_tokens", "retries"):
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
        if self.cancel_check is not None and not callable(self.cancel_check):
            raise InvalidArgumentsError("cancel_check must be callable.")
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
        payload_adjustment: dict[str, Any] | None = None,
    ) -> GenerationResponse:
        """Make one budgeted request and preserve append-only, secret-free evidence."""

        self.ensure_not_cancelled()
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
        if payload_adjustment:
            request_record["payload_adjustment"] = payload_adjustment
        record_exchange("pipeline_request", request_record)
        LOGGER.debug("Request %s: connector=%s model=%s output_budget=%s input_estimate=%s",
                     stage, self.connector_name, self.connector.model, allowance,
                     self.budget.input_tokens(system_prompt, user_prompt))
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
            record_exchange("pipeline_response", {
                "stage": stage, "connector": self.connector_name, "model": self.connector.model,
                "text": result.text, "finish_reason": result.finish_reason,
                "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                "metadata": result.raw_metadata,
            })
            LOGGER.debug("Response %s: finish_reason=%s input_tokens=%s output_tokens=%s characters=%s",
                         stage, result.finish_reason, result.input_tokens, result.output_tokens, len(result.text))
            self.ensure_not_cancelled()
        except Exception as exc:
            record_exchange("pipeline_failure", {"stage": stage, "connector": self.connector_name,
                                                   "model": self.connector.model, "error_type": type(exc).__name__,
                                                   "error": _safe_error(exc), "status_code": getattr(exc, "status_code", None)})
            LOGGER.debug("Request %s failed: %s", stage, _safe_error(exc))
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

    def ensure_not_cancelled(self) -> None:
        if self.options.cancel_check and self.options.cancel_check():
            raise GenerationCancelled("Conversion was discarded by the user.")

    @staticmethod
    def _shorten_payload(text: str, maximum_characters: int) -> str:
        """Keep request framing plus the most recent material within a smaller payload."""

        if len(text) <= maximum_characters:
            return text
        marker = "\n\n[Earlier request material omitted after provider payload rejection.]\n\n"
        retained = max(1, (maximum_characters - len(marker)) // 2)
        return text[:retained] + marker + text[-retained:]

    def _adapt_payload(self, original: dict[str, Any], attempt: int) -> dict[str, Any]:
        """Create a materially smaller request after a 400-style provider rejection.

        This is intentionally based on the original request rather than repeatedly
        trimming an already-trimmed payload, so every evidence record is predictable
        and preserves both the request framing and the latest repair material.
        """

        adapted = dict(original)
        original_prompt = str(original["user_prompt"])
        initial_output = original.get("requested_output") or self.options.max_output_tokens
        if initial_output is None:
            initial_output = self.profile.reserved_output_tokens
        prompt_limit = max(
            _MIN_ADAPTED_PROMPT_CHARACTERS,
            len(original_prompt) // (2 ** attempt),
        )
        output_limit = max(
            _MIN_ADAPTED_OUTPUT_TOKENS,
            int(initial_output) // (2 ** attempt),
        )
        adapted["user_prompt"] = self._shorten_payload(original_prompt, prompt_limit)
        adapted["requested_output"] = output_limit
        # A Responses endpoint may reject a structured-output feature for a model
        # even when the text payload is valid. The prompt still demands JSON, so
        # this fallback keeps schema guidance without the provider-only parameter.
        if attempt >= 2 and adapted.get("json_mode"):
            adapted["json_mode"] = False
        adapted["payload_adjustment"] = {
            "reason": "provider_payload_rejection",
            "attempt": attempt,
            "user_prompt_characters": len(adapted["user_prompt"]),
            "requested_output_tokens": output_limit,
            "json_mode": adapted["json_mode"],
        }
        return adapted

    def call_with_retries(self, **request: Any) -> GenerationResponse:
        """Retry API failures until success unless an explicit limit was supplied.

        ``--retries N`` remains available for batch/CI jobs that require a finite
        failure time. With no override, transient provider failures use capped
        exponential backoff indefinitely. Payload rejections instead get four
        progressively smaller variants before the caller regenerates from smaller
        source chunks; validation and repair attempts remain bounded by the model
        profile's ``retries`` value.
        """

        last: Exception | None = None
        original_request = dict(request)
        current_request = dict(request)
        attempt = 0
        payload_attempts = 0
        while True:
            self.ensure_not_cancelled()
            try:
                return self.request(**current_request)
            except Exception as exc:
                last = exc
                retry_limit = self.options.retries
                if (
                    (retry_limit is not None and attempt >= retry_limit)
                    or budget_rejection(exc)
                    or not retryable(exc)
                ):
                    raise
                if payload_rejection(exc):
                    payload_attempts += 1
                    if payload_attempts > _PAYLOAD_ADAPTATION_ATTEMPTS:
                        raise GenerationFailure(
                            "Provider rejected progressively smaller request payloads; "
                            "regenerate this stage from smaller source chunks.",
                            payload_rejected=True,
                        ) from exc
                    current_request = self._adapt_payload(original_request, payload_attempts)
                self.metrics["retry_count"] += 1
                delay = backoff_delay(attempt)
                self.metrics["recovery_events"].append({
                    "stage": original_request["stage"],
                    "strategy": (
                        "provider_payload_retry" if payload_rejection(exc) else "provider_retry"
                    ),
                    "attempt": attempt + 1, "error_type": type(exc).__name__,
                    "delay_seconds": delay,
                    "payload_adjustment": current_request.get("payload_adjustment"),
                })
                LOGGER.warning(
                    "API request failed during %s (%s); retrying in %s seconds%s.",
                    original_request["stage"], type(exc).__name__, int(delay),
                    " with a smaller payload" if payload_rejection(exc) else "",
                )
                backoff(attempt, cancel_check=self.options.cancel_check)
                attempt += 1
        raise last or ProviderError("Generation failed without an error.")  # pragma: no cover

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
        if self.options.chunk_characters is not None:
            target = self.options.chunk_characters
            chunks = chunk_source(self.source_text, self.source_path.name, target, 0, len)
            chunks = [
                replace(chunk, estimated_input_tokens=estimate_tokens(chunk.overlap_text + chunk.text))
                for chunk in chunks
            ]
            self.metrics["target_chunk_characters"] = target
        else:
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
        LOGGER.debug("Prepared %s source chunks: target=%s %s available_input_tokens=%s overlap_tokens=%s",
                     len(chunks), target, "characters" if self.options.chunk_characters is not None else "tokens",
                     available, overlap)
        if self.options.chunk_characters is None:
            self.metrics["target_chunk_tokens"] = target
        return chunks

    def extraction(self, chunk: Chunk) -> dict:
        key = f"{chunk.chunk_id}:extraction"
        fingerprint = digest({"text": chunk.text, "model": self.connector.model,
                              "prompt": EXTRACTION_SYSTEM, "profile": asdict(self.profile)})
        path = self.intermediate_dir / "extractions" / f"{chunk.chunk_id}.json"
        if cached := self.checkpoint.get(key, fingerprint):
            LOGGER.debug("Using completed extraction checkpoint for %s", chunk.chunk_id)
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

    def generation_source(self, chunk: Chunk, knowledge: dict | None = None) -> str:
        source = chunk.overlap_text + chunk.text
        if knowledge is not None:
            source = (
                "NORMALIZED KNOWLEDGE INVENTORY (each quote is source evidence)\n"
                + json.dumps(knowledge, ensure_ascii=False, separators=(",", ":"))
                + "\nEND KNOWLEDGE INVENTORY"
            )
        return source

    def generation_prompts(
        self,
        chunk: Chunk,
        knowledge: dict | None = None,
        *,
        source_override: str | None = None,
    ) -> tuple[str, str]:
        source = source_override if source_override is not None else self.generation_source(chunk, knowledge)
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

    @staticmethod
    def truncate_validation_retry_source(source: str) -> tuple[str, dict[str, Any]]:
        """Randomly shorten retry context while retaining at least half of small inputs."""

        before = len(source)
        if before <= 1:
            return source, {
                "source_characters_before": before,
                "source_characters_after": before,
                "source_characters_removed": 0,
                "source_reduction_fraction": 0.0,
            }
        minimum = min(_MIN_VALIDATION_RETRY_SOURCE_CHARACTERS, max(1, before // 2))
        fraction = random.uniform(*_VALIDATION_RETRY_TRUNCATION_RANGE)
        target = max(minimum, before - max(1, round(before * fraction)))
        # Prefer a paragraph or line boundary within two percentage points of
        # the random target so a distant boundary cannot cause a huge removal.
        boundary_floor = max(minimum, target - max(1, round(before * 0.02)))
        boundary = source.rfind("\n\n", boundary_floor, target + 1)
        if boundary < boundary_floor:
            boundary = source.rfind("\n", boundary_floor, target + 1)
        after = boundary if boundary >= boundary_floor else target
        shortened = source[:after].rstrip()
        actual_after = len(shortened)
        return shortened, {
            "source_characters_before": before,
            "source_characters_after": actual_after,
            "source_characters_removed": before - actual_after,
            "source_reduction_fraction": round((before - actual_after) / before, 4),
        }

    @staticmethod
    def validation_retry_prompt(prompt: str, errors: list[str]) -> str:
        """Ask for a complete regeneration with actionable validation feedback.

        Starting from the original generation prompt is important for artifacts
        with dynamic fields (such as data tables): a repair prompt that only
        includes the canonical schema cannot describe those generated fields.
        """

        return (
            prompt
            + "\n\nPREVIOUS VALIDATION FAILURES\n- "
            + "\n- ".join(errors[:20])
            + "\nReturn a complete replacement JSON object. Fix every listed failure while "
              "preserving the required schema, source grounding, and all requested content."
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
            LOGGER.debug("Using completed generation checkpoint for %s", chunk.chunk_id)
            return cached["data"]
        system, user = self.generation_prompts(chunk, knowledge)
        retry_source = self.generation_source(chunk, knowledge)
        response = self.call_with_retries(
            stage=f"{chunk.chunk_id}_generate", system_prompt=system, user_prompt=user,
            json_mode=self.uses_json,
        )
        checked = validate_output(response.text, self.action, ".json" if self.uses_json else self.extension, response.finish_reason)
        LOGGER.debug("Validation %s: valid=%s truncated=%s errors=%s", chunk.chunk_id,
                     checked["valid"], checked["truncated"], checked["errors"][:8])

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
            retry_source, truncation = self.truncate_validation_retry_source(retry_source)
            LOGGER.debug("Validation retry %s attempt=%s source_characters=%s errors=%s",
                         chunk.chunk_id, attempts, len(retry_source), checked["errors"][:8])
            self.metrics["recovery_events"].append({
                "stage": key, "strategy": "validation_prompt_retry", "attempt": attempts,
                "errors": checked["errors"][:8],
                **truncation,
            })
            retry_system, retry_user = self.generation_prompts(
                chunk, knowledge, source_override=retry_source
            )
            response = self.call_with_retries(
                stage=f"{chunk.chunk_id}_validation_retry_{attempts}",
                system_prompt=retry_system,
                user_prompt=self.validation_retry_prompt(retry_user, checked["errors"]),
                json_mode=True,
            )
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
            LOGGER.debug("Using completed coverage repair checkpoint for %s", chunk.chunk_id)
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

    @staticmethod
    def podcast_material(value: dict | str) -> str:
        """Keep spoken draft material available for a source-grounded summary."""

        if isinstance(value, str):
            return value
        lines = [json.dumps({key: value[key] for key in ("episode_title", "podcast_show", "cast")},
                            ensure_ascii=False, separators=(",", ":"))]
        for segment in value["script"]:
            lines.append("\n" + segment["segment_name"])
            for scene in segment["scenes"]:
                lines.append(f"{scene['speaker_id']}: {scene.get('directions', '')} {scene['dialogue']}")
        return "\n".join(lines)

    def assemble_podcast(self, values: list[dict | str]) -> dict | str:
        """Summarize chunk drafts into one episode instead of appending episodes."""

        if len(values) == 1:
            return values[0]
        LOGGER.debug("Assembling one podcast from %s chunk drafts", len(values))
        system, _ = build_generation_prompts(
            action=self.action, artifact_name="single text-only podcast episode",
            source_text="", source_path=self.source_path, skill_text=self.skill_text,
            output_path=Path("episode.json" if self.uses_json else "episode.md"),
        )
        header = (
            "Assemble the source-grounded drafts below into exactly one self-contained podcast "
            "episode for this study set. Use one introduction, a connected discussion, and one "
            "closing/sign-off. Summarize and prioritize the key concepts and examples to fit; "
            "remove repeated introductions, recaps, and sign-offs. Do not invent facts or split "
            "the result into episodes. Runtime must be at most 20 minutes at 150 dialogue words "
            "per minute plus all scripted pauses. There is no minimum length.\n"
        )
        if self.uses_json:
            header += "Return exactly one JSON object using this schema:\n" + json.dumps(get_schema(self.action)) + "\n"
        else:
            header += "Return the canonical Markdown transcript with bold speaker labels.\n"
        requested = self.options.max_output_tokens or self.profile.reserved_output_tokens
        capacity = (self.profile.context_window - self.profile.safety_margin
                    - self.budget.input_tokens(system, header)
                    - min(requested, self.profile.max_output_tokens) - 256)
        if capacity < 1024:
            raise GenerationFailure("Model context budget is too small to assemble a podcast; increase the verified context limit.")
        word_limit = min(2700, max(50, (capacity // 2 - 600) // 4))
        LOGGER.debug("Podcast assembly input_capacity=%s dialogue_word_target=%s", capacity, word_limit)
        header += f"Keep dialogue to at most {word_limit} words, allowing room for pauses.\nDRAFT MATERIAL\n"
        material = "\n\n".join(self.podcast_material(value) for value in values)
        nodes = []
        for chunk in chunk_source(material, self.source_path.name, capacity, 0, estimate_tokens):
            if chunk.estimated_input_tokens <= capacity:
                nodes.append(chunk.text)
            else:
                # Drafts can be excerpted for synthesis even when they contain a
                # large protected block. Keep every character within the budget.
                width = max(1, capacity // 4)
                nodes.extend(chunk.text[start:start + width] for start in range(0, len(chunk.text), width))

        def synthesize(drafts: list[str], stage: str, *, intermediate: bool) -> dict | str:
            prompt = header + "\n\n".join(drafts)
            fingerprint = digest({"system": system, "prompt": prompt, "profile": asdict(self.profile),
                                  "model": self.connector.model, "requested_output": requested,
                                  "intermediate": intermediate})
            key = "podcast:" + stage
            if cached := self.checkpoint.get(key, fingerprint):
                LOGGER.debug("Using completed podcast assembly checkpoint for %s", stage)
                return cached["data"]
            errors: list[str] = []
            for attempt in range(self.retries + 1):
                response = self.call_with_retries(
                    stage=f"podcast_{stage}_{attempt}", system_prompt=system,
                    user_prompt=prompt + ("\nFix these validation failures:\n- " + "\n- ".join(errors) if errors else ""),
                    json_mode=self.uses_json,
                )
                checked = validate_output(response.text, self.action, ".json" if self.uses_json else self.extension,
                                          response.finish_reason)
                value = checked["data"] if self.uses_json else checked["text"]
                errors = list(checked["errors"])
                if checked["valid"] and intermediate and estimate_tokens(self.podcast_material(value)) > capacity // 2:
                    errors.append(f"Condense the complete draft to at most {capacity // 2} estimated input tokens for assembly.")
                if not errors:
                    self.checkpoint.save(key, fingerprint, self.aggregate_dir / f"podcast_{stage}.checkpoint.json", {"data": value})
                    return value
                self.metrics["repair_count"] += 1
            self.checkpoint.fail(key, fingerprint, "; ".join(errors[:8]))
            raise GenerationFailure("Podcast assembly failed validation: " + "; ".join(errors[:3]))

        # Large sets get bounded summaries before the final assembly request.
        if len(nodes) > 1:
            nodes = [self.podcast_material(synthesize([node], f"draft_{index:04d}", intermediate=True))
                     for index, node in enumerate(nodes, 1)]
        level = 0
        while len(nodes) > 2:
            nodes = [self.podcast_material(synthesize(nodes[start:start + 2], f"level_{level:02d}_{start:04d}", intermediate=True))
                     for start in range(0, len(nodes), 2)]
            level += 1
        result = synthesize(nodes, "final", intermediate=False)
        self.metrics["recovery_events"].append({"stage": "podcast:final", "strategy": "single_episode_assembly"})
        return result

    def run(self) -> PipelineResult:
        self.ensure_not_cancelled()
        LOGGER.debug("Pipeline started: action=%s strategy=%s source_characters=%s output=%s profile=%s",
                     self.action, self.options.strategy, len(self.source_text), self.output_path, asdict(self.profile))
        self.work_dir.mkdir(parents=True, exist_ok=True)
        option_values = {
            item.name: getattr(self.options, item.name)
            for item in fields(self.options)
            if item.name != "cancel_check"
        }
        atomic_json(self.work_dir / "run_config.json", {
            "source": str(self.source_path), "output": str(self.output_path), "action": self.action,
            "connector": self.connector_name, "model": self.connector.model,
            "options": {**option_values, "work_dir": str(self.work_dir)},
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
                        (
                            getattr(exc, "truncated", False)
                            or getattr(exc, "payload_rejected", False)
                            or budget_rejection(exc)
                        )
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
                    LOGGER.debug("Splitting %s after %s into %s smaller chunks at depth %s",
                                 chunk.chunk_id, type(exc).__name__, len(children), depth + 1)
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
                if self.action == "create_podcasts":
                    final_value = self.assemble_podcast(objects)
                elif self.options.aggregation == "hierarchical":
                    final_value = hierarchical_aggregate(objects, self.action, self.aggregate_dir, self.options.group_size)
                else:
                    final_value = merge_artifacts(objects, self.action)
                atomic_json(self.aggregate_dir / "aggregate.json", final_value)
                atomic_json(self.aggregate_dir / "provenance.json", provenance_index(
                    final_value, objects, [self.provenance(chunk, "generation") for chunk in chunks]
                ))
            else:
                final_value = (self.assemble_podcast(values) if self.action == "create_podcasts" else
                               self.aggregate_text([str(value) for value in values]))
                atomic_text(self.aggregate_dir / ("aggregate" + self.extension), final_value)

        final_text = (json.dumps(final_value, ensure_ascii=False, indent=2) + "\n"
                      if self.uses_json else str(final_value))
        validation = (validate_coverage(self.source_text, final_value, self.action, self.source_path.name)
                      if self.options.validate else {"valid": True, "validation_skipped": True})
        final_check = validate_output(final_text, self.action, ".json" if self.uses_json else self.extension)
        LOGGER.debug("Final validation: valid=%s errors=%s provider_calls=%s retries=%s repairs=%s",
                     final_check["valid"], final_check["errors"][:8], self.metrics["provider_calls"],
                     self.metrics["retry_count"], self.metrics["repair_count"])
        validation.update({
            "valid": final_check["valid"], "output_errors": final_check["errors"],
            "truncated": final_check["truncated"], "passed": final_check["valid"],
        })
        if self.action == "create_podcasts":
            validation["podcast_runtime"] = podcast_runtime(final_value)
        self.metrics.update({
            "runtime_seconds": time.perf_counter() - self.started,
            "output_bytes": len(final_text.encode("utf-8")),
            "estimated_cost": self.estimated_cost(),
            "status": "complete" if final_check["valid"] else "validation_failed",
        })
        atomic_json(self.work_dir / "metrics.json", self.metrics)
        atomic_json(self.work_dir / "validation.json", validation)
        if self.action == "create_podcasts" and not final_check["valid"]:
            raise GenerationFailure("Final podcast failed validation: " + "; ".join(final_check["errors"][:3]))
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

    runner = _Run(
        source_text=source_text, source_path=source_path, skill_text=skill_text,
        connector=connector, connector_name=connector_name, action=action,
        output_path=output_path, options=options or PipelineOptions(),
    )
    try:
        return runner.run()
    except Exception as exc:
        # Let server and batch frontends report retry counts when no result exists.
        exc.pipeline_metrics = runner.metrics
        raise
