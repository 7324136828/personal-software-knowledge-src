from __future__ import annotations

import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from connectors.base import GenerationResponse
from errors import ApplicationError, GenerationCancelled
from pipeline.engine import PipelineResult
import server


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.history_temp = tempfile.TemporaryDirectory()
        self.history_patch = patch.object(server, "HISTORY_DIR", Path(self.history_temp.name))
        self.history_patch.start()
        server.ACTIVE_CONVERSIONS.clear()
        server.DISPATCHED_CONVERSIONS.clear()
        server.QUEUED_API_KEYS.clear()
        self.client = TestClient(server.app)

    def tearDown(self) -> None:
        self.history_patch.stop()
        self.history_temp.cleanup()

    def test_health_endpoint(self) -> None:
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("timestamp", data)

    def test_config_endpoint(self) -> None:
        response = self.client.get("/api/config")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("actions", data)
        self.assertIn("create_flashcards", data["actions"])
        self.assertIn("create_datatables", data["actions"])
        self.assertIn("connectors", data)
        self.assertIn("openai", data["connectors"])
        self.assertIn("system_temp_dir", data)

    def test_convert_rejects_missing_input(self) -> None:
        response = self.client.post(
            "/api/convert",
            data={"action": "create_flashcards", "connector": "openai"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("No file or pasted text provided", response.json()["detail"])

    def test_convert_rejects_invalid_action(self) -> None:
        response = self.client.post(
            "/api/convert",
            data={
                "action": "non_existent_action",
                "connector": "openai",
                "pasted_text": "Sample text",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported action", response.json()["detail"])

    def test_convert_rejects_invalid_connector(self) -> None:
        response = self.client.post(
            "/api/convert",
            data={
                "action": "create_flashcards",
                "connector": "invalid_provider",
                "pasted_text": "Sample text",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported connector", response.json()["detail"])

    def test_convert_and_download_flow(self) -> None:
        fake_output_content = '{"flashcards": [{"front": "What is ML?", "back": "Machine Learning"}]}'

        def fake_execute_generation(*, connector_name, input_path, action, output_path, model=None, api_key=None, options=None):
            self.assertEqual(api_key, "test-secret-key")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(fake_output_content, encoding="utf-8")
            return PipelineResult(
                text=fake_output_content,
                metrics={"runtime_seconds": 0.42},
                validation={},
                work_dir=output_path.parent,
            )

        with patch("server.execute_generation", side_effect=fake_execute_generation):
            # Test with pasted text
            response = self.client.post(
                "/api/convert",
                data={
                    "action": "create_flashcards",
                    "connector": "openai",
                    "pasted_text": "Machine Learning is the study of algorithms...",
                    "api_key": "test-secret-key",
                },
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["success"])
            self.assertIn("temp_folder", data)
            # Verify the conversion is stored in persistent history.
            self.assertTrue(str(server.HISTORY_DIR).lower() in data["temp_folder"].lower())
            self.assertEqual(data["output_text"], fake_output_content)
            self.assertIn("download_url", data)

            history = self.client.get("/api/history").json()["conversions"]
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["status"], "completed")
            self.assertEqual(history[0]["input_filename"], "pasted_document.txt")

            archive_response = self.client.get(history[0]["download_url"])
            self.assertEqual(archive_response.status_code, 200)
            self.assertEqual(archive_response.headers.get("content-type"), "application/zip")
            with zipfile.ZipFile(io.BytesIO(archive_response.content)) as archive:
                self.assertIn("source/pasted_document.txt", archive.namelist())
                self.assertIn(f"output/{data['output_filename']}", archive.namelist())
                self.assertIn("conversion.json", archive.namelist())
                self.assertNotIn("test-secret-key", archive.read("conversion.json").decode("utf-8"))

            download_url = data["download_url"]
            # Test download endpoint
            dl_resp = self.client.get(download_url)
            self.assertEqual(dl_resp.status_code, 200)
            self.assertEqual(dl_resp.text, fake_output_content)
            self.assertEqual(dl_resp.headers.get("content-type"), "application/json")

    def test_convert_pdf_upload_flow(self) -> None:
        from pypdf import PdfWriter

        # Create in-memory blank PDF
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        pdf_buffer = io.BytesIO()
        writer.write(pdf_buffer)
        pdf_bytes = pdf_buffer.getvalue()

        fake_output_content = "# Executive Report\nSummary of findings."

        def fake_execute_generation(*, connector_name, input_path, action, output_path, model=None, api_key=None, options=None):
            self.assertTrue(input_path.name.endswith(".pdf"))
            self.assertTrue(input_path.exists())
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(fake_output_content, encoding="utf-8")
            return PipelineResult(
                text=fake_output_content,
                metrics={"runtime_seconds": 0.55},
                validation={},
                work_dir=output_path.parent,
            )

        with patch("server.execute_generation", side_effect=fake_execute_generation):
            response = self.client.post(
                "/api/convert",
                data={
                    "action": "create_reports",
                    "connector": "openai",
                },
                files={
                    "file": ("test_document.pdf", pdf_bytes, "application/pdf"),
                },
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["success"])
            self.assertEqual(data["input_filename"], "test_document.pdf")
            self.assertIn("temp_folder", data)
            self.assertEqual(data["output_text"], fake_output_content)

            # Test download of markdown report
            dl_resp = self.client.get(data["download_url"])
            self.assertEqual(dl_resp.status_code, 200)
            self.assertEqual(dl_resp.text.replace("\r\n", "\n"), fake_output_content)

    def test_failed_conversion_can_be_continued_and_discarded(self) -> None:
        fake_output = '{"flashcards": []}'

        failure = ApplicationError("Provider unavailable")
        failure.pipeline_metrics = {"retry_count": 2, "repair_count": 3}
        with patch("server.execute_generation", side_effect=failure):
            response = self.client.post(
                "/api/convert",
                data={
                    "action": "create_flashcards",
                    "connector": "openai",
                    "pasted_text": "Resume this source",
                },
            )
        self.assertEqual(response.status_code, 400)
        history = self.client.get("/api/history").json()["conversions"]
        self.assertEqual(history[0]["status"], "failed")
        self.assertTrue(history[0]["can_continue"])
        self.assertEqual(history[0]["retry_count"], 2)
        self.assertEqual(history[0]["repair_count"], 3)
        self.assertIn("2 provider retries and 3 validation retries", history[0]["log"][-1]["message"])
        conversion_id = history[0]["id"]

        def resumed_generation(*, output_path, options, **kwargs):
            self.assertEqual(options.chunk_characters, 2048)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(fake_output, encoding="utf-8")
            return PipelineResult(
                text=fake_output,
                metrics={"runtime_seconds": 0.1},
                validation={},
                work_dir=output_path.parent,
            )

        with patch("server.execute_generation", side_effect=resumed_generation):
            resumed = self.client.post(
                f"/api/history/{conversion_id}/continue",
                data={"chunk_characters": "2048"},
            )
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.json()["output_text"], fake_output)

        discarded = self.client.delete(f"/api/history/{conversion_id}")
        self.assertEqual(discarded.status_code, 200)
        self.assertTrue(discarded.json()["discarded"])
        self.assertEqual(self.client.get("/api/history").json()["conversions"], [])

    def test_enqueue_groups_artifacts_for_one_file(self) -> None:
        group_id = "a" * 32
        with patch("server._ensure_queue_dispatcher"):
            for action in ("create_reports", "create_qandas"):
                response = self.client.post(
                    "/api/convert",
                    data={
                        "action": action,
                        "connector": "ollama",
                        "pasted_text": "Queued source",
                        "input_filename": "same-file.txt",
                        "enqueue": "true",
                        "source_group_id": group_id,
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["queued"])

            with patch("server._ensure_queue_dispatcher"):
                history = self.client.get("/api/history").json()["conversions"]
        self.assertEqual(len(history), 2)
        self.assertEqual({item["source_group_id"] for item in history}, {group_id})
        self.assertEqual({item["status"] for item in history}, {"queued"})

    def test_partial_study_set_archive_snapshots_only_finished_artifacts(self) -> None:
        group_id = "b" * 32
        records = []
        with patch("server._ensure_queue_dispatcher"):
            # Duplicate artifact types exercise ZIP filename collision handling.
            for action, status in (("create_reports", "completed"), ("create_reports", "completed"),
                                   ("create_qandas", "in_progress"), ("create_quizzes", "failed"),
                                   ("create_flashcards", "queued")):
                response = self.client.post("/api/convert", data={
                    "action": action, "connector": "the_connector", "pasted_text": "Study source",
                    "input_filename": "chapter.txt", "enqueue": "true", "source_group_id": group_id,
                })
                self.assertEqual(response.status_code, 200)
                record = server._read_record(response.json()["session_id"])
                record["status"] = status
                directory = server._conversion_dir(record["id"])
                # Even unfinished jobs with output files must be excluded.
                (directory / record["output_path"]).write_text(f"{status}-{record['id']}", encoding="utf-8")
                server._write_record(record)
                records.append(record)

        # An unrelated finished study set must never enter this download.
        unrelated = dict(records[0], id="c" * 32, source_group_id="d" * 32)
        server._write_record(unrelated)
        server.ACTIVE_CONVERSIONS.add(records[2]["id"])
        before = {record["id"]: server._metadata_path(record["id"]).read_bytes() for record in records}
        archive_paths = []
        real_temporary_file = server.tempfile.NamedTemporaryFile

        def tracked_temporary_file(**kwargs):
            temporary = real_temporary_file(**kwargs)
            archive_paths.append(Path(temporary.name))
            return temporary

        with patch("server.tempfile.NamedTemporaryFile", side_effect=tracked_temporary_file):
            response = self.client.get(f"/api/history/groups/{group_id}/download")
        self.assertEqual(response.status_code, 200)
        self.assertIn("chapter-study-set-partial.zip", response.headers["content-disposition"])
        self.assertFalse(archive_paths[0].exists(), "Temporary ZIP should be removed after sending")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertTrue(manifest["partial"])
            self.assertEqual((manifest["completed_artifacts"], manifest["total_artifacts"]), (2, 5))
            self.assertEqual(archive.read("source/chapter.txt"), b"Study source")
            outputs = [name for name in archive.namelist() if name.startswith("artifacts/") and not name.endswith("conversion.json")]
            self.assertEqual(len(outputs), 2)
            for record in records[:2]:
                expected = f"artifacts/{record['action']}-{record['id']}/{record['output_filename']}"
                self.assertIn(expected, outputs)
                self.assertEqual(archive.read(expected).decode(), f"completed-{record['id']}")
            self.assertEqual({item["status"] for item in manifest["artifacts"] if not item["included"]}, {"queued", "failed", "in_progress"})
        self.assertIn(records[2]["id"], server.ACTIVE_CONVERSIONS)
        self.assertEqual(before, {record["id"]: server._metadata_path(record["id"]).read_bytes() for record in records})

        # A later download includes newly finished types, instead of a cached ZIP.
        for record in records[2:]:
            record["status"] = "completed"
            server._write_record(record)
        with patch("server.tempfile.NamedTemporaryFile", side_effect=tracked_temporary_file):
            refreshed = self.client.get(f"/api/history/groups/{group_id}/download")
        self.assertNotEqual(archive_paths[0], archive_paths[1])
        self.assertFalse(archive_paths[1].exists())
        with zipfile.ZipFile(io.BytesIO(refreshed.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertFalse(manifest["partial"])
            self.assertEqual(manifest["completed_artifacts"], 5)
        self.assertIn("chapter-study-set.zip", refreshed.headers["content-disposition"])

    def test_study_set_download_rejects_unknown_or_unfinished_groups(self) -> None:
        self.assertEqual(self.client.get(f"/api/history/groups/{'e' * 32}/download").status_code, 404)
        group_id = "f" * 32
        with patch("server._ensure_queue_dispatcher"):
            response = self.client.post("/api/convert", data={
                "action": "create_reports", "connector": "the_connector", "pasted_text": "Study source",
                "enqueue": "true", "source_group_id": group_id,
            })
        self.assertEqual(response.status_code, 200)
        response = self.client.get(f"/api/history/groups/{group_id}/download")
        self.assertEqual(response.status_code, 409)
        self.assertIn("No completed artifacts", response.json()["detail"])
        # A completed record whose output is missing is also unavailable.
        record = server._records_on_disk()[0]
        record["status"] = "completed"
        server._write_record(record)
        self.assertEqual(self.client.get(f"/api/history/groups/{group_id}/download").status_code, 409)

    def test_active_conversion_appears_in_history_and_can_be_discarded(self) -> None:
        started = threading.Event()
        responses = []

        def cancellable_generation(*, options, **kwargs):
            started.set()
            while not options.cancel_check():
                time.sleep(0.01)
            raise GenerationCancelled("discarded")

        def submit_conversion():
            with TestClient(server.app) as client:
                responses.append(
                    client.post(
                        "/api/convert",
                        data={
                            "action": "create_reports",
                            "connector": "ollama",
                            "pasted_text": "Long-running source",
                        },
                    )
                )

        with patch("server.execute_generation", side_effect=cancellable_generation):
            worker = threading.Thread(target=submit_conversion)
            worker.start()
            self.assertTrue(started.wait(timeout=2))
            history = self.client.get("/api/history").json()["conversions"]
            self.assertEqual(history[0]["status"], "in_progress")
            self.assertTrue(history[0]["active"])
            conversion_id = history[0]["id"]

            discarded = self.client.delete(f"/api/history/{conversion_id}")
            self.assertEqual(discarded.status_code, 200)
            self.assertTrue(discarded.json()["discarding"])
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(responses[0].status_code, 400)
        self.assertEqual(self.client.get("/api/history").json()["conversions"], [])


if __name__ == "__main__":
    unittest.main()
