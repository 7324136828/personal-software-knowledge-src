from __future__ import annotations

import html
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from errors import ApplicationError, GenerationCancelled, InputDocumentError
import server
import study_package
from pipeline.schemas import get_schema, schema_errors
from tests.test_notebooklm import PNG_IMAGE, SOURCE_UUID, canonical_podcast, canonical_slides
import base64


SOURCE_MAP_PATH = "input/metadata/sources.metadata.json"
SOURCE_MAP = {"source.txt metadata.json": {"id": SOURCE_UUID}}


def notebooklm_package(
    *, types: list[str] | None = None, output: str = "output", source_map: bool = True,
    input_directory: str = "input",
) -> bytes:
    config = {"files": [{
        "type": "notebooklm", "input": input_directory, "output": output,
        "types": types or ["quiz", "slide"], "formats": ["json"],
    }]}
    metadata = {"status": "ARTIFACT_STATUS_READY", "sources": [{"sourceId": {"id": SOURCE_UUID}}]}
    quiz = {"quiz": [{"question": "What is preserved?", "hint": "Existing exports",
                       "answerOptions": [{"text": "Answers", "isCorrect": True, "rationale": "Correct"},
                                         {"text": "Nothing", "isCorrect": False, "rationale": "Incorrect"},
                                         {"text": "Unrelated files", "isCorrect": False, "rationale": "Incorrect"},
                                         {"text": "Newly generated answers", "isCorrect": False, "rationale": "Incorrect"}]}]}
    members = {
        "study-set-config.json": json.dumps(config),
        "input/Artifacts/Quiz metadata.json": json.dumps({**metadata, "title": "Quiz", "type": "ARTIFACT_TYPE_APP",
            "app": {"generationOptions": {"appType": "APP_TYPE_QUIZ"}}}),
        "input/Artifacts/Quiz": '<app-root data-app-data="' + html.escape(json.dumps(quiz), quote=True) + '"></app-root>',
        "input/Artifacts/Audio metadata.json": json.dumps({**metadata, "title": "Audio", "type": "ARTIFACT_TYPE_AUDIO_OVERVIEW"}),
        "input/Artifacts/Audio.wav": b"RIFF\x00\x00\x00\x00WAVEexported audio",
        "input/Artifacts/Audio.content.json": json.dumps(canonical_podcast()),
        "input/Artifacts/Deck metadata.json": json.dumps({**metadata, "title": "Deck", "type": "ARTIFACT_TYPE_SLIDES"}),
        "input/Artifacts/Deck/slide_1": b"\x89PNG\r\n\x1a\nexported slide",
        "input/Artifacts/Deck/deck.pdf": b"%PDF-1.4\nexported slides",
        "input/Artifacts/Deck.content.json": json.dumps(canonical_slides()),
        "input/Artifacts/Diagram metadata.json": json.dumps({**metadata, "title": "Diagram", "type": "ARTIFACT_TYPE_INFOGRAPHIC"}),
        "input/Artifacts/Diagram.png": PNG_IMAGE,
        "input/Artifacts/Diagram.content.json": json.dumps({
            "title": "Diagram", "subtitle": "", "sections": [{
                "type": "svg", "title": "Diagram", "value": "assets/diagram.svg", "label": None,
                "items": [], "panel": None, "span": "full",
            }],
            "assets": [{"id": "assets/diagram.svg", "svg": '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1" viewBox="0 0 1 1"><image width="1" height="1" href="data:image/png;base64,' + base64.b64encode(PNG_IMAGE).decode("ascii") + '"/></svg>'}],
        }),
    }
    if source_map:
        members[SOURCE_MAP_PATH] = json.dumps(SOURCE_MAP)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return stream.getvalue()


class NotebookLMServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.history_patch = patch.object(server, "HISTORY_DIR", Path(self.temporary.name) / "history")
        self.history_patch.start()
        self.dispatcher_patch = patch("server._ensure_queue_dispatcher")
        self.dispatcher_patch.start()
        server.ACTIVE_CONVERSIONS.clear()
        server.DISPATCHED_CONVERSIONS.clear()
        server.CANCEL_EVENTS.clear()
        server.QUEUED_API_KEYS.clear()
        self.client = TestClient(server.app)

    def tearDown(self) -> None:
        self.dispatcher_patch.stop()
        self.history_patch.stop()
        self.temporary.cleanup()

    def import_package(self, **kwargs) -> list[dict]:
        response = self.client.post("/api/study-sets/import", files={
            "file": ("notebooklm.zip", notebooklm_package(**kwargs), "application/zip"),
        })
        self.assertEqual(response.status_code, 200, response.text)
        return [server._read_record(identifier) for identifier in response.json()["session_ids"]]

    def test_conversion_only_import_requires_source_map_before_any_model_or_history_work(self) -> None:
        with patch("server.execute_generation") as generate, \
                patch("server.options_from_args") as options, \
                patch("notebooklm_workflow.execute_notebooklm_task") as execute:
            response = self.client.post("/api/study-sets/import", files={
                "file": ("missing-map.zip", notebooklm_package(types=["quiz"], source_map=False), "application/zip"),
            })
        self.assertEqual(response.status_code, 400, response.text)
        detail = response.json()["detail"]
        self.assertIn("metadata/sources.metadata.json", detail.replace("\\", "/"))
        self.assertIn('{"{metadata.json}":{"id":"source_id"}}', detail.replace(" ", ""))
        generate.assert_not_called()
        options.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(server._records_on_disk(), [])

    def test_import_stages_extensionless_apps_and_canonical_slide_directories_without_model(self) -> None:
        with patch("server.options_from_args", side_effect=AssertionError("No provider options needed")), \
                patch("server.execute_generation") as generate:
            records = self.import_package()
        generate.assert_not_called()
        self.assertEqual(len(records), 2)
        self.assertEqual({record["action"] for record in records}, {"create_quizzes", "create_slides"})
        for record in records:
            directory = server._conversion_dir(record["id"])
            self.assertEqual(record["input_type"], "notebooklm")
            self.assertEqual(record["connector"], "notebooklm")
            self.assertIsNone(record["model"])
            self.assertEqual(record["options"], {})
            self.assertTrue((directory / record["input_path"]).is_file())
            self.assertTrue((directory / record["notebooklm_metadata_path"]).is_file())
            staged_map = directory / "package" / SOURCE_MAP_PATH
            self.assertEqual(json.loads(staged_map.read_text(encoding="utf-8")), SOURCE_MAP)
            task = server._notebooklm_task_from_record(record, directory)
            self.assertEqual(task.source.source_id, SOURCE_UUID)
            self.assertIn(staged_map, task.source.dependencies)
            self.assertIn(str(staged_map.relative_to(directory)), record["notebooklm_dependency_paths"])
        slides = next(record for record in records if record["action"] == "create_slides")
        self.assertTrue((server._conversion_dir(slides["id"]) / "package/input/Artifacts/Deck/slide_1").is_file())
        self.assertTrue((server._conversion_dir(slides["id"]) / "package/input/Artifacts/Deck.content.json").is_file())

    def test_artifacts_input_stages_required_map_from_notebook_root_without_sources_directory(self) -> None:
        record = self.import_package(types=["quiz"], input_directory="input/Artifacts")[0]
        directory = server._conversion_dir(record["id"])
        task = server._notebooklm_task_from_record(record, directory)
        staged_map = directory / "package" / SOURCE_MAP_PATH
        self.assertEqual(task.source.source_id, SOURCE_UUID)
        self.assertIn(staged_map, task.source.dependencies)
        self.assertEqual(json.loads(staged_map.read_text(encoding="utf-8")), SOURCE_MAP)

    def test_failed_conversion_retries_from_durable_metadata_without_provider(self) -> None:
        record = self.import_package(types=["quiz"])[0]
        with patch("server.execute_generation", side_effect=AssertionError("Provider called")), \
                patch("server._options_from_record", side_effect=AssertionError("Provider options called")), \
                patch("notebooklm_workflow.convert_notebooklm_artifact", side_effect=InputDocumentError("Retry conversion")):
            response = self.client.post(f"/api/history/{record['id']}/continue")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(server._read_record(record["id"])["status"], "failed")
        with patch("server.execute_generation", side_effect=AssertionError("Provider called")), \
                patch("server._options_from_record", side_effect=AssertionError("Provider options called")):
            response = self.client.post(f"/api/history/{record['id']}/continue")
        self.assertEqual(response.status_code, 200, response.text)
        completed = server._read_record(record["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["run_attempts"], 2)
        result = json.loads(response.json()["output_text"])
        self.assertIn("What is preserved?", json.dumps(result))
        self.assertEqual(list(result), ["title", "description", "questions"])
        self.assertNotIn("provenance", result)
        output = server._conversion_dir(record["id"]) / completed["output_path"]
        self.assertTrue(output.with_suffix(".metadata.json").is_file())

    def test_conversion_retry_keeps_mapped_uuid_after_original_upload_package_is_removed(self) -> None:
        import_roots = []
        prepare = study_package.prepare_study_set_archive

        def capture_prepared(*args, **kwargs):
            prepared = prepare(*args, **kwargs)
            import_roots.append(prepared.root)
            return prepared

        with patch("study_package.prepare_study_set_archive", side_effect=capture_prepared):
            record = self.import_package(types=["quiz"])[0]
        self.assertEqual(len(import_roots), 1)
        self.assertFalse(import_roots[0].exists())
        directory = server._conversion_dir(record["id"])
        staged_map = directory / "package" / SOURCE_MAP_PATH
        self.assertEqual(json.loads(staged_map.read_text(encoding="utf-8")), SOURCE_MAP)
        with patch("notebooklm_workflow.convert_notebooklm_artifact", side_effect=InputDocumentError("Retry conversion")):
            response = self.client.post(f"/api/history/{record['id']}/continue")
        self.assertEqual(response.status_code, 400, response.text)
        with patch("server.execute_generation", side_effect=AssertionError("Provider called")):
            response = self.client.post(f"/api/history/{record['id']}/continue")
        self.assertEqual(response.status_code, 200, response.text)
        restored = server._notebooklm_task_from_record(server._read_record(record["id"]), directory)
        self.assertEqual(restored.source.source_id, SOURCE_UUID)
        self.assertIn(staged_map, restored.dependencies)
        self.assertTrue(staged_map.is_file())
        self.assertTrue((directory / server._read_record(record["id"])["output_path"]).parent.is_relative_to(directory / "package/output" / SOURCE_UUID))

    def test_notebooklm_worker_forwards_cancellation_and_retains_retryable_history(self) -> None:
        record = self.import_package(types=["quiz"])[0]

        def cancel_conversion(artifact, output_path, *, cancel_check):
            self.assertFalse(cancel_check())
            server.CANCEL_EVENTS[record["id"]].set()
            self.assertTrue(cancel_check())
            raise GenerationCancelled("Conversion cancelled.")

        with patch("notebooklm_workflow.convert_notebooklm_artifact", side_effect=cancel_conversion), \
                patch("server.execute_generation") as generate:
            response = self.client.post(f"/api/history/{record['id']}/continue")
        self.assertEqual(response.status_code, 410, response.text)
        generate.assert_not_called()
        self.assertEqual(server._read_record(record["id"])["status"], "failed")
        self.assertNotIn(record["id"], server.ACTIVE_CONVERSIONS)

    def test_single_group_and_bulk_archives_include_canonical_outputs_sidecars_and_svg_assets(self) -> None:
        records = self.import_package(types=["quiz", "slide", "infographic"])
        with patch("server.execute_generation", side_effect=AssertionError("Provider called")):
            for record in records:
                response = self.client.post(f"/api/history/{record['id']}/continue")
                self.assertEqual(response.status_code, 200, response.text)
        for record in records:
            with self.subTest(action=record["action"]):
                response = self.client.get(f"/api/history/{record['id']}/download")
                self.assertEqual(response.status_code, 200, response.text)
                with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                    prefix = SOURCE_UUID + "/" + record["action"].removeprefix("create_") + "/"
                    artifact_path = prefix + record["output_filename"]
                    artifact = json.loads(archive.read(artifact_path))
                    self.assertEqual(schema_errors(artifact, get_schema(record["action"])), [])
                    self.assertNotIn("media", artifact)
                    sidecar_path = prefix + Path(record["output_filename"]).with_suffix(".metadata.json").name
                    self.assertIn(sidecar_path, archive.namelist())
                    sidecar = json.loads(archive.read(sidecar_path))
                    for medium in sidecar.get("original_media", []):
                        self.assertIn(prefix + medium["path"], archive.namelist())
                    if record["action"] == "create_infographics":
                        for section in artifact["sections"]:
                            self.assertIn(prefix + section["value"], archive.namelist())
                            self.assertIn(b"data:image/png;base64,", archive.read(prefix + section["value"]))
                    self.assertIn(SOURCE_UUID + "/source/" + record["notebooklm_metadata_path"].replace("\\", "/").removeprefix("package/"), archive.namelist())
                    self.assertEqual(json.loads(archive.read(SOURCE_UUID + "/source/" + SOURCE_MAP_PATH)), SOURCE_MAP)
                    self.assertIn("conversion.json", archive.namelist())
                response = self.client.get(f"/api/history/groups/{record['source_group_id']}/download")
                self.assertEqual(response.status_code, 200, response.text)
                with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                    prefix = SOURCE_UUID + "/" + record["action"].removeprefix("create_") + "/"
                    artifact_path = prefix + record["output_filename"]
                    artifact = json.loads(archive.read(artifact_path))
                    sidecar_path = prefix + Path(record["output_filename"]).with_suffix(".metadata.json").name
                    self.assertIn(sidecar_path, archive.namelist())
                    for medium in json.loads(archive.read(sidecar_path)).get("original_media", []):
                        self.assertIn(prefix + medium["path"], archive.namelist())
                    if record["action"] == "create_infographics":
                        for section in artifact["sections"]:
                            self.assertIn(prefix + section["value"], archive.namelist())
                    self.assertIn(SOURCE_UUID + "/manifest.json", archive.namelist())
                    source_map_member = SOURCE_UUID + "/source/" + SOURCE_MAP_PATH
                    self.assertEqual(archive.namelist().count(source_map_member), 1)
                    self.assertEqual(json.loads(archive.read(source_map_member)), SOURCE_MAP)
                    histories = [name for name in archive.namelist() if name.startswith(SOURCE_UUID + "/metadata/")]
                    self.assertEqual(set(histories), {SOURCE_UUID + "/metadata/" + item["id"] + ".json" for item in records})
                    self.assertEqual({name.split("/", 1)[0] for name in archive.namelist()}, {SOURCE_UUID})
        response = self.client.post("/api/history/download", json={"group_ids": [record["source_group_id"] for record in records]})
        self.assertEqual(response.status_code, 200, response.text)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            artifacts = []
            for record in records:
                suffix = "/" + SOURCE_UUID + "/" + record["action"].removeprefix("create_") + "/" + record["output_filename"]
                matches = [name for name in archive.namelist() if name.endswith(suffix)]
                self.assertEqual(len(matches), 1)
                artifacts.extend(matches)
            self.assertEqual(len(artifacts), 3)
            for name in artifacts:
                prefix = name.rsplit("/", 1)[0] + "/"
                sidecar_path = prefix + Path(name).with_suffix(".metadata.json").name
                self.assertIn(sidecar_path, archive.namelist())
                for medium in json.loads(archive.read(sidecar_path)).get("original_media", []):
                    self.assertIn(prefix + medium["path"], archive.namelist())
                payload = json.loads(archive.read(name))
                for section in payload.get("sections", []):
                    self.assertIn(prefix + section["value"], archive.namelist())


class NotebookLMPackageTests(unittest.TestCase):
    def test_media_and_extensionless_apps_are_valid_package_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "input.zip"
            archive.write_bytes(notebooklm_package())
            prepared = study_package.prepare_study_set_archive(archive, root / "extracted")
            self.assertEqual(len(prepared.jobs), 2)
            self.assertTrue(all(job.input_type == "notebooklm" for job in prepared.jobs))

    def test_output_cannot_be_written_inside_original_slide_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "input.zip"
            archive.write_bytes(notebooklm_package(types=["slide"], output="input/Artifacts/Deck"))
            with self.assertRaises(ApplicationError):
                study_package.prepare_study_set_archive(archive, root / "extracted")


if __name__ == "__main__":
    unittest.main()
