from __future__ import annotations

import base64
import html
import io
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from errors import GenerationCancelled, InputDocumentError, InvalidArgumentsError
from pipeline.engine import PipelineResult
import server
import study_package


SOURCE_UUID = "11111111-1111-4111-8111-111111111111"
SOURCE_NAME = "chapter.txt"
SOURCE_MAP_PATH = "input/metadata/sources.metadata.json"
SOURCE_MAP = {"chapter.txt metadata.json": {"id": SOURCE_UUID}}
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+XW8sAAAAASUVORK5CYII=")


def hybrid_package(
    types: list[str] | None = None, *, formats: list[str] | None = None, source_map: bool = True,
    input_directory: str = "input",
) -> bytes:
    config = {"connector": "openai", "model": "hybrid-test-model", "context_window": 24000,
              "pipeline": {"strategy": "baseline", "retries": 0, "temperature": .25},
              "files": [{"type": "notebooklm", "input": input_directory, "output": "output",
                         "types": types or ["quiz", "podcast", "slide", "infographic", "report"],
                         **({"formats": formats} if formats is not None else {"formats": ["json"]})}]}
    association = [{"sourceId": {"id": SOURCE_UUID}, "filename": SOURCE_NAME}]
    metadata = {"status": "ARTIFACT_STATUS_READY", "sources": association}
    quiz = {"quiz": [{"question": "Which text is preserved?", "hint": "Read the source",
                      "answerOptions": [{"text": "The original", "isCorrect": True, "rationale": "Recorded answer"},
                                        {"text": "A different chapter", "isCorrect": False, "rationale": "Wrong source"},
                                        {"text": "An image", "isCorrect": False, "rationale": "Wrong format"},
                                        {"text": "An audio file", "isCorrect": False, "rationale": "Unused audio"}]}]}
    members = {
        "study-set-config.json": json.dumps(config),
        "input/Sources/chapter.txt metadata.json": json.dumps({"title": SOURCE_NAME, "sourceId": {"id": SOURCE_UUID}}),
        "input/Sources/chapter.txt.html": "<html><body><p>Original chapter facts used for generation.</p></body></html>",
        "input/Artifacts/Quiz metadata.json": json.dumps({**metadata, "title": "Quiz", "type": "ARTIFACT_TYPE_APP",
                                                        "app": {"generationOptions": {"appType": "APP_TYPE_QUIZ"}}}),
        "input/Artifacts/Quiz": '<app-root data-app-data="' + html.escape(json.dumps(quiz), quote=True) + '"></app-root>',
        "input/Artifacts/Diagram metadata.json": json.dumps({**metadata, "title": "Diagram", "type": "ARTIFACT_TYPE_INFOGRAPHIC"}),
        "input/Artifacts/Diagram.png": PNG,
        "input/Artifacts/Deck metadata.json": json.dumps({**metadata, "title": "Deck", "type": "ARTIFACT_TYPE_SLIDES"}),
        "input/Artifacts/Deck/slide_1": PNG,
        "input/Artifacts/Unused recording metadata.json": json.dumps({**metadata, "title": "Unused recording", "type": "ARTIFACT_TYPE_AUDIO_OVERVIEW"}),
        "input/Artifacts/Unused recording.wav": b"UNUSED ORIGINAL AUDIO",
    }
    if source_map:
        members[SOURCE_MAP_PATH] = json.dumps(SOURCE_MAP)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, value in members.items():
            archive.writestr(name, value)
    return stream.getvalue()


def write_result(task, output_path: Path, **kwargs) -> PipelineResult:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"title": "Result from " + task.source.source_name}) + "\n"
    output_path.write_text(text, encoding="utf-8")
    output_path.with_suffix(".metadata.json").write_text(json.dumps({"mode": task.mode}), encoding="utf-8")
    if task.mode == "ocr":
        asset = output_path.parent / "assets" / "preserved.svg"
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><rect width="1" height="1"/></svg>', encoding="utf-8")
    return PipelineResult(text, {"provider_calls": 0 if task.mode == "convert" else 1}, {"valid": True}, output_path.parent)


class NotebookLMWorkflowServerTests(unittest.TestCase):
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
        server.QUEUED_API_KEYS.clear()
        self.dispatcher_patch.stop()
        self.history_patch.stop()
        self.temporary.cleanup()

    def import_package(
        self, *, types=None, formats=None, api_key=None, default_formats=False, input_directory="input",
    ) -> tuple[dict, list[dict]]:
        content = hybrid_package(types, formats=formats, input_directory=input_directory)
        if default_formats:
            output = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(content)) as original, zipfile.ZipFile(output, "w") as archive:
                for info in original.infolist():
                    value = original.read(info)
                    if info.filename == "study-set-config.json":
                        config = json.loads(value)
                        config["files"][0].pop("formats", None)
                        value = json.dumps(config).encode("utf-8")
                    archive.writestr(info, value)
            content = output.getvalue()
        response = self.client.post("/api/study-sets/import", data={"api_key": api_key} if api_key else {},
                                    files={"file": ("hybrid.zip", content, "application/zip")})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        return payload, [server._read_record(identifier) for identifier in payload["session_ids"]]

    def test_missing_required_source_map_rejects_even_uuid_metadata_before_model_execution(self) -> None:
        with patch("server.execute_generation") as generate, \
                patch("notebooklm_workflow.create_connector") as connector, \
                patch("notebooklm_workflow.run_pipeline") as pipeline, \
                patch("notebooklm_workflow.execute_notebooklm_task") as execute:
            response = self.client.post("/api/study-sets/import", files={
                "file": ("missing-map.zip", hybrid_package(types=["podcast"], source_map=False), "application/zip"),
            })
        self.assertEqual(response.status_code, 400, response.text)
        detail = response.json()["detail"]
        self.assertIn("metadata/sources.metadata.json", detail.replace("\\", "/"))
        self.assertIn('{"{metadata.json}":{"id":"source_id"}}', detail.replace(" ", ""))
        generate.assert_not_called()
        connector.assert_not_called()
        pipeline.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(server._records_on_disk(), [])
        self.assertEqual(server.QUEUED_API_KEYS, {})

    def test_subfolder_input_stages_map_from_notebook_root_for_generation_and_conversion(self) -> None:
        cases = (("input/Sources", "report", "source"), ("input/Artifacts", "report", "source"),
                 ("input/Sources", "quiz", "convert"))
        for input_directory, artifact_type, mode in cases:
            with self.subTest(input_directory=input_directory, artifact_type=artifact_type):
                _, records = self.import_package(types=[artifact_type], input_directory=input_directory)
                record = records[0]
                directory = server._conversion_dir(record["id"])
                task = server._notebooklm_task_from_record(record, directory)
                staged_map = directory / "package" / SOURCE_MAP_PATH
                self.assertEqual((task.mode, task.source.source_id), (mode, SOURCE_UUID))
                self.assertIn(staged_map, task.source.dependencies)
                self.assertEqual(json.loads(staged_map.read_text(encoding="utf-8")), SOURCE_MAP)
                if mode == "convert":
                    self.assertEqual(task.artifact.source, directory / "package/input/Artifacts/Quiz")

    def test_import_stages_all_modes_per_source_with_provider_options_and_ephemeral_keys(self) -> None:
        secret = "ephemeral-test-secret"
        with patch("server.execute_generation", side_effect=AssertionError("Import must not generate")), \
                patch("notebooklm_workflow.execute_notebooklm_task", side_effect=AssertionError("Import must not execute")):
            payload, records = self.import_package(api_key=secret)
        self.assertEqual((payload["source_count"], payload["job_count"]), (1, 5))
        expected_modes = {"create_quizzes": "convert", "create_podcasts": "source", "create_reports": "source",
                          "create_slides": "ocr", "create_infographics": "ocr"}
        self.assertEqual({record["action"]: record["notebooklm_task"]["mode"] for record in records}, expected_modes)
        self.assertEqual(len({record["source_group_id"] for record in records}), 1)
        for record in records:
            directory = server._conversion_dir(record["id"])
            task = server._notebooklm_task_from_record(record, directory)
            self.assertEqual(task.source.source_id, SOURCE_UUID)
            self.assertEqual(task.source.source_name, SOURCE_NAME)
            self.assertEqual(record["input_filename"], SOURCE_NAME)
            self.assertTrue(task.source.path.is_file())
            self.assertTrue(task.source.path.is_relative_to(directory))
            self.assertTrue(all(path.exists() and path.is_relative_to(directory) for path in task.dependencies))
            staged_map = directory / "package" / SOURCE_MAP_PATH
            self.assertEqual(json.loads(staged_map.read_text(encoding="utf-8")), SOURCE_MAP)
            self.assertIn(staged_map, task.source.dependencies)
            self.assertIn(str(staged_map.relative_to(directory)), record["notebooklm_dependency_paths"])
            self.assertFalse(any(path.suffix.lower() == ".wav" for path in task.dependencies))
            self.assertNotIn(secret, (directory / "conversion.json").read_text(encoding="utf-8"))
            self.assertEqual(server._public_record(record)["notebooklm_mode"], task.mode)
            self.assertEqual(server._public_record(record)["notebooklm_source_id"], SOURCE_UUID)
            if task.mode == "convert":
                self.assertEqual(record["options"], {})
                self.assertNotIn(record["id"], server.QUEUED_API_KEYS)
            else:
                self.assertEqual(record["connector"], "openai")
                self.assertEqual(record["model"], "hybrid-test-model")
                self.assertEqual(record["options"]["strategy"], "baseline")
                self.assertEqual(record["options"]["profile_overrides"]["context_window"], 24000)
                self.assertEqual(server.QUEUED_API_KEYS[record["id"]], secret)
                self.assertTrue(record["api_key_was_supplied"])

    def test_retry_reconstructs_source_task_and_forwards_saved_options_and_new_key(self) -> None:
        import_roots = []
        prepare = study_package.prepare_study_set_archive

        def capture_prepared(*args, **kwargs):
            prepared = prepare(*args, **kwargs)
            import_roots.append(prepared.root)
            return prepared

        with patch("study_package.prepare_study_set_archive", side_effect=capture_prepared):
            _, records = self.import_package(types=["podcast"])
        record = records[0]
        self.assertEqual(len(import_roots), 1)
        self.assertFalse(import_roots[0].exists())
        with patch("notebooklm_workflow.execute_notebooklm_task", side_effect=InputDocumentError("Local test retry")):
            response = self.client.post(f"/api/history/{record['id']}/continue")
        self.assertEqual(response.status_code, 400, response.text)
        with patch("server.execute_generation", side_effect=AssertionError("Wrong dispatcher")), \
                patch("notebooklm_workflow.execute_notebooklm_task", side_effect=write_result) as execute:
            response = self.client.post(f"/api/history/{record['id']}/continue", data={"api_key": " replacement-key "})
        self.assertEqual(response.status_code, 200, response.text)
        task, output_path = execute.call_args.args
        self.assertEqual(task.mode, "source")
        self.assertEqual(task.source.source_id, SOURCE_UUID)
        staged_map = server._conversion_dir(record["id"]) / "package" / SOURCE_MAP_PATH
        self.assertEqual(json.loads(staged_map.read_text(encoding="utf-8")), SOURCE_MAP)
        self.assertIn(staged_map, task.source.dependencies)
        self.assertIn("Original chapter facts", task.source.path.read_text(encoding="utf-8"))
        self.assertTrue(output_path.is_relative_to(server._conversion_dir(record["id"])))
        kwargs = execute.call_args.kwargs
        self.assertEqual((kwargs["connector_name"], kwargs["model"], kwargs["api_key"]), ("openai", "hybrid-test-model", "replacement-key"))
        self.assertEqual(kwargs["options"].strategy, "baseline")
        self.assertEqual(kwargs["options"].temperature, .25)
        self.assertTrue(callable(kwargs["options"].cancel_check))
        completed = server._read_record(record["id"])
        self.assertEqual((completed["status"], completed["run_attempts"]), ("completed", 2))
        self.assertNotIn("replacement-key", json.dumps(completed))

    def test_queued_worker_consumes_import_key_without_persisting_or_copying_audio(self) -> None:
        _, records = self.import_package(types=["podcast"], api_key="import-only-key")
        record = records[0]
        with patch("notebooklm_workflow.execute_notebooklm_task", side_effect=write_result) as execute:
            server._run_queued(record["id"])
        self.assertEqual(execute.call_args.kwargs["api_key"], "import-only-key")
        self.assertNotIn(record["id"], server.QUEUED_API_KEYS)
        self.assertFalse(list(server._conversion_dir(record["id"]).rglob("*.wav")))
        self.assertEqual(server._read_record(record["id"])["status"], "completed")

    def test_default_podcast_companions_share_one_generation_across_durable_records(self) -> None:
        from tests.test_notebooklm import canonical_podcast

        _, records = self.import_package(types=["podcast"], default_formats=True)
        self.assertEqual({record["output_format"] for record in records}, {"json", "md"})
        self.assertEqual(len({record["notebooklm_generation_owner_id"] for record in records}), 1)

        def generate(**kwargs):
            return PipelineResult(json.dumps(canonical_podcast()), {"provider_calls": 1}, {"valid": True}, kwargs["options"].work_dir)

        with patch("notebooklm_workflow.create_connector", return_value=SimpleNamespace(model="hybrid-test-model")), \
                patch("notebooklm_workflow.run_pipeline", side_effect=generate) as pipeline:
            for record in records:
                response = self.client.post(f"/api/history/{record['id']}/continue")
                self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(pipeline.call_count, 1)
        completed = [server._read_record(record["id"]) for record in records]
        self.assertEqual(sum(record["metrics"]["provider_calls"] for record in completed), 1)
        self.assertEqual(sum(bool(record["metrics"].get("canonical_reused")) for record in completed), 1)

    def test_companion_retry_uses_own_cache_after_generation_owner_is_deleted(self) -> None:
        _, records = self.import_package(types=["podcast"], formats=["json", "md"])
        owner_id = records[0]["notebooklm_generation_owner_id"]
        companion = next(record for record in records if record["id"] != owner_id)
        server.discard_conversion(owner_id)
        with patch("notebooklm_workflow.execute_notebooklm_task", side_effect=write_result) as execute:
            response = self.client.post(f"/api/history/{companion['id']}/continue")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(execute.call_args.kwargs["options"].work_dir.is_relative_to(server._conversion_dir(companion["id"])))

    def test_deleting_generation_owner_waits_for_all_active_companions_to_finish(self) -> None:
        _, records = self.import_package(types=["report"], default_formats=True)
        self.assertEqual(len(records), 3)
        owner_id = records[0]["notebooklm_generation_owner_id"]
        companions = [record for record in records if record["id"] != owner_id]
        owner_dir = server._conversion_dir(owner_id)
        with patch("notebooklm_workflow.execute_notebooklm_task", side_effect=write_result):
            response = self.client.post(f"/api/history/{owner_id}/continue")
        self.assertEqual(response.status_code, 200, response.text)

        started = {record["id"]: threading.Event() for record in companions}
        release = {record["id"]: threading.Event() for record in companions}
        output_ids = {server._conversion_dir(record["id"]) / record["output_path"]: record["id"] for record in companions}
        responses = {}

        def generate_companion(task, output_path, **kwargs):
            conversion_id = output_ids[output_path]
            work_dir = kwargs["options"].work_dir
            self.assertTrue(work_dir.is_relative_to(owner_dir))
            started[conversion_id].set()
            if not release[conversion_id].wait(timeout=5):
                raise AssertionError("Test did not release active companion")
            work_dir.mkdir(parents=True, exist_ok=True)
            (work_dir / (conversion_id + ".json")).write_text("shared generation cache remained writable", encoding="utf-8")
            return write_result(task, output_path, **kwargs)

        def continue_companion(conversion_id):
            responses[conversion_id] = self.client.post(f"/api/history/{conversion_id}/continue")

        workers = [threading.Thread(target=continue_companion, args=(record["id"],), daemon=True) for record in companions]
        with patch("notebooklm_workflow.execute_notebooklm_task", side_effect=generate_companion):
            try:
                for worker in workers:
                    worker.start()
                for event in started.values():
                    self.assertTrue(event.wait(timeout=5))
                response = self.client.delete(f"/api/history/{owner_id}")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), {"discarded": False, "discarding": True})
                self.assertEqual(server._read_record(owner_id)["status"], "discarding")
                self.assertTrue(owner_dir.is_dir())
                history = self.client.get("/api/history").json()["conversions"]
                self.assertTrue(any(record["id"] == owner_id and record["status"] == "discarding" for record in history))

                # A run starting after deletion was requested uses its own work
                # folder; already active companions retain their current cache.
                remaining = companions[1]
                values = server._options_from_record(server._read_record(remaining["id"]), server._conversion_dir(remaining["id"]))
                self.assertTrue(values.work_dir.is_relative_to(server._conversion_dir(remaining["id"])))

                first = companions[0]["id"]
                release[first].set()
                workers[0].join(timeout=5)
                self.assertFalse(workers[0].is_alive())
                self.assertEqual(responses[first].status_code, 200, responses[first].text)
                self.assertTrue(owner_dir.is_dir())
                self.assertEqual(server._read_record(owner_id)["status"], "discarding")
                last = companions[1]["id"]
                release[last].set()
                workers[1].join(timeout=5)
                self.assertFalse(workers[1].is_alive())
                self.assertEqual(responses[last].status_code, 200, responses[last].text)
                self.assertFalse(owner_dir.exists())
                for record in companions:
                    completed = server._read_record(record["id"])
                    self.assertEqual(completed["status"], "completed")
                    self.assertTrue(server._output_available(completed))
            finally:
                for event in release.values():
                    event.set()
                for worker in workers:
                    worker.join(timeout=5)

    def test_legacy_conversion_record_remains_retryable_without_hybrid_task(self) -> None:
        _, records = self.import_package(types=["quiz"])
        record = records[0]
        record.pop("notebooklm_task")
        server._write_record(record)
        with patch("notebooklm_workflow.execute_notebooklm_task", side_effect=AssertionError("Legacy record should use converter")):
            response = self.client.post(f"/api/history/{record['id']}/continue")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(server._read_record(record["id"])["status"], "completed")
        response = self.client.get(f"/api/history/{record['id']}/download")
        self.assertEqual(response.status_code, 200, response.text)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertIn("source/input/Artifacts/Quiz", archive.namelist())
            self.assertIn(f"output/{record['output_filename']}", archive.namelist())
            self.assertIn("conversion.json", archive.namelist())
        response = self.client.get(f"/api/history/groups/{record['source_group_id']}/download")
        self.assertEqual(response.status_code, 200, response.text)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertIn("manifest.json", archive.namelist())
            self.assertIn(f"artifacts/{record['action']}-{record['id']}/{record['output_filename']}", archive.namelist())

    def test_model_task_cancellation_is_forwarded_and_history_remains_retryable(self) -> None:
        _, records = self.import_package(types=["infographic"])
        record = records[0]

        def cancel_task(task, output_path, **kwargs):
            self.assertEqual(task.mode, "ocr")
            server.CANCEL_EVENTS[record["id"]].set()
            self.assertTrue(kwargs["cancel_check"]())
            self.assertTrue(kwargs["options"].cancel_check())
            raise GenerationCancelled("Local extraction cancelled.")

        with patch("notebooklm_workflow.execute_notebooklm_task", side_effect=cancel_task):
            response = self.client.post(f"/api/history/{record['id']}/continue")
        self.assertEqual(response.status_code, 410, response.text)
        self.assertEqual(server._read_record(record["id"])["status"], "failed")
        self.assertNotIn(record["id"], server.ACTIVE_CONVERSIONS)

    def test_source_group_archive_includes_all_original_bundles_metadata_and_generated_assets(self) -> None:
        _, records = self.import_package(types=["quiz", "podcast", "infographic"])
        with patch("notebooklm_workflow.execute_notebooklm_task", side_effect=write_result):
            for record in records:
                response = self.client.post(f"/api/history/{record['id']}/continue")
                self.assertEqual(response.status_code, 200, response.text)
        group_id = records[0]["source_group_id"]
        response = self.client.get(f"/api/history/groups/{group_id}/download")
        self.assertEqual(response.status_code, 200, response.text)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), len(set(names)))
            self.assertIn(f"{SOURCE_UUID}/source/input/Sources/chapter.txt.html", names)
            self.assertIn(f"{SOURCE_UUID}/source/input/Artifacts/Quiz", names)
            self.assertIn(f"{SOURCE_UUID}/source/input/Artifacts/Diagram.png", names)
            source_map_member = f"{SOURCE_UUID}/source/{SOURCE_MAP_PATH}"
            self.assertEqual(names.count(source_map_member), 1)
            self.assertEqual(json.loads(archive.read(source_map_member)), SOURCE_MAP)
            self.assertIn(f"{SOURCE_UUID}/manifest.json", names)
            manifest = json.loads(archive.read(f"{SOURCE_UUID}/manifest.json"))
            self.assertEqual(manifest["notebooklm_source_id"], SOURCE_UUID)
            for record in records:
                category = record["action"].removeprefix("create_")
                self.assertIn(f"{SOURCE_UUID}/{category}/{record['output_filename']}", names)
                self.assertIn(f"{SOURCE_UUID}/metadata/{record['id']}.json", names)
            self.assertFalse(any(name.startswith("artifacts/") for name in names))
            self.assertFalse(any(name.endswith(".wav") for name in names))
            output_sidecars = [name for name in names if name.endswith(".metadata.json")
                               and not name.startswith(f"{SOURCE_UUID}/source/")]
            self.assertEqual(len(output_sidecars), 3)
            self.assertIn(f"{SOURCE_UUID}/infographics/assets/preserved.svg", names)
        source_record = next(record for record in records if record["action"] == "create_podcasts")
        response = self.client.get(f"/api/history/{source_record['id']}/download")
        self.assertEqual(response.status_code, 200, response.text)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertIn(f"{SOURCE_UUID}/source/input/Sources/chapter.txt.html", archive.namelist())
            self.assertEqual(json.loads(archive.read(f"{SOURCE_UUID}/source/{SOURCE_MAP_PATH}")), SOURCE_MAP)
            self.assertIn(f"{SOURCE_UUID}/podcasts/{source_record['output_filename']}", archive.namelist())
            self.assertIn("conversion.json", archive.namelist())
            self.assertFalse(any(name.endswith(".wav") for name in archive.namelist()))

    def test_companion_bundle_deduplicates_shared_metadata_and_assets_in_type_folder(self) -> None:
        _, records = self.import_package(types=["infographic"], formats=["json", "md", "html"])
        with patch("notebooklm_workflow.execute_notebooklm_task", side_effect=write_result):
            for record in records:
                response = self.client.post(f"/api/history/{record['id']}/continue")
                self.assertEqual(response.status_code, 200, response.text)
        response = self.client.get(f"/api/history/groups/{records[0]['source_group_id']}/download")
        self.assertEqual(response.status_code, 200, response.text)
        category_prefix = f"{SOURCE_UUID}/infographics/"
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), len(set(names)))
            for record in records:
                member = category_prefix + record["output_filename"]
                self.assertIn(member, names)
                self.assertEqual(json.loads(archive.read(member))["title"], "Result from " + SOURCE_NAME)
            metadata_name = Path(records[0]["output_filename"]).with_suffix(".metadata.json").name
            self.assertEqual([name for name in names if name.startswith(category_prefix) and name.endswith(".metadata.json")],
                             [category_prefix + metadata_name])
            self.assertEqual(names.count(category_prefix + "assets/preserved.svg"), 1)
            self.assertEqual(len([name for name in names if name.startswith(f"{SOURCE_UUID}/metadata/")]), 3)
            self.assertEqual(names.count(f"{SOURCE_UUID}/source/input/Artifacts/Diagram.png"), 1)

    def test_bulk_bundle_namespaces_repeated_source_uuids_per_import(self) -> None:
        batches = [self.import_package(types=["quiz"])[1] for _ in range(2)]
        group_ids = []
        with patch("notebooklm_workflow.execute_notebooklm_task", side_effect=write_result):
            for index, records in enumerate(batches, 1):
                record = records[0]
                group_ids.append(record["source_group_id"])
                response = self.client.post(f"/api/history/{record['id']}/continue")
                self.assertEqual(response.status_code, 200, response.text)
                output = server._conversion_dir(record["id"]) / record["output_path"]
                output.write_text(json.dumps({"batch": index}), encoding="utf-8")
        response = self.client.post("/api/history/download", json={"group_ids": group_ids})
        self.assertEqual(response.status_code, 200, response.text)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), len(set(names)))
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["study_sets"], 2)
            wrappers = [group["folder"] for group in manifest["groups"]]
            self.assertEqual(len(set(wrappers)), 2)
            for index, (records, wrapper) in enumerate(zip(batches, wrappers), 1):
                record = records[0]
                root = f"{wrapper}/{SOURCE_UUID}"
                self.assertEqual(json.loads(archive.read(f"{root}/quizzes/{record['output_filename']}")), {"batch": index})
                self.assertIn(f"{root}/source/input/Sources/chapter.txt.html", names)
                self.assertEqual(json.loads(archive.read(f"{root}/source/{SOURCE_MAP_PATH}")), SOURCE_MAP)
                self.assertIn(f"{root}/manifest.json", names)
                self.assertIn(f"{root}/metadata/{record['id']}.json", names)


class NotebookLMWorkflowPackageTests(unittest.TestCase):
    def test_task_source_dependencies_are_protected_from_output_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "input.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("study-set-config.json", "{}")
                archive.writestr("input/source.html", "Original document")
                archive.writestr("input/protected.json", "Original supporting dependency")

            def plan(package, **kwargs):
                source = package / "input/source.html"
                protected = package / "input/protected.json"
                task = SimpleNamespace(source=SimpleNamespace(path=source), dependencies=(source, protected))
                return [SimpleNamespace(source=source, output=protected, input_type="notebooklm",
                                        notebooklm_artifact=None, notebooklm_task=task,
                                        args=SimpleNamespace(work_dir=package / "work"))]

            with patch("study_package.plan_study_sets", side_effect=plan):
                with self.assertRaisesRegex(InvalidArgumentsError, "overwrite"):
                    study_package.prepare_study_set_archive(archive_path, root / "extracted")


if __name__ == "__main__":
    unittest.main()
