from __future__ import annotations

import io
import json
import base64
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from errors import ApplicationError, GenerationCancelled, InputDocumentError
from notebooklm_converter import artifact_from_metadata
from notebooklm_workflow import (
    NotebookLMSource,
    NotebookLMTask,
    discover_notebooklm_sources,
    execute_notebooklm_task,
    task_from_dict,
    task_to_dict,
)
from pipeline.engine import PipelineOptions, PipelineResult
from pipeline.schemas import get_schema, schema_errors
import study_set
import notebooklm_workflow
from tests.test_notebooklm import PNG_IMAGE, SOURCE_UUID, TIMESTAMP, canonical_podcast, exported_artifact, quiz_payload, source_metadata_map


SECOND_UUID = "66666666-7777-8888-9999-aaaaaaaaaaaa"


def source_export(root: Path, name: str = "alpha.txt", uuid: str | None = SOURCE_UUID,
                  marker: str = "ALPHA_ORIGINAL_SOURCE") -> Path:
    folder = root / "input" / "Sources"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name + ".html")
    path.write_text(f"<html><head><style>STYLE_NOT_SOURCE</style></head><body><h1>{name}</h1><p>{marker}</p><script>SCRIPT_NOT_SOURCE</script></body></html>", encoding="utf-8")
    metadata = {"title": name}
    if uuid is not None:
        metadata["sourceId"] = {"id": uuid}
        source_metadata_map(root, {name + " metadata.json": uuid}, merge=True)
    (folder / (name + " metadata.json")).write_text(json.dumps(metadata), encoding="utf-8")
    return path


def workflow_config(root: Path, types: list[str], *, model: str | None = "configured-model",
                    formats: list[str] | None = None, pipeline: dict | None = None) -> Path:
    config = {"files": [{"type": "notebooklm", "input": "input", "output": "output", "types": types}],
              "connector": "openai"}
    if model is not None:
        config["model"] = model
    if formats is not None:
        config["files"][0]["formats"] = formats
    if pipeline is not None:
        config["pipeline"] = pipeline
    path = root / "study-set-config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def generated_payload(action: str, source_name: str = "alpha.txt") -> dict:
    if action == "create_podcasts":
        return canonical_podcast()
    if action == "create_qandas":
        return {"title": "Questions", "description": "From the original source", "questions": [{
            "id": "question-one", "kind": "generated", "exercise_label": None, "question": "What does the source say?",
            "answer": "The original answer.", "placeholder": "Your answer", "required": True, "source_ids": [source_name],
        }]}
    if action == "create_reports":
        return {"title": "Report", "executive_summary": "Original source summary", "sections": [{
            "title": "Analysis", "content": "Source-based analysis", "claims": [{"claim": "Observed claim", "citations": [{
                "source_id": source_name, "page": None, "chunk_id": None,
            }]}],
        }], "conclusions": "Source-based conclusion"}
    if action == "create_infographics":
        return {"title": "Infographic", "subtitle": "From observed text", "sections": [{
            "type": "quote", "title": "Observed text", "value": "OCR_GENERATED_CONTENT", "label": None,
            "items": [], "panel": None, "span": "full",
        }]}
    if action == "create_slides":
        return {"title": "Slides", "slides": [{"title": "Observed title", "subtitle": "",
            "bullets": ["Observed one", "Observed two", "Observed three"], "speaker_notes": "Observed notes",
            "image_query": "observed diagram", "source_ids": [source_name]}]}
    raise AssertionError("Unexpected action: " + action)


def pipeline_result(payload: dict, work_dir: Path, *, partial: bool = False) -> PipelineResult:
    return PipelineResult(json.dumps(payload), {"provider_calls": 1, "status": "failed" if partial else "completed"},
                          {"valid": not partial, "errors": [], **({"partial": True} if partial else {})}, work_dir)


class NotebookLMWorkflowPlanningTests(unittest.TestCase):
    def test_source_uuid_is_folder_identity_and_original_filename_is_citation_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = source_export(root)
            sources = discover_notebooklm_sources(root / "input")
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0].path, path)
            self.assertEqual(sources[0].source_id, SOURCE_UUID)
            self.assertEqual(sources[0].source_name, "alpha.txt")
            workflow_config(root, ["qanda"], formats=["json"])
            with patch("study_set._timestamp", return_value=TIMESTAMP):
                jobs = study_set.plan_study_sets(root)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].notebooklm_task.mode, "source")
            self.assertEqual(jobs[0].output, root / "output" / SOURCE_UUID / "qandas" / f"{TIMESTAMP}.json")

    def test_podcasts_always_use_original_sources_and_ignore_existing_wav_exports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = source_export(root)
            exported_artifact(root, "Unusable Audio", "ARTIFACT_TYPE_AUDIO_OVERVIEW", b"WAV_ONLY_DO_NOT_PROCESS", extension=".wav")
            workflow_config(root, ["podcast"], formats=["json"])
            with patch("study_set._timestamp", return_value=TIMESTAMP):
                jobs = study_set.plan_study_sets(root)
            self.assertEqual(len(jobs), 1)
            task = jobs[0].notebooklm_task
            self.assertEqual(task.mode, "source")
            self.assertEqual(task.source.path, original)
            self.assertIsNone(task.artifact)
            self.assertFalse(any(path.suffix == ".wav" for path in task.source.dependencies))

    def test_source_only_notebook_plans_all_missing_types_and_default_companion_formats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_export(root)
            self.assertFalse((root / "input" / "Artifacts").exists())
            workflow_config(root, list(study_set.STUDY_TYPES))
            jobs = study_set.plan_study_sets(root)
            self.assertEqual(len(jobs), 21)
            self.assertEqual({job.action for job in jobs}, set(study_set.STUDY_TYPES.values()))
            self.assertTrue(all(job.notebooklm_task.mode == "source" for job in jobs))
            self.assertTrue(all(job.output.relative_to(root / "output").parts[0] == SOURCE_UUID for job in jobs))
            for action in study_set.STUDY_TYPES.values():
                work_dirs = {job.args.work_dir for job in jobs if job.action == action}
                self.assertEqual(len(work_dirs), 1)

    def test_existing_and_missing_artifact_types_are_planned_separately_per_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_export(root)
            source_export(root, "beta.txt", SECOND_UUID, "BETA_ORIGINAL_SOURCE")
            exported_artifact(root, "Alpha Quiz", "APP_TYPE_QUIZ", quiz_payload())
            workflow_config(root, ["quiz", "qanda", "report", "podcast"], formats=["json"])
            jobs = study_set.plan_study_sets(root)
            actual = {(job.notebooklm_task.source.source_id, job.action, job.notebooklm_task.mode) for job in jobs}
            self.assertEqual(len(jobs), 8)
            self.assertEqual(actual, {
                (SOURCE_UUID, "create_quizzes", "convert"), (SECOND_UUID, "create_quizzes", "source"),
                (SOURCE_UUID, "create_qandas", "source"), (SECOND_UUID, "create_qandas", "source"),
                (SOURCE_UUID, "create_reports", "source"), (SECOND_UUID, "create_reports", "source"),
                (SOURCE_UUID, "create_podcasts", "source"), (SECOND_UUID, "create_podcasts", "source"),
            })

    def test_connector_and_model_are_required_only_when_generation_is_needed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_export(root)
            exported_artifact(root, "Quiz", "APP_TYPE_QUIZ", quiz_payload())
            workflow_config(root, ["quiz"], model=None, formats=["json"])
            self.assertEqual(study_set.plan_study_sets(root)[0].notebooklm_task.mode, "convert")
            workflow_config(root, ["quiz", "report"], model=None, formats=["json"])
            with self.assertRaisesRegex(ApplicationError, "model"):
                study_set.plan_study_sets(root)

    def test_podcast_without_original_sources_does_not_fall_back_to_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exported_artifact(root, "Audio", "ARTIFACT_TYPE_AUDIO_OVERVIEW", b"RIFFaudio", extension=".wav")
            workflow_config(root, ["podcast"], formats=["json"])
            with self.assertRaises(InputDocumentError):
                study_set.plan_study_sets(root)

    def test_missing_source_map_stops_with_the_required_path_and_generic_json_template(self) -> None:
        for mode in ("source", "convert"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                if mode == "source":
                    source_export(root)
                else:
                    exported_artifact(root, "Quiz", "APP_TYPE_QUIZ", quiz_payload())
                mapping = root / "input" / "metadata" / "sources.metadata.json"
                mapping.unlink()
                workflow_config(root, ["qanda" if mode == "source" else "quiz"], formats=["json"])
                with patch("notebooklm_workflow.create_connector") as connector, patch("notebooklm_workflow.run_pipeline") as pipeline:
                    with self.assertRaises(InputDocumentError) as caught:
                        study_set.plan_study_sets(root)
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        self.assertEqual(study_set.generate_study_sets(root), InputDocumentError.exit_code)
                message = str(caught.exception)
                self.assertIn("sources.metadata.json", message)
                self.assertIn('{"{metadata.json}": {"id": "source_id"}}', message)
                self.assertIn("sources.metadata.json", stderr.getvalue())
                self.assertIn('{"{metadata.json}": {"id": "source_id"}}', stderr.getvalue())
                self.assertEqual(stdout.getvalue(), "")
                connector.assert_not_called()
                pipeline.assert_not_called()
                self.assertFalse(mapping.exists())
                self.assertFalse((root / "output").exists())

    def test_source_without_an_embedded_uuid_uses_its_exact_metadata_filename_in_the_map(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_export(root, uuid=None)
            mapping = source_metadata_map(root, {"alpha.txt metadata.json": SOURCE_UUID})
            source = discover_notebooklm_sources(root / "input")[0]
            self.assertEqual(source.source_id, SOURCE_UUID)
            self.assertEqual(source.source_name, "alpha.txt")
            self.assertIn(mapping, source.dependencies)

    def test_missing_source_metadata_entry_stops_instead_of_using_its_embedded_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_export(root)
            source_metadata_map(root, {"unrelated.txt metadata.json": SECOND_UUID})
            workflow_config(root, ["qanda"], formats=["json"])
            with self.assertRaises(InputDocumentError) as caught:
                study_set.plan_study_sets(root)
            self.assertIn("alpha.txt metadata.json", str(caught.exception))
            self.assertIn("sources.metadata.json", str(caught.exception))
            self.assertFalse((root / "output").exists())

    def test_required_source_map_overrides_a_conflicting_embedded_source_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_export(root)
            source_metadata_map(root, {"alpha.txt metadata.json": SECOND_UUID})
            workflow_config(root, ["qanda"], formats=["json"])
            jobs = study_set.plan_study_sets(root)
            self.assertEqual(jobs[0].notebooklm_task.source.source_id, SECOND_UUID)
            self.assertEqual(jobs[0].notebooklm_task.source.source_name, "alpha.txt")
            self.assertEqual(jobs[0].output.relative_to(root / "output").parts[0], SECOND_UUID)

    def test_artifact_uuid_disagreement_with_the_authoritative_map_fails_association(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_export(root)
            metadata, _ = exported_artifact(root, "Quiz", "APP_TYPE_QUIZ", quiz_payload())
            source_metadata_map(root, {"alpha.txt metadata.json": SECOND_UUID})
            workflow_config(root, ["quiz"], formats=["json"])
            self.assertEqual(artifact_from_metadata(metadata).metadata["sources"][0]["sourceId"]["id"], SOURCE_UUID)
            with self.assertRaisesRegex(InputDocumentError, SOURCE_UUID):
                study_set.plan_study_sets(root)

    def test_explicitly_shared_artifact_retains_all_original_references_in_each_uuid_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_export(root)
            source_export(root, "beta.txt", SECOND_UUID, "BETA_ORIGINAL_SOURCE")
            metadata, _ = exported_artifact(root, "Mixed Quiz", "APP_TYPE_QUIZ", quiz_payload())
            value = json.loads(metadata.read_text(encoding="utf-8"))
            value["sources"].append({"sourceId": {"id": SECOND_UUID}})
            value["source_map"] = {SOURCE_UUID: "alpha.txt", SECOND_UUID: "beta.txt"}
            metadata.write_text(json.dumps(value), encoding="utf-8")
            workflow_config(root, ["quiz"], model=None, formats=["json"])
            jobs = study_set.plan_study_sets(root)
            self.assertEqual(len(jobs), 2)
            self.assertEqual({job.notebooklm_task.source.source_id for job in jobs}, {SOURCE_UUID, SECOND_UUID})
            for job in jobs:
                self.assertEqual(job.notebooklm_task.mode, "convert")
                with patch("notebooklm_workflow.create_connector") as connector, patch("notebooklm_workflow.run_pipeline") as pipeline:
                    execute_notebooklm_task(job.notebooklm_task, job.output)
                connector.assert_not_called()
                pipeline.assert_not_called()
                payload = json.loads(job.output.read_text(encoding="utf-8"))
                self.assertEqual(payload["questions"][0]["sources"], ["alpha.txt", "beta.txt"])
                sidecar = json.loads(job.output.with_suffix(".metadata.json").read_text(encoding="utf-8"))
                self.assertEqual(sidecar["original_metadata"]["sources"], value["sources"])

    def test_recorded_uuid_controls_association_when_filename_hint_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_export(root)
            source_export(root, "beta.txt", SECOND_UUID, "BETA_ORIGINAL_SOURCE")
            metadata, _ = exported_artifact(root, "Alpha Quiz", "APP_TYPE_QUIZ", quiz_payload())
            value = json.loads(metadata.read_text(encoding="utf-8"))
            value["source_map"] = {SOURCE_UUID: "beta.txt"}
            metadata.write_text(json.dumps(value), encoding="utf-8")
            workflow_config(root, ["quiz"], formats=["json"])
            jobs = study_set.plan_study_sets(root)
            self.assertEqual({(job.notebooklm_task.source.source_id, job.notebooklm_task.mode) for job in jobs},
                             {(SOURCE_UUID, "convert"), (SECOND_UUID, "source")})

    def test_shared_artifact_with_a_missing_source_export_reports_the_unresolved_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_export(root)
            metadata, _ = exported_artifact(root, "Shared Quiz", "APP_TYPE_QUIZ", quiz_payload())
            value = json.loads(metadata.read_text(encoding="utf-8"))
            value["sources"].append({"sourceId": {"id": SECOND_UUID}})
            value["source_map"] = {SOURCE_UUID: "alpha.txt", SECOND_UUID: "beta.txt"}
            metadata.write_text(json.dumps(value), encoding="utf-8")
            source_metadata_map(root, {"beta.txt metadata.json": SECOND_UUID}, merge=True)
            workflow_config(root, ["quiz"], model=None, formats=["json"])
            with patch("notebooklm_workflow.create_connector") as connector, patch("notebooklm_workflow.run_pipeline") as pipeline:
                with self.assertRaisesRegex(InputDocumentError, SECOND_UUID):
                    study_set.plan_study_sets(root)
            connector.assert_not_called()
            pipeline.assert_not_called()
            self.assertFalse((root / "output").exists())


class NotebookLMWorkflowExecutionTests(unittest.TestCase):
    def source_task(self, root: Path, action: str = "create_qandas") -> NotebookLMTask:
        source_export(root)
        source = discover_notebooklm_sources(root / "input")[0]
        return NotebookLMTask("source", source, action)

    def test_source_generation_uses_visible_original_text_and_configured_credentials_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = self.source_task(root)
            options = PipelineOptions(strategy="baseline", retries=0, work_dir=root / "work",
                                      max_output_tokens=2048, profile_overrides={"context_window": 8192})
            output = root / "output" / "questions.json"
            fake_connector = SimpleNamespace(model="chosen-model")
            with patch("notebooklm_workflow.create_connector", return_value=fake_connector) as connector, \
                    patch("notebooklm_workflow.run_pipeline", return_value=pipeline_result(generated_payload(task.action), root / "work")) as pipeline:
                result = execute_notebooklm_task(task, output, connector_name="openai", model="chosen-model", api_key="private-key", options=options)
            connector.assert_called_once_with("openai", model="chosen-model", api_key="private-key")
            request = pipeline.call_args.kwargs
            self.assertIn("ALPHA_ORIGINAL_SOURCE", request["source_text"])
            self.assertNotIn("SCRIPT_NOT_SOURCE", request["source_text"])
            self.assertNotIn("STYLE_NOT_SOURCE", request["source_text"])
            self.assertEqual(request["source_path"].name, "alpha.txt")
            self.assertIs(request["connector"], fake_connector)
            self.assertEqual(request["options"].max_output_tokens, 2048)
            self.assertEqual(request["options"].profile_overrides["context_window"], 8192)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(schema_errors(payload, get_schema(task.action)), [])
            self.assertNotIn("provenance", payload)
            self.assertEqual(payload["questions"][0]["source_ids"], ["alpha.txt"])
            self.assertEqual(result.metrics["provider_calls"], 1)
            self.assertTrue(output.with_suffix(".metadata.json").is_file())

    def test_converted_existing_quiz_uses_its_associated_original_filename_without_model_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_export(root)
            metadata, _ = exported_artifact(root, "Quiz", "APP_TYPE_QUIZ", quiz_payload())
            source = discover_notebooklm_sources(root / "input")[0]
            task = NotebookLMTask("convert", source, "create_quizzes", artifact_from_metadata(metadata))
            output = root / "output" / "quiz.json"
            with patch("notebooklm_workflow.create_connector") as connector, patch("notebooklm_workflow.run_pipeline") as pipeline:
                execute_notebooklm_task(task, output)
            connector.assert_not_called()
            pipeline.assert_not_called()
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["questions"][0]["sources"], ["alpha.txt"])

    def test_cli_podcast_companions_generate_once_from_source_and_never_process_wav(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_export(root)
            exported_artifact(root, "Audio", "ARTIFACT_TYPE_AUDIO_OVERVIEW", b"WAV_ONLY_DO_NOT_PROCESS", extension=".wav")
            workflow_config(root, ["podcast"])
            with patch("study_set._timestamp", return_value=TIMESTAMP), \
                    patch("notebooklm_media.extract_podcast", side_effect=AssertionError("WAV must be skipped")), \
                    patch("notebooklm_workflow.create_connector", return_value=SimpleNamespace(model="configured-model")) as connector, \
                    patch("notebooklm_workflow.run_pipeline", return_value=pipeline_result(canonical_podcast(), root / "work")) as pipeline, \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(study_set.generate_study_sets(root), 0)
            connector.assert_called_once()
            pipeline.assert_called_once()
            self.assertIn("ALPHA_ORIGINAL_SOURCE", pipeline.call_args.kwargs["source_text"])
            self.assertNotIn("WAV_ONLY_DO_NOT_PROCESS", pipeline.call_args.kwargs["source_text"])
            folder = root / "output" / SOURCE_UUID / "podcasts"
            self.assertEqual(json.loads((folder / f"{TIMESTAMP}_episode0.json").read_text(encoding="utf-8")), canonical_podcast())
            self.assertIn("Imported audio", (folder / f"{TIMESTAMP}_episode0.md").read_text(encoding="utf-8"))

    def test_ocr_artifact_generation_uses_recognized_text_with_only_its_associated_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_export(root)
            source_export(root, "beta.txt", SECOND_UUID, "BETA_ORIGINAL_SOURCE")
            metadata, _ = exported_artifact(root, "Alpha Diagram", "ARTIFACT_TYPE_INFOGRAPHIC", PNG_IMAGE, extension=".png")
            sources = discover_notebooklm_sources(root / "input")
            source = next(item for item in sources if item.source_id == SOURCE_UUID)
            task = NotebookLMTask("ocr", source, "create_infographics", artifact_from_metadata(metadata))
            with patch("notebooklm_media.extract_ocr_text", return_value="OBSERVED_ALPHA_IMAGE_TEXT") as ocr, \
                    patch("notebooklm_workflow.create_connector", return_value=SimpleNamespace(model="chosen-model")), \
                    patch("notebooklm_workflow.run_pipeline", return_value=pipeline_result(generated_payload(task.action), root / "work")) as pipeline:
                execute_notebooklm_task(task, root / "output" / "infographic.json", connector_name="openai", model="chosen-model", options=PipelineOptions(work_dir=root / "work"))
            ocr.assert_called_once()
            text = pipeline.call_args.kwargs["source_text"]
            self.assertIn("OBSERVED_ALPHA_IMAGE_TEXT", text)
            self.assertIn("ALPHA_ORIGINAL_SOURCE", text)
            self.assertNotIn("BETA_ORIGINAL_SOURCE", text)
            self.assertEqual(pipeline.call_args.kwargs["source_path"].name, "alpha.txt")

    def test_missing_ocr_tool_fails_before_model_calls_and_produces_no_learning_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = self.source_task(root, "create_infographics")
            metadata, _ = exported_artifact(root, "Diagram", "ARTIFACT_TYPE_INFOGRAPHIC", PNG_IMAGE, extension=".png")
            task = replace(task, mode="ocr", artifact=artifact_from_metadata(metadata))
            output = root / "output" / "infographic.json"
            with patch("notebooklm_media.extract_ocr_text", side_effect=InputDocumentError("Tesseract is missing")), \
                    patch("notebooklm_workflow.create_connector") as connector, patch("notebooklm_workflow.run_pipeline") as pipeline:
                with self.assertRaisesRegex(InputDocumentError, "Tesseract"):
                    execute_notebooklm_task(task, output, connector_name="openai", model="chosen-model")
            connector.assert_not_called()
            pipeline.assert_not_called()
            self.assertFalse(output.exists())

    def test_companion_formats_reuse_one_canonical_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = self.source_task(root, "create_reports")
            options = PipelineOptions(strategy="baseline", work_dir=root / "shared-work")
            with patch("notebooklm_workflow.create_connector", return_value=SimpleNamespace(model="chosen-model")), \
                    patch("notebooklm_workflow.run_pipeline", return_value=pipeline_result(generated_payload(task.action), options.work_dir)) as pipeline:
                outputs = []
                for extension in (".json", ".md", ".html"):
                    output = root / "output" / ("report" + extension)
                    execute_notebooklm_task(task, output, connector_name="openai", model="chosen-model", options=options)
                    outputs.append(output)
            pipeline.assert_called_once()
            self.assertTrue(all(output.is_file() and output.with_suffix(".metadata.json").is_file() for output in outputs))
            self.assertIn("Source-based analysis", outputs[1].read_text(encoding="utf-8"))
            self.assertIn("Source-based analysis", outputs[2].read_text(encoding="utf-8"))

    def test_generated_infographic_svg_assets_survive_cached_companions_in_separate_output_folders(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(notebooklm_workflow._RUN_CACHE, {}, clear=True):
            root = Path(directory)
            task = self.source_task(root, "create_infographics")
            options = PipelineOptions(work_dir=root / "shared-work")
            diagram = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 60">'
                       '<rect x="5" y="5" width="110" height="50" fill="#447799"/>'
                       '<text x="10" y="30">GENERATED_DIAGRAM</text></svg>')
            payload = {"title": "Diagram", "subtitle": "Source-based visualization", "sections": [{
                "type": "svg", "title": "Observed relationship", "value": "assets/diagram.svg", "label": None,
                "items": [], "panel": None, "span": "full",
            }], "assets": [{"id": "assets/diagram.svg", "svg": diagram}]}
            results, outputs = [], []
            with patch("notebooklm_workflow.create_connector", return_value=SimpleNamespace(model="chosen-model")) as connector, \
                    patch("notebooklm_workflow.run_pipeline", return_value=pipeline_result(payload, options.work_dir)) as pipeline:
                for index, extension in enumerate((".json", ".md", ".html", ".svg")):
                    output = root / "output" / extension[1:] / ("infographic" + extension)
                    results.append(execute_notebooklm_task(task, output, connector_name="openai", model="chosen-model", options=options))
                    outputs.append(output)
                    if index == 0:
                        # A later worker must recover the diagram from its durable
                        # canonical result rather than relying on process memory.
                        notebooklm_workflow._RUN_CACHE.clear()
            connector.assert_called_once()
            pipeline.assert_called_once()
            canonical = json.loads(outputs[0].read_text(encoding="utf-8"))
            self.assertEqual(set(canonical), {"title", "subtitle", "sections"})
            self.assertEqual(canonical["sections"][0]["value"], "assets/diagram.svg")
            saved_diagrams = [(output.parent / "assets" / "diagram.svg").read_text(encoding="utf-8") for output in outputs]
            self.assertTrue(all(value == saved_diagrams[0] for value in saved_diagrams))
            self.assertIn("GENERATED_DIAGRAM", saved_diagrams[0])
            self.assertIn("](assets/diagram.svg)", outputs[1].read_text(encoding="utf-8"))
            self.assertIn("GENERATED_DIAGRAM", outputs[2].read_text(encoding="utf-8"))
            poster = ET.fromstring(outputs[3].read_text(encoding="utf-8"))
            embedded = poster.find("{http://www.w3.org/2000/svg}image").attrib["href"]
            self.assertTrue(embedded.startswith("data:image/svg+xml;base64,"))
            self.assertIn("GENERATED_DIAGRAM", base64.b64decode(embedded.split(",", 1)[1]).decode("utf-8"))
            self.assertEqual([result.metrics["provider_calls"] for result in results], [1, 0, 0, 0])
            self.assertTrue(all(result.validation["valid"] for result in results))

    def test_loose_slide_images_are_ocr_processed_in_numeric_order_then_generated_from_one_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_export(root)
            metadata, deck = exported_artifact(root, "Images Only", "ARTIFACT_TYPE_SLIDES", b"")
            deck.unlink()
            deck.mkdir()
            image_names = ("slide10.png", "slide2.png", "slide1")
            for name in image_names:
                (deck / name).write_bytes(PNG_IMAGE)
            (deck / "README.txt").write_text("Not slide content", encoding="utf-8")
            (deck / "broken.png").write_text("Not an image", encoding="utf-8")
            workflow_config(root, ["slide"], formats=["json"])
            job = study_set.plan_study_sets(root)[0]
            self.assertEqual(job.notebooklm_task.mode, "ocr")
            self.assertEqual(job.notebooklm_task.artifact.metadata_path, metadata)
            observed = {"slide1": "FIRST_OBSERVED_TEXT", "slide2.png": "SECOND_OBSERVED_TEXT", "slide10.png": "TENTH_OBSERVED_TEXT"}
            with patch("notebooklm_media._ocr", side_effect=lambda image, check: observed[image.name]) as ocr, \
                    patch("notebooklm_workflow.create_connector", return_value=SimpleNamespace(model="configured-model")), \
                    patch("notebooklm_workflow.run_pipeline", return_value=pipeline_result(generated_payload(job.action), job.args.work_dir)) as pipeline:
                execute_notebooklm_task(job.notebooklm_task, job.output, connector_name="openai", model="configured-model",
                                       options=PipelineOptions(work_dir=job.args.work_dir))
            self.assertEqual([call.args[0].name for call in ocr.call_args_list], ["slide1", "slide2.png", "slide10.png"])
            pipeline.assert_called_once()
            source_text = pipeline.call_args.kwargs["source_text"]
            self.assertIn("ALPHA_ORIGINAL_SOURCE", source_text)
            self.assertNotIn("Not slide content", source_text)
            self.assertLess(source_text.index("FIRST_OBSERVED_TEXT"), source_text.index("SECOND_OBSERVED_TEXT"))
            self.assertLess(source_text.index("SECOND_OBSERVED_TEXT"), source_text.index("TENTH_OBSERVED_TEXT"))
            self.assertEqual(pipeline.call_args.kwargs["source_path"].name, "alpha.txt")
            self.assertEqual(json.loads(job.output.read_text(encoding="utf-8"))["slides"][0]["source_ids"], ["alpha.txt"])
            sidecar = json.loads(job.output.with_suffix(".metadata.json").read_text(encoding="utf-8"))
            copied = {Path(item["path"]).name: job.output.parent / item["path"] for item in sidecar["original_media"]}
            self.assertTrue(all(copied[name].read_bytes() == PNG_IMAGE for name in image_names))

    def test_partial_generation_remains_partial_instead_of_becoming_a_completed_companion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = self.source_task(root)
            with patch("notebooklm_workflow.create_connector", return_value=SimpleNamespace(model="chosen-model")), \
                    patch("notebooklm_workflow.run_pipeline", return_value=pipeline_result(generated_payload(task.action), root / "work", partial=True)):
                result = execute_notebooklm_task(task, root / "output" / "questions.json", connector_name="openai", model="chosen-model", options=PipelineOptions(work_dir=root / "work"))
            self.assertTrue(result.validation["partial"])
            self.assertEqual(result.metrics["status"], "failed")

    def test_retry_of_partial_output_reenters_pipeline_instead_of_reusing_failed_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = self.source_task(root)
            options = PipelineOptions(work_dir=root / "work")
            responses = [pipeline_result(generated_payload(task.action), root / "work", partial=True),
                         pipeline_result(generated_payload(task.action), root / "work")]
            with patch("notebooklm_workflow.create_connector", return_value=SimpleNamespace(model="chosen-model")), \
                    patch("notebooklm_workflow.run_pipeline", side_effect=responses) as pipeline:
                first = execute_notebooklm_task(task, root / "output" / "questions.json", connector_name="openai", model="chosen-model", options=options)
                second = execute_notebooklm_task(task, root / "output" / "questions.json", connector_name="openai", model="chosen-model", options=options)
            self.assertTrue(first.validation["partial"])
            self.assertEqual(pipeline.call_count, 2)
            self.assertFalse(second.validation.get("partial", False))
            self.assertTrue(second.validation["valid"])

    def test_task_serialization_retains_uuid_filename_and_original_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = self.source_task(root)
            restored = task_from_dict(task_to_dict(task))
            self.assertEqual(restored, task)
            self.assertEqual(restored.source.source_id, SOURCE_UUID)
            self.assertEqual(restored.source.source_name, "alpha.txt")

    def test_cancellation_prevents_connector_or_model_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = self.source_task(root)
            with patch("notebooklm_workflow.create_connector") as connector, patch("notebooklm_workflow.run_pipeline") as pipeline:
                with self.assertRaises(GenerationCancelled):
                    execute_notebooklm_task(task, root / "output" / "questions.json", connector_name="openai", model="chosen-model", cancel_check=lambda: True)
            connector.assert_not_called()
            pipeline.assert_not_called()


if __name__ == "__main__":
    unittest.main()
