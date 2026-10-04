from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from errors import ApplicationError, GenerationCancelled
from pipeline.engine import PipelineResult
import server


class HistoryManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.history_temp = tempfile.TemporaryDirectory()
        self.history_patch = patch.object(server, "HISTORY_DIR", Path(self.history_temp.name))
        self.history_patch.start()
        # Each test controls execution explicitly; no queue worker may consume its fixtures.
        self.dispatch_patch = patch("server._ensure_queue_dispatcher")
        self.dispatch_patch.start()
        server.ACTIVE_CONVERSIONS.clear()
        server.DISPATCHED_CONVERSIONS.clear()
        server.CANCEL_EVENTS.clear()
        server.QUEUED_API_KEYS.clear()
        self.client = TestClient(server.app)

    def tearDown(self) -> None:
        self.dispatch_patch.stop()
        self.history_patch.stop()
        self.history_temp.cleanup()
        server.ACTIVE_CONVERSIONS.clear()
        server.DISPATCHED_CONVERSIONS.clear()
        server.CANCEL_EVENTS.clear()
        server.QUEUED_API_KEYS.clear()

    def _queue(self, group_id: str, *, action: str = "create_reports", filename: str = "chapter.txt") -> dict:
        response = self.client.post("/api/convert", data={
            "action": action,
            "connector": "the_connector",
            "pasted_text": f"Source for {group_id}",
            "input_filename": filename,
            "enqueue": "true",
            "source_group_id": group_id,
            "api_key": "temporary-test-secret",
        })
        self.assertEqual(response.status_code, 200, response.text)
        return server._read_record(response.json()["session_id"])

    def _output(self, record: dict, *, status: str, partial: bool = False, content: str = "Generated artifact") -> dict:
        output_path = server._conversion_dir(record["id"]) / record["output_path"]
        output_path.write_text(content, encoding="utf-8")
        record.update(status=status, output_partial=partial)
        server._write_record(record)
        return record

    def test_delete_file_history_removes_completed_failed_and_queued_artifacts(self) -> None:
        group_id = "a" * 32
        records = [
            self._output(self._queue(group_id), status="completed"),
            self._output(self._queue(group_id, action="create_flashcards"), status="failed", partial=True),
            self._queue(group_id, action="create_quizzes"),
        ]
        unrelated = self._queue("b" * 32)

        response = self.client.delete(f"/api/history/groups/{group_id}?include_completed=true")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["affected"], 3)
        for record in records:
            self.assertFalse(server._conversion_dir(record["id"]).exists())
            self.assertNotIn(record["id"], server.QUEUED_API_KEYS)
        self.assertTrue(server._conversion_dir(unrelated["id"]).exists())
        self.assertIn(unrelated["id"], server.QUEUED_API_KEYS)
        history = self.client.get("/api/history").json()["conversions"]
        self.assertEqual([item["id"] for item in history], [unrelated["id"]])

    def test_cancel_file_group_preserves_completed_history(self) -> None:
        group_id = "c" * 32
        completed = self._output(self._queue(group_id), status="completed")
        queued = self._queue(group_id, action="create_quizzes")
        failed = self._output(self._queue(group_id, action="create_flashcards"), status="failed", partial=True)

        response = self.client.delete(f"/api/history/groups/{group_id}")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["affected"], 2)
        self.assertTrue(server._conversion_dir(completed["id"]).exists())
        self.assertEqual(self.client.get(f"/api/history/{completed['id']}/download").status_code, 200)
        self.assertFalse(server._conversion_dir(queued["id"]).exists())
        self.assertFalse(server._conversion_dir(failed["id"]).exists())

    def test_delete_active_file_group_cancels_before_removing_its_files(self) -> None:
        group_id = "d" * 32
        record = self._queue(group_id)
        completed = self._output(self._queue(group_id, action="create_flashcards"), status="completed")
        started = threading.Event()
        cancellation_seen = threading.Event()
        release = threading.Event()
        responses = []

        def cancellable_generation(*, options, **kwargs):
            started.set()
            if not server.CANCEL_EVENTS[record["id"]].wait(timeout=3):
                raise RuntimeError("Test conversion was not cancelled")
            self.assertTrue(options.cancel_check())
            cancellation_seen.set()
            release.wait(timeout=3)
            raise GenerationCancelled("Deleted by user")

        def continue_conversion():
            with TestClient(server.app) as client:
                responses.append(client.post(f"/api/history/{record['id']}/continue"))

        with patch("server.execute_generation", side_effect=cancellable_generation):
            worker = threading.Thread(target=continue_conversion)
            worker.start()
            try:
                self.assertTrue(started.wait(timeout=3))
                response = self.client.delete(f"/api/history/groups/{group_id}?include_completed=true")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["affected"], 2)
                self.assertTrue(cancellation_seen.wait(timeout=3))
                self.assertEqual(server._read_record(record["id"])["status"], "discarding")
                self.assertTrue(server._conversion_dir(record["id"]).exists())
                self.assertFalse(server._conversion_dir(completed["id"]).exists())
            finally:
                event = server.CANCEL_EVENTS.get(record["id"])
                if event is not None:
                    event.set()
                release.set()
                worker.join(timeout=3)

        self.assertFalse(worker.is_alive())
        self.assertEqual(responses[0].status_code, 410)
        self.assertFalse(server._conversion_dir(record["id"]).exists())
        self.assertNotIn(record["id"], server.ACTIVE_CONVERSIONS)
        self.assertNotIn(record["id"], server.CANCEL_EVENTS)
        self.assertEqual(self.client.get("/api/history").json()["conversions"], [])

    def test_bulk_download_deduplicates_and_separates_study_sets_with_same_filename(self) -> None:
        first_group, second_group, unrelated_group = "e" * 32, "f" * 32, "1" * 32
        completed = self._output(self._queue(first_group), status="completed", content="Finished report")
        partial = self._output(self._queue(first_group, action="create_flashcards"),
                               status="failed", partial=True, content='{"flashcards": [{"front": "Retained"}]}')
        unfinished = self._output(self._queue(first_group, action="create_quizzes"),
                                  status="in_progress", content="Unmarked unfinished output")
        second = self._output(self._queue(second_group), status="completed", content="Second study set")
        unrelated = self._output(self._queue(unrelated_group), status="completed", content="Unrelated study set")
        records = [completed, partial, unfinished, second, unrelated]
        before = {record["id"]: server._metadata_path(record["id"]).read_bytes() for record in records}
        temporary_archives = []
        real_temporary = server.tempfile.NamedTemporaryFile

        def tracked_temporary(**kwargs):
            temporary = real_temporary(**kwargs)
            temporary_archives.append(Path(temporary.name))
            return temporary

        with patch("server.tempfile.NamedTemporaryFile", side_effect=tracked_temporary):
            response = self.client.post("/api/history/download", json={
                "group_ids": [first_group, second_group, first_group],
            })

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertTrue(temporary_archives)
        self.assertTrue(all(not path.exists() for path in temporary_archives))
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), len(set(names)), "ZIP members must be unique")
            root_manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(len(root_manifest["groups"]), 2)
            self.assertEqual({group["source_group_id"] for group in root_manifest["groups"]}, {first_group, second_group})
            manifests = [name for name in names if name.startswith("study-sets/") and name.endswith("/manifest.json")]
            self.assertEqual(set(manifests), {"study-sets/chapter-1/manifest.json", "study-sets/chapter-2/manifest.json"})
            prefixes = {}
            for manifest_path in manifests:
                manifest = json.loads(archive.read(manifest_path))
                prefix = manifest_path.removesuffix("/manifest.json")
                prefixes[manifest["source_group_id"]] = prefix
                self.assertEqual(archive.read(f"{prefix}/source/chapter.txt").decode(), f"Source for {manifest['source_group_id']}")
                if manifest["source_group_id"] == first_group:
                    self.assertTrue(manifest["partial"])
                    self.assertEqual(manifest["completed_artifacts"], 1)
                    self.assertEqual(manifest["included_artifacts"], 2)
                    self.assertEqual(manifest["partial_artifacts"], 1)
                    self.assertEqual(manifest["total_artifacts"], 3)
                    self.assertFalse(next(item for item in manifest["artifacts"] if item["id"] == unfinished["id"])["included"])
            for record in [completed, partial, second]:
                output_member = (f"{prefixes[record['source_group_id']]}/artifacts/"
                                 f"{record['action']}-{record['id']}/{record['output_filename']}")
                self.assertEqual(archive.read(output_member),
                                 (server._conversion_dir(record["id"]) / record["output_path"]).read_bytes())
            self.assertFalse(any(unfinished["id"] in name or unrelated["id"] in name for name in names))
            self.assertFalse(any(b"temporary-test-secret" in archive.read(name) for name in names))
        self.assertEqual(before, {record["id"]: server._metadata_path(record["id"]).read_bytes() for record in records})

    def test_bulk_download_rejects_invalid_selection_and_unavailable_groups(self) -> None:
        available_group, unfinished_group = "2" * 32, "3" * 32
        self._output(self._queue(available_group), status="completed")
        unfinished = self._output(self._queue(unfinished_group), status="failed", content="Unmarked failure")
        for payload in ({}, {"group_ids": []}, {"group_ids": "invalid"},
                        {"group_ids": [42]}, {"group_ids": [available_group, {}]}):
            with self.subTest(payload=payload):
                response = self.client.post("/api/history/download", json=payload)
                self.assertEqual(response.status_code, 400, response.text)
        response = self.client.post("/api/history/download", json={"group_ids": [available_group, "4" * 32]})
        self.assertEqual(response.status_code, 404, response.text)
        response = self.client.post("/api/history/download", json={"group_ids": [available_group, unfinished_group]})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse(server._public_record(unfinished)["output_available"])

        # Marking an empty output partial must not make an empty artifact downloadable.
        self._output(unfinished, status="failed", partial=True, content="")
        response = self.client.post("/api/history/download", json={"group_ids": [unfinished_group]})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse(server._public_record(unfinished)["output_available"])

    def test_bulk_download_removes_temporary_zip_when_root_manifest_serialization_fails(self) -> None:
        group_id = "6" * 32
        record = self._output(self._queue(group_id), status="completed")
        before = server._metadata_path(record["id"]).read_bytes()
        temporary_archives = []
        real_temporary = server.tempfile.NamedTemporaryFile
        real_dumps = server.json.dumps

        def tracked_temporary(**kwargs):
            temporary = real_temporary(**kwargs)
            temporary_archives.append(Path(temporary.name))
            return temporary

        def fail_root_manifest(value, **kwargs):
            if isinstance(value, dict) and "study_sets" in value:
                raise TypeError("Simulated root manifest serialization failure")
            return real_dumps(value, **kwargs)

        with patch("server.tempfile.NamedTemporaryFile", side_effect=tracked_temporary), \
                patch("server.json.dumps", side_effect=fail_root_manifest):
            with TestClient(server.app, raise_server_exceptions=False) as client:
                response = client.post("/api/history/download", json={"group_ids": [group_id]})

        self.assertEqual(response.status_code, 500)
        self.assertTrue(temporary_archives, "Failure must happen after creating the temporary ZIP")
        self.assertTrue(all(not path.exists() for path in temporary_archives))
        self.assertEqual(server._metadata_path(record["id"]).read_bytes(), before)
        # Serialization failure must not leave the history lock held or prevent a later download.
        self.assertEqual(self.client.post("/api/history/download", json={"group_ids": [group_id]}).status_code, 200)

    def test_bulk_download_snapshot_survives_concurrent_history_deletion(self) -> None:
        group_id = "7" * 32
        record = self._output(self._queue(group_id), status="completed", content="Keep this snapshot")
        snapshot_started = threading.Event()
        release_snapshot = threading.Event()
        deletion_started = threading.Event()
        deletion_done = threading.Event()
        downloads, deletions = [], []
        real_write_snapshot = server._write_study_set_snapshot

        def paused_snapshot(*args, **kwargs):
            snapshot_started.set()
            if not release_snapshot.wait(timeout=3):
                raise RuntimeError("Test did not release the snapshot")
            return real_write_snapshot(*args, **kwargs)

        def download():
            with TestClient(server.app) as client:
                downloads.append(client.post("/api/history/download", json={"group_ids": [group_id]}))

        def delete():
            with TestClient(server.app) as client:
                deletion_started.set()
                deletions.append(client.delete(f"/api/history/groups/{group_id}?include_completed=true"))
                deletion_done.set()

        with patch("server._write_study_set_snapshot", side_effect=paused_snapshot):
            download_worker = threading.Thread(target=download)
            delete_worker = threading.Thread(target=delete)
            download_worker.start()
            try:
                self.assertTrue(snapshot_started.wait(timeout=3))
                delete_worker.start()
                self.assertTrue(deletion_started.wait(timeout=3))
                self.assertFalse(deletion_done.wait(timeout=0.05))
                self.assertTrue(server._conversion_dir(record["id"]).exists())
            finally:
                release_snapshot.set()
                download_worker.join(timeout=3)
                if delete_worker.ident is not None:
                    delete_worker.join(timeout=3)

        self.assertFalse(download_worker.is_alive())
        self.assertFalse(delete_worker.is_alive())
        self.assertEqual(downloads[0].status_code, 200)
        self.assertEqual(deletions[0].status_code, 200)
        self.assertFalse(server._conversion_dir(record["id"]).exists())
        with zipfile.ZipFile(io.BytesIO(downloads[0].content)) as archive:
            member = f"study-sets/chapter-1/artifacts/{record['action']}-{record['id']}/{record['output_filename']}"
            self.assertEqual(archive.read(member), b"Keep this snapshot")

    def test_direct_download_snapshot_survives_history_deletion_before_streaming(self) -> None:
        group_id = "9" * 32
        record = self._output(self._queue(group_id), status="failed", partial=True, content="Retained for download")
        snapshot_paths = []
        real_file_response = server.FileResponse

        def delete_before_response(**kwargs):
            snapshot_path = Path(kwargs["path"])
            snapshot_paths.append(snapshot_path)
            self.assertFalse(snapshot_path.is_relative_to(server._conversion_dir(record["id"])))
            deleted = server.cancel_file_group(group_id, include_completed=True)
            self.assertEqual(deleted["affected"], 1)
            self.assertFalse(server._conversion_dir(record["id"]).exists())
            self.assertEqual(snapshot_path.read_bytes(), b"Retained for download")
            return real_file_response(**kwargs)

        with patch("server.FileResponse", side_effect=delete_before_response):
            response = self.client.get(f"/api/download/{record['id']}/{record['output_filename']}")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, b"Retained for download")
        self.assertEqual(response.headers["content-type"], "text/markdown; charset=utf-8")
        self.assertIn(record["output_filename"], response.headers["content-disposition"])
        self.assertEqual(len(snapshot_paths), 1)
        self.assertFalse(snapshot_paths[0].exists(), "Streaming response must clean up its temporary snapshot")

    def test_partial_conversion_is_retained_downloadable_and_retryable(self) -> None:
        partial_text = "# First completed section\nUseful retained content."
        validation = {"partial": True, "validation_skipped": True}

        def partial_generation(*, output_path, **kwargs):
            output_path.write_text(partial_text, encoding="utf-8")
            return PipelineResult(partial_text, {"status": "partial", "failed_chunks": [1], "chunk_count": 2},
                                  validation, output_path.parent)

        with patch("server.execute_generation", side_effect=partial_generation):
            response = self.client.post("/api/convert", data={
                "action": "create_reports", "connector": "the_connector", "pasted_text": "Study source",
            })

        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["success"])
        self.assertTrue(response.json()["partial"])
        self.assertEqual(response.json()["output_text"], partial_text)
        record = server._read_record(response.json()["session_id"])
        self.assertEqual(record["status"], "failed")
        self.assertTrue(record["output_partial"])
        self.assertEqual(record["validation"], validation)
        public = self.client.get("/api/history").json()["conversions"][0]
        self.assertTrue(public["can_continue"])
        self.assertTrue(public["output_available"])
        self.assertTrue(public["output_partial"])
        self.assertTrue(public["download_url"])
        direct = self.client.get(response.json()["download_url"])
        self.assertEqual(direct.status_code, 200)
        self.assertEqual(direct.text.replace("\r\n", "\n"), partial_text)
        archive_response = self.client.get(public["download_url"])
        self.assertEqual(archive_response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(archive_response.content)) as archive:
            self.assertEqual(archive.read(f"output/{record['output_filename']}").decode().replace("\r\n", "\n"), partial_text)
            self.assertTrue(json.loads(archive.read("conversion.json"))["validation"]["validation_skipped"])
        group_archive = self.client.get(f"/api/history/groups/{record['source_group_id']}/download")
        self.assertEqual(group_archive.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(group_archive.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertTrue(manifest["partial"])
            self.assertEqual((manifest["completed_artifacts"], manifest["included_artifacts"], manifest["partial_artifacts"]), (0, 1, 1))

        def successful_retry(*, output_path, **kwargs):
            output_path.write_text("# Complete report", encoding="utf-8")
            return PipelineResult("# Complete report", {}, {"valid": True}, output_path.parent)

        with patch("server.execute_generation", side_effect=successful_retry):
            retried = self.client.post(f"/api/history/{record['id']}/continue")
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertTrue(retried.json()["success"])
        refreshed = self.client.get("/api/history").json()["conversions"][0]
        self.assertEqual(refreshed["status"], "completed")
        self.assertFalse(refreshed["output_partial"])
        self.assertFalse(refreshed["can_continue"])
        self.assertEqual(server._read_record(record["id"])["validation"], {"valid": True})

    def test_failed_retry_preserves_previously_retained_partial_output(self) -> None:
        group_id = "8" * 32
        record = self._output(self._queue(group_id), status="failed", partial=True, content="Prior useful sections")
        record["validation"] = {"partial": True, "validation_skipped": True}
        server._write_record(record)
        output_path = server._conversion_dir(record["id"]) / record["output_path"]
        before = output_path.read_bytes()

        def unavailable_generation(*, output_path, **kwargs):
            self.assertEqual(output_path.read_bytes(), before)
            public = server._public_record(server._read_record(record["id"]))
            self.assertTrue(public["active"])
            self.assertTrue(public["output_partial"])
            self.assertTrue(public["output_available"])
            raise ApplicationError("Provider unavailable during retry")

        with patch("server.execute_generation", side_effect=unavailable_generation):
            response = self.client.post(f"/api/history/{record['id']}/continue")

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(output_path.read_bytes(), before)
        refreshed = server._read_record(record["id"])
        self.assertEqual(refreshed["status"], "failed")
        self.assertTrue(refreshed["output_partial"])
        self.assertEqual(refreshed["validation"], record["validation"])
        public = self.client.get("/api/history").json()["conversions"][0]
        self.assertTrue(public["can_continue"])
        self.assertTrue(public["output_available"])
        self.assertFalse(public["active"])
        download = self.client.get(public["download_url"])
        self.assertEqual(download.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            self.assertEqual(archive.read(f"output/{record['output_filename']}"), before)
            metadata = json.loads(archive.read("conversion.json"))
            self.assertTrue(metadata["output_partial"])
            self.assertTrue(metadata["validation"]["validation_skipped"])

    def test_unmarked_unfinished_output_cannot_be_downloaded_directly_or_as_archive(self) -> None:
        record = self._output(self._queue("5" * 32), status="failed", content="Unvalidated stale output")
        public = server._public_record(record)
        self.assertFalse(public["output_available"])
        self.assertFalse(public["output_partial"])
        self.assertIsNone(public["download_url"])
        self.assertEqual(self.client.get(f"/api/download/{record['id']}/{record['output_filename']}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/history/{record['id']}/download").status_code, 409)
        self.assertEqual(self.client.get(f"/api/history/groups/{record['source_group_id']}/download").status_code, 409)


if __name__ == "__main__":
    unittest.main()
