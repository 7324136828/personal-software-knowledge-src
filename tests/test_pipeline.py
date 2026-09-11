from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from connectors.base import GenerationResponse
from experiments.runner import SCENARIOS, compare_runs, load_scenario, parse_model_spec
from pipeline.chunker import chunk_source
from pipeline.engine import PipelineOptions, run_pipeline
from pipeline.model_profiles import ModelProfile, get_model_profile
from pipeline.retry import backoff_delay
from pipeline.token_budget import TokenBudget, TokenBudgetError
from pipeline.validator import validate_output
from errors import ProviderError


def qanda(question_id: str = "generated-question") -> str:
    return json.dumps({
        "title": "Study questions",
        "description": "Grounded review",
        "questions": [{
            "id": question_id,
            "kind": "generated",
            "exercise_label": None,
            "question": "What is the central idea?",
            "answer": "The source explains the central idea.",
            "placeholder": "Recall the section…",
            "required": True,
            "source_ids": ["chapter.txt"],
        }],
    })


class RecordingConnector:
    model = "fake-model"

    def __init__(self, *, truncate_first: bool = False) -> None:
        self.calls: list[dict] = []
        self.truncate_first = truncate_first

    def generate_response(self, **request) -> GenerationResponse:
        self.calls.append(request)
        if self.truncate_first and len(self.calls) == 1:
            return GenerationResponse('{"title":', finish_reason="length", input_tokens=10, output_tokens=2)
        return GenerationResponse(qanda(f"question-{len(self.calls)}"), finish_reason="stop", input_tokens=10, output_tokens=20)


class ExtractionConnector(RecordingConnector):
    def generate_response(self, **request) -> GenerationResponse:
        self.calls.append(request)
        if "knowledge inventory" in request["system_prompt"].lower():
            evidence = "A compact source statement."
            inventory = {
                "concepts": [{"text": "compact source", "quote": evidence, "section": "Preamble"}],
                "definitions": [], "formulas": [], "examples": [], "exercises": [],
                "solutions": [], "important_claims": [], "source_sections": [],
            }
            return GenerationResponse(json.dumps(inventory), finish_reason="stop")
        return GenerationResponse(qanda("extracted-question"), finish_reason="stop")


class FlakyConnector(RecordingConnector):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def generate_response(self, **request) -> GenerationResponse:
        self.calls.append(request)
        if len(self.calls) <= self.failures:
            error = ProviderError("OpenAI request failed (HTTP 404): NotFoundError.")
            error.status_code = 404
            raise error
        return GenerationResponse(qanda("eventual-success"), finish_reason="stop")


class PayloadFlakyConnector(RecordingConnector):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def generate_response(self, **request) -> GenerationResponse:
        self.calls.append(request)
        if len(self.calls) <= self.failures:
            error = ProviderError("OpenAI request failed (HTTP 400): BadRequestError.")
            error.status_code = 400
            raise error
        return GenerationResponse(qanda("smaller-payload-success"), finish_reason="stop")


class ValidationFeedbackConnector(RecordingConnector):
    """Returns an invalid source ID first, then fixes it after validation feedback."""

    def generate_response(self, **request) -> GenerationResponse:
        self.calls.append(request)
        if len(self.calls) == 1:
            return GenerationResponse(json.dumps({
                "title": "Model selection",
                "fields": [{
                    "name": "model_selection", "description": "Selection guidance",
                    "example": "Choose the simpler model",
                }],
                "data": [{
                    "source_id": "input/chapter.txt", "page": "1",
                    "model_selection": "Choose the simpler model",
                }],
            }), finish_reason="stop")
        return GenerationResponse(json.dumps({
            "title": "Model selection",
            "fields": [{
                "name": "model_selection", "description": "Selection guidance",
                "example": "Choose the simpler model",
            }],
            "data": [{
                "source_id": "chapter.txt", "page": "1",
                "model_selection": "Choose the simpler model",
            }],
        }), finish_reason="stop")


class PipelineTests(unittest.TestCase):
    def test_exponential_backoff_sequence_caps_at_four_minutes(self) -> None:
        self.assertEqual(
            [backoff_delay(attempt) for attempt in range(10)],
            [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 240.0, 240.0],
        )

    def test_provider_errors_retry_until_success_when_limit_is_unspecified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connector = FlakyConnector(2)
            from unittest.mock import patch
            with patch("pipeline.engine.backoff") as sleep:
                result = run_pipeline(
                    source_text="A compact source statement.",
                    source_path=Path("chapter.txt"),
                    skill_text="Return canonical JSON.",
                    connector=connector,
                    connector_name="openai",
                    action="create_qandas",
                    output_path=Path(directory) / "final.json",
                    options=PipelineOptions(
                        strategy="chunked",
                        work_dir=Path(directory) / "work",
                        profile_overrides={
                            "context_window": 32000,
                            "max_output_tokens": 4096,
                            "recommended_chunk_tokens": 5000,
                            "reserved_output_tokens": 4096,
                        },
                    ),
                )
            self.assertEqual(len(connector.calls), 3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [0, 1])
            self.assertEqual(result.metrics["retry_count"], 2)

    def test_http_400_retries_with_a_smaller_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connector = PayloadFlakyConnector(2)
            from unittest.mock import patch
            with patch("pipeline.engine.backoff"):
                result = run_pipeline(
                    source_text="Source evidence. " * 800,
                    source_path=Path("chapter.txt"),
                    skill_text="Return canonical JSON.",
                    connector=connector,
                    connector_name="openai",
                    action="create_qandas",
                    output_path=Path(directory) / "final.json",
                    options=PipelineOptions(
                        strategy="chunked", chunk_tokens=16000,
                        work_dir=Path(directory) / "work",
                        profile_overrides={
                            "context_window": 32000,
                            "max_output_tokens": 4096,
                            "recommended_chunk_tokens": 16000,
                            "reserved_output_tokens": 4096,
                        },
                    ),
                )
            self.assertEqual(len(connector.calls), 3)
            self.assertLess(
                len(connector.calls[1]["user_prompt"]),
                len(connector.calls[0]["user_prompt"]),
            )
            self.assertLess(
                connector.calls[1]["max_output_tokens"],
                connector.calls[0]["max_output_tokens"],
            )
            self.assertFalse(connector.calls[2]["json_mode"])
            self.assertEqual(
                [event["strategy"] for event in result.metrics["recovery_events"][:2]],
                ["provider_payload_retry", "provider_payload_retry"],
            )

    def test_validation_retry_truncates_source_and_appends_errors(self) -> None:
        source = (
            "Choose models by comparing performance and complexity. " * 40
            + "END_OF_ORIGINAL_CHUNK"
        )
        with tempfile.TemporaryDirectory() as directory:
            connector = ValidationFeedbackConnector()
            from unittest.mock import patch
            with patch("pipeline.engine.random.uniform", return_value=0.1):
                result = run_pipeline(
                    source_text=source,
                    source_path=Path("chapter.txt"),
                    skill_text="Return canonical JSON.",
                    connector=connector,
                    connector_name="ollama",
                    action="create_datatables",
                    output_path=Path(directory) / "final.json",
                    options=PipelineOptions(
                        strategy="chunked", retries=1, work_dir=Path(directory) / "work",
                        profile_overrides={
                            "context_window": 32000, "max_output_tokens": 4096,
                            "recommended_chunk_tokens": 5000, "reserved_output_tokens": 4096,
                        },
                    ),
                )
        self.assertEqual(len(connector.calls), 2)
        retry_prompt = connector.calls[1]["user_prompt"]
        self.assertIn("PREVIOUS VALIDATION FAILURES", retry_prompt)
        self.assertIn(
            "$.data[0].source_id: invalid format",
            retry_prompt,
        )
        self.assertIn("Choose models by comparing performance and complexity.", retry_prompt)
        self.assertNotIn("END_OF_ORIGINAL_CHUNK", retry_prompt)
        self.assertNotIn("Artifact to repair", retry_prompt)
        self.assertTrue(result.validation["valid"])
        recovery = next(
            event for event in result.metrics["recovery_events"]
            if event["strategy"] == "validation_prompt_retry"
        )
        self.assertGreater(recovery["source_characters_removed"], 0)
        self.assertLess(
            recovery["source_characters_after"], recovery["source_characters_before"]
        )

    def test_character_chunk_separation_is_exactly_configurable(self) -> None:
        source = "A sentence with source evidence. " * 80
        with tempfile.TemporaryDirectory() as directory:
            connector = RecordingConnector()
            result = run_pipeline(
                source_text=source,
                source_path=Path("chapter.txt"),
                skill_text="Return canonical JSON.",
                connector=connector,
                connector_name="ollama",
                action="create_qandas",
                output_path=Path(directory) / "final.json",
                options=PipelineOptions(
                    strategy="chunked", chunk_characters=512, retries=0,
                    work_dir=Path(directory) / "work",
                    profile_overrides={
                        "context_window": 32000, "max_output_tokens": 4096,
                        "recommended_chunk_tokens": 5000, "reserved_output_tokens": 4096,
                    },
                ),
            )
        self.assertEqual(result.metrics["target_chunk_characters"], 512)
        self.assertGreater(result.metrics["chunk_count"], 1)

    def test_repeated_http_400_regenerates_from_smaller_source_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connector = PayloadFlakyConnector(5)
            from unittest.mock import patch
            with patch("pipeline.engine.backoff"):
                result = run_pipeline(
                    source_text="Source evidence about regression. " * 800,
                    source_path=Path("chapter.txt"),
                    skill_text="Return canonical JSON.",
                    connector=connector,
                    connector_name="openai",
                    action="create_qandas",
                    output_path=Path(directory) / "final.json",
                    options=PipelineOptions(
                        strategy="chunked", chunk_tokens=16000,
                        work_dir=Path(directory) / "work",
                        profile_overrides={
                            "context_window": 32000,
                            "max_output_tokens": 4096,
                            "recommended_chunk_tokens": 16000,
                            "reserved_output_tokens": 4096,
                        },
                    ),
                )
            self.assertEqual(len(connector.calls), 7)
            self.assertIn(
                "smaller_chunk",
                [event["strategy"] for event in result.metrics["recovery_events"]],
            )

    def test_semantic_chunking_is_lossless_and_keeps_atomic_blocks(self) -> None:
        source = (
            "# 1 Introduction\n\nA paragraph about the topic.\n\n"
            "\\begin{align}\nx &= y + 1 \\\\\ny &= z + 2\n\\end{align}\n\n"
            "Exercises\n1. Prove the displayed result.\n   (a) Use the first identity.\n"
            "   (b) Check the second identity.\n\n# 2 Next\n\nFinal paragraph.\n"
        )
        chunks = chunk_source(source, "chapter.txt", target_tokens=35, overlap_tokens=5)
        self.assertEqual("".join(chunk.text for chunk in chunks), source)
        formula = "\\begin{align}\nx &= y + 1"
        exercise = "1. Prove the displayed result."
        self.assertTrue(any(formula in chunk.text for chunk in chunks))
        self.assertTrue(any(exercise in chunk.text and "(b) Check" in chunk.text for chunk in chunks))
        self.assertTrue(all(chunk.chunk_id == f"chunk_{index:04d}" for index, chunk in enumerate(chunks, 1)))

    def test_unclosed_latex_does_not_hide_later_exercises(self) -> None:
        source = (
            "# 1 Body\n\\[\\begin{matrix} 1 & 0 \\] \n\n"
            "1.9 Exercises\n1.1. Show the result.\n1.2. Explain the consequence.\n"
        )
        from pipeline.chunker import source_inventory
        inventory = source_inventory(source)
        self.assertEqual([item["label"] for item in inventory["exercises"]], ["1.1", "1.2"])

    def test_token_budget_rejects_oversized_prompt(self) -> None:
        profile = ModelProfile(context_window=1000, max_output_tokens=200,
                               recommended_chunk_tokens=300, reserved_output_tokens=200,
                               safety_margin=100)
        budget = TokenBudget(profile)
        self.assertGreater(budget.chunk_allowance("short"), 0)
        with self.assertRaises(TokenBudgetError):
            budget.output_allowance("x" * 4000, "", 200)

    def test_unknown_model_uses_safe_profile_and_accepts_overrides(self) -> None:
        default = get_model_profile("openrouter", "vendor/new-model")
        overridden = get_model_profile("openrouter", "vendor/new-model", {"context_window": 16000})
        self.assertEqual(default.context_window, 8192)
        self.assertEqual(overridden.context_window, 16000)

    def test_json_validator_repairs_fence_and_trailing_comma(self) -> None:
        text = "```json\n" + qanda()[:-1] + ",}\n```"
        result = validate_output(text, "create_qandas", ".json", "stop")
        self.assertTrue(result["valid"], result["errors"])
        self.assertIn("removed_outer_json_fence", result["repairs"])
        self.assertIn("removed_trailing_commas", result["repairs"])

    def test_chunked_pipeline_persists_provenance_raw_data_and_resumes(self) -> None:
        source = "# 1 First\n\nAlpha material.\n\n# 2 Second\n\nBeta material.\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "work"
            output = root / "final.json"
            options = PipelineOptions(
                strategy="chunked", chunk_tokens=12, chunk_overlap=2,
                generation_passes=2, retries=0, work_dir=work,
                profile_overrides={"context_window": 32000, "max_output_tokens": 4096,
                                   "recommended_chunk_tokens": 5000, "reserved_output_tokens": 4096},
            )
            first = RecordingConnector()
            result = run_pipeline(
                source_text=source, source_path=Path("chapter.txt"),
                skill_text="Return the requested canonical JSON.", connector=first,
                connector_name="ollama", action="create_qandas",
                output_path=output, options=options,
            )
            chunks = sorted((work / "chunks").glob("chunk_*.txt"))
            self.assertGreater(len(chunks), 1)
            self.assertEqual("".join(path.read_text(encoding="utf-8") for path in chunks), source)
            self.assertTrue(list((work / "raw").glob("*_request.json")))
            self.assertTrue((work / "aggregate" / "provenance.json").is_file())
            self.assertTrue(result.validation["schema_valid"])

            resumed = RecordingConnector()
            second = run_pipeline(
                source_text=source, source_path=Path("chapter.txt"),
                skill_text="Return the requested canonical JSON.", connector=resumed,
                connector_name="ollama", action="create_qandas",
                output_path=output, options=options,
            )
            self.assertEqual(resumed.calls, [])
            self.assertEqual(json.loads(result.text), json.loads(second.text))

    def test_truncated_json_falls_back_to_smaller_chunks(self) -> None:
        source = "# 1 Topic\n\n" + ("A complete paragraph about regression selection. " * 30)
        with tempfile.TemporaryDirectory() as directory:
            connector = RecordingConnector(truncate_first=True)
            result = run_pipeline(
                source_text=source, source_path=Path("chapter.txt"),
                skill_text="Return canonical JSON.", connector=connector,
                connector_name="ollama", action="create_qandas",
                output_path=Path(directory) / "final.json",
                options=PipelineOptions(
                    strategy="chunked", chunk_tokens=5000, retries=0,
                    work_dir=Path(directory) / "work",
                    profile_overrides={"context_window": 32000, "max_output_tokens": 4096,
                                       "recommended_chunk_tokens": 5000, "reserved_output_tokens": 4096},
                ),
            )
            strategies = [event["strategy"] for event in result.metrics["recovery_events"]]
            self.assertIn("smaller_chunk", strategies)
            self.assertGreaterEqual(result.metrics["chunk_count"], 2)
            self.assertTrue(result.validation["valid"])

    def test_extraction_then_generation_persists_normalized_ir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connector = ExtractionConnector()
            work = Path(directory) / "work"
            result = run_pipeline(
                source_text="A compact source statement.", source_path=Path("chapter.txt"),
                skill_text="Return canonical JSON.", connector=connector,
                connector_name="ollama", action="create_qandas",
                output_path=Path(directory) / "final.json",
                options=PipelineOptions(
                    strategy="extraction_then_generation", retries=0, work_dir=work,
                    profile_overrides={"context_window": 32000, "max_output_tokens": 4096,
                                       "recommended_chunk_tokens": 5000, "reserved_output_tokens": 4096},
                ),
            )
            extraction = json.loads((work / "knowledge_and_artifacts" / "extractions" / "chunk_0001.json").read_text(encoding="utf-8"))
            self.assertEqual(extraction["concepts"][0]["quote"], "A compact source statement.")
            self.assertEqual(result.metrics["provider_calls"], 2)
            self.assertTrue(result.validation["valid"])

    def test_experiment_presets_and_compare(self) -> None:
        self.assertEqual(len(SCENARIOS), 9)
        for scenario in SCENARIOS:
            self.assertEqual(load_scenario(scenario)["name"], scenario)
        self.assertEqual(parse_model_spec("ollama:llama3.2:3b"), ("ollama", "llama3.2:3b"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "run"
            root.mkdir(parents=True)
            (root / "config.json").write_text(json.dumps({"name": "demo", "connector": "ollama", "model": "tiny"}), encoding="utf-8")
            (root / "metrics.json").write_text(json.dumps({"status": "complete", "input_tokens": 10, "output_tokens": 5, "runtime_seconds": 1.2}), encoding="utf-8")
            (root / "validation.json").write_text(json.dumps({"coverage": 1.0, "exercise_coverage": 0.5, "duplicate_ratio": 0.0, "truncated_outputs": 0}), encoding="utf-8")
            table = compare_runs(Path(directory))
            self.assertIn("demo", table)
            self.assertIn("100.0%", table)


if __name__ == "__main__":
    unittest.main()
