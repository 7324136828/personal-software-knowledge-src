from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cli_runtime import execute_generation
from connectors.base import GenerationResponse
from errors import GenerationCancelled, OutputWriteError, ProviderError
from orchestrator import main
from output_writer import write_output
from pipeline.engine import PipelineOptions
from pipeline.retry import GenerationFailure
from pipeline.validator import validate_coverage, validate_output


SOURCE = (
    "Alpha source explains its central concept.\n\n"
    "Beta source offers a representative example.\n\n"
    "Gamma source describes the final consequence.\n"
)


class SubsetConnector:
    model = "fake-model"

    def __init__(self, failures: set[str] | None = None, *, text: bool = False,
                 cancelled: bool = False) -> None:
        self.failures = failures or set()
        self.text = text
        self.cancelled = cancelled
        self.chunks: list[str] = []

    def generate_response(self, **request) -> GenerationResponse:
        chunk_id = request["user_prompt"].split("Chunk: ", 1)[1].split(";", 1)[0]
        self.chunks.append(chunk_id)
        if chunk_id in self.failures:
            if self.cancelled:
                raise GenerationCancelled("Discarded")
            raise ProviderError("Subset request failed")
        if self.text:
            value = f"Completed source material for {chunk_id}.\n"
        else:
            value = json.dumps({
                "title": "Study questions", "description": "Grounded review",
                "questions": [{
                    "id": chunk_id.replace("_", "-"), "kind": "generated",
                    "exercise_label": None, "question": f"Explain {chunk_id}.",
                    "answer": f"Source material for {chunk_id}.",
                    "placeholder": "Recall the source", "required": True,
                    "source_ids": ["chapter.txt"],
                }],
            })
        return GenerationResponse(value, finish_reason="stop")


def options(work_dir: Path) -> PipelineOptions:
    return PipelineOptions(
        strategy="chunked", chunk_characters=64, retries=0, work_dir=work_dir,
        profile_overrides={
            "context_window": 32000, "max_output_tokens": 4096,
            "reserved_output_tokens": 4096,
        },
    )


class PartialOutputTests(unittest.TestCase):
    def test_failed_middle_subset_retains_other_subsets_and_resume_only_retries_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chapter.txt"
            source.write_text(SOURCE, encoding="utf-8")
            output = root / "final.json"
            pipeline_options = options(root / "work")
            connector = SubsetConnector({"chunk_0002"})
            with patch("cli_runtime.create_connector", return_value=connector), patch(
                "pipeline.engine.validate_coverage", wraps=validate_coverage,
            ) as coverage, patch("pipeline.engine.validate_output", wraps=validate_output) as checked:
                partial = execute_generation(
                    connector_name="ollama", input_path=source, action="create_qandas",
                    output_path=output, options=pipeline_options,
                )
            self.assertEqual(connector.chunks, ["chunk_0001", "chunk_0002", "chunk_0003"])
            self.assertEqual(checked.call_count, 2)  # Completed subsets only; no final validation.
            coverage.assert_not_called()
            self.assertEqual(partial.metrics["status"], "partial")
            self.assertEqual(partial.metrics["chunk_count"], 2)
            self.assertEqual(partial.metrics["failed_chunks"][0]["chunk_id"], "chunk_0002")
            self.assertTrue(partial.validation["validation_skipped"])
            self.assertTrue(partial.validation["partial"])
            self.assertFalse(partial.validation["valid"])
            self.assertEqual(
                [question["id"] for question in json.loads(output.read_text(encoding="utf-8"))["questions"]],
                ["chunk-0001", "chunk-0003"],
            )
            self.assertTrue(json.loads((root / "work" / "validation.json").read_text())["partial"])

            resumed_connector = SubsetConnector()
            with patch("cli_runtime.create_connector", return_value=resumed_connector), patch(
                "pipeline.engine.validate_coverage", wraps=validate_coverage,
            ) as coverage:
                complete = execute_generation(
                    connector_name="ollama", input_path=source, action="create_qandas",
                    output_path=output, options=pipeline_options,
                )
            self.assertEqual(resumed_connector.chunks, ["chunk_0002"])
            self.assertEqual(coverage.call_count, 1)
            self.assertEqual(complete.metrics["status"], "complete")
            self.assertTrue(complete.validation["valid"])
            self.assertNotIn("partial", complete.validation)
            self.assertEqual(len(json.loads(output.read_text(encoding="utf-8"))["questions"]), 3)

    def test_failed_subset_preserves_text_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chapter.txt"
            source.write_text(SOURCE, encoding="utf-8")
            output = root / "cards.txt"
            connector = SubsetConnector({"chunk_0002"}, text=True)
            with patch("cli_runtime.create_connector", return_value=connector):
                partial = execute_generation(
                    connector_name="ollama", input_path=source, action="create_flashcards",
                    output_path=output, options=options(root / "work"),
                )
            text = output.read_text(encoding="utf-8")
            self.assertIn("chunk_0001", text)
            self.assertIn("chunk_0003", text)
            self.assertNotIn("chunk_0002", text)
            self.assertTrue(partial.validation["partial"])

    def test_aggregation_write_failure_still_writes_completed_subsets_to_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chapter.txt"
            source.write_text(SOURCE, encoding="utf-8")
            output = root / "final.json"
            connector = SubsetConnector()
            from pipeline.checkpoint import atomic_json

            def fail_aggregate_write(path, value):
                if path.parent == root / "work" / "aggregate":
                    raise OSError("Cannot write aggregate diagnostics")
                atomic_json(path, value)

            with patch("cli_runtime.create_connector", return_value=connector), patch(
                "pipeline.engine.atomic_json", side_effect=fail_aggregate_write,
            ), patch("pipeline.engine.validate_coverage") as coverage:
                partial = execute_generation(
                    connector_name="ollama", input_path=source, action="create_qandas",
                    output_path=output, options=options(root / "work"),
                )
            coverage.assert_not_called()
            self.assertTrue(partial.validation["partial"])
            self.assertEqual(partial.metrics["failed_chunks"][0]["stage"], "aggregation")
            self.assertEqual(len(json.loads(output.read_text(encoding="utf-8"))["questions"]), 3)

    def test_subset_write_failure_retains_valid_draft_and_later_subsets(self) -> None:
        for strategy, failed_write in (("chunked", 1), ("multi_pass", 2)):
            with self.subTest(strategy=strategy), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "chapter.txt"
                source.write_text(SOURCE, encoding="utf-8")
                output = root / "final.json"
                connector = SubsetConnector()
                pipeline_options = options(root / "work")
                pipeline_options.strategy = strategy
                pipeline_options.generation_passes = 1
                from pipeline.checkpoint import atomic_json
                writes = 0

                def fail_subset_write(path, value):
                    nonlocal writes
                    if path.parent.name == "generated" and path.name == "chunk_0002.json":
                        writes += 1
                        if writes == failed_write:
                            raise OSError("Cannot write generated subset")
                    atomic_json(path, value)

                with patch("cli_runtime.create_connector", return_value=connector), patch(
                    "pipeline.engine.atomic_json", side_effect=fail_subset_write,
                ), patch("pipeline.engine._Run.extraction", return_value=None), patch(
                    "pipeline.engine.validate_coverage",
                ) as coverage:
                    partial = execute_generation(
                        connector_name="ollama", input_path=source, action="create_qandas",
                        output_path=output, options=pipeline_options,
                    )
                coverage.assert_not_called()
                self.assertEqual(connector.chunks, ["chunk_0001", "chunk_0002", "chunk_0003"])
                self.assertTrue(partial.validation["partial"])
                self.assertEqual(partial.metrics["failed_chunks"][0]["stage"], "write")
                self.assertEqual(partial.metrics["failed_chunks"][0]["chunk_id"], "chunk_0002")
                self.assertEqual(
                    [question["id"] for question in json.loads(output.read_text(encoding="utf-8"))["questions"]],
                    ["chunk-0001", "chunk-0002", "chunk-0003"],
                )

    def test_coverage_repair_failure_retains_valid_draft_and_later_subsets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chapter.txt"
            source.write_text(SOURCE, encoding="utf-8")
            output = root / "final.json"
            connector = SubsetConnector()
            pipeline_options = options(root / "work")
            pipeline_options.strategy = "multi_pass"

            def repair(chunk, value):
                if chunk.chunk_id == "chunk_0002":
                    raise OSError("Cannot write coverage repair")
                return value

            with patch("cli_runtime.create_connector", return_value=connector), patch(
                "pipeline.engine._Run.extraction", return_value=None,
            ), patch("pipeline.engine._Run.coverage_repair", side_effect=repair), patch(
                "pipeline.engine.validate_coverage",
            ) as coverage:
                partial = execute_generation(
                    connector_name="ollama", input_path=source, action="create_qandas",
                    output_path=output, options=pipeline_options,
                )
            coverage.assert_not_called()
            self.assertEqual(connector.chunks, ["chunk_0001", "chunk_0002", "chunk_0003"])
            self.assertEqual(partial.metrics["failed_chunks"][0]["stage"], "coverage_repair")
            self.assertEqual(len(json.loads(output.read_text(encoding="utf-8"))["questions"]), 3)

    def test_recovery_child_write_failure_retains_drafts_and_continues_other_chunks(self) -> None:
        class SplittingConnector(SubsetConnector):
            def generate_response(self, **request):
                if "Chunk: chunk_0001;" in request["user_prompt"]:
                    self.chunks.append("chunk_0001")
                    raise GenerationFailure("Truncated first chunk", truncated=True)
                return super().generate_response(**request)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chapter.txt"
            source.write_text("A complete source paragraph about regression. " * 80, encoding="utf-8")
            output = root / "final.json"
            connector = SplittingConnector()
            pipeline_options = options(root / "work")
            pipeline_options.chunk_characters = 1500
            from pipeline.checkpoint import atomic_text

            def fail_child_write(path, value):
                if path.name == "chunk_0001_01.txt":
                    raise OSError("Cannot write recovery child")
                atomic_text(path, value)

            with patch("cli_runtime.create_connector", return_value=connector), patch(
                "pipeline.engine.atomic_text", side_effect=fail_child_write,
            ), patch("pipeline.engine.validate_coverage") as coverage:
                partial = execute_generation(
                    connector_name="ollama", input_path=source, action="create_qandas",
                    output_path=output, options=pipeline_options,
                )
            coverage.assert_not_called()
            self.assertIn("chunk_0001_01", connector.chunks)
            self.assertIn("chunk_0001_02", connector.chunks)
            self.assertIn("chunk_0002", connector.chunks)
            self.assertEqual(partial.metrics["failed_chunks"][0]["stage"], "chunk_write")
            self.assertEqual(partial.metrics["failed_chunks"][0]["chunk_id"], "chunk_0001_01")
            questions = json.loads(output.read_text(encoding="utf-8"))["questions"]
            self.assertEqual(len(questions), len(connector.chunks) - 1)
            self.assertIn("chunk-0001-01", [question["id"] for question in questions])

    def test_no_completed_subset_still_raises_and_creates_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chapter.txt"
            source.write_text(SOURCE, encoding="utf-8")
            output = root / "final.json"
            connector = SubsetConnector({"chunk_0001", "chunk_0002", "chunk_0003"})
            with patch("cli_runtime.create_connector", return_value=connector):
                with self.assertRaises(ProviderError):
                    execute_generation(
                        connector_name="ollama", input_path=source, action="create_qandas",
                        output_path=output, options=options(root / "work"),
                    )
            self.assertFalse(output.exists())

    def test_cancelled_subset_aborts_without_retaining_downloadable_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chapter.txt"
            source.write_text(SOURCE, encoding="utf-8")
            output = root / "final.json"
            connector = SubsetConnector({"chunk_0002"}, cancelled=True)
            with patch("cli_runtime.create_connector", return_value=connector):
                with self.assertRaises(GenerationCancelled):
                    execute_generation(
                        connector_name="ollama", input_path=source, action="create_qandas",
                        output_path=output, options=options(root / "work"),
                    )
            self.assertEqual(connector.chunks, ["chunk_0001", "chunk_0002"])
            self.assertFalse(output.exists())

    def test_cli_reports_failure_while_retaining_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "chapter.txt"
            source.write_text(SOURCE, encoding="utf-8")
            output = root / "final.json"
            connector = SubsetConnector({"chunk_0002"})
            with patch("cli_runtime.create_connector", return_value=connector), patch(
                "cli_runtime.options_from_args", return_value=options(root / "work"),
            ):
                exit_code = main([
                    "--connector", "ollama", "--input", str(source),
                    "--action", "create_qandas", "--output", str(output),
                ])
            self.assertEqual(exit_code, ProviderError.exit_code)
            self.assertEqual(len(json.loads(output.read_text(encoding="utf-8"))["questions"]), 2)

    def test_failed_atomic_replacement_preserves_existing_partial_and_cleans_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "final.json"
            output.write_text("previous partial output", encoding="utf-8")
            with patch("output_writer.os.replace", side_effect=OSError("replacement failed")):
                with self.assertRaises(OutputWriteError):
                    write_output(output, "new full output")
            self.assertEqual(output.read_text(encoding="utf-8"), "previous partial output")
            self.assertEqual(list(root.iterdir()), [output])


if __name__ == "__main__":
    unittest.main()
