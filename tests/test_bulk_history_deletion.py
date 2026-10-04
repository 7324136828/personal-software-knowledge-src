from __future__ import annotations

import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from errors import GenerationCancelled
import server


class BulkHistoryDeletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        directory = self.resources.enter_context(tempfile.TemporaryDirectory())
        self.history_dir = Path(directory)
        for name, value in (
            ("HISTORY_DIR", self.history_dir),
            ("ACTIVE_CONVERSIONS", set()),
            ("DISPATCHED_CONVERSIONS", set()),
            ("QUEUED_API_KEYS", {}),
            ("CANCEL_EVENTS", {}),
            ("QUEUE_WAKE", threading.Event()),
        ):
            self.resources.enter_context(patch.object(server, name, value))
        self.resources.enter_context(patch.object(server, "_ensure_queue_dispatcher"))
        self.client = self.resources.enter_context(TestClient(server.app))

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

    def _output(self, record: dict, *, status: str, partial: bool = False) -> dict:
        output = server._conversion_dir(record["id"]) / record["output_path"]
        output.write_text("Retained study material", encoding="utf-8")
        record.update(status=status, output_partial=partial)
        server._write_record(record)
        return record

    def _delete(self, group_ids: list[str]):
        return self.client.post("/api/history/delete", json={"group_ids": group_ids})

    def test_deletes_multiple_selected_sources_including_partial_and_outputless_jobs(self) -> None:
        first_group, second_group, unrelated_group = "a" * 32, "b" * 32, "c" * 32
        selected = [
            self._output(self._queue(first_group), status="completed"),
            self._output(self._queue(first_group, action="create_flashcards"), status="failed", partial=True),
            self._queue(first_group, action="create_quizzes"),
            self._output(self._queue(second_group), status="completed"),
            self._queue(second_group, action="create_quizzes"),
        ]
        # A matching filename must never expand the selection to another study set.
        unrelated = self._queue(unrelated_group)
        unrelated_metadata = server._metadata_path(unrelated["id"]).read_bytes()
        settings = self.client.put("/api/settings", json={"concurrent_runs": 7})
        self.assertEqual(settings.status_code, 200, settings.text)
        database = self.history_dir / "settings.sqlite3"
        database_before = database.read_bytes()

        response = self._delete([second_group, first_group, second_group])

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["affected"], 5)
        self.assertEqual(response.json()["deleted_group_ids"], [second_group, first_group])
        self.assertEqual(response.json()["missing_group_ids"], [])
        self.assertEqual(response.json()["discarding"], 0)
        for record in selected:
            self.assertFalse(server._conversion_dir(record["id"]).exists())
            self.assertNotIn(record["id"], server.QUEUED_API_KEYS)
        self.assertEqual(server._metadata_path(unrelated["id"]).read_bytes(), unrelated_metadata)
        self.assertIn(unrelated["id"], server.QUEUED_API_KEYS)
        self.assertEqual(database.read_bytes(), database_before)
        self.assertEqual(self.client.get("/api/settings").json()["concurrent_runs"], 7)
        self.assertEqual([record["id"] for record in self.client.get("/api/history").json()["conversions"]],
                         [unrelated["id"]])

    def test_invalid_selection_is_rejected_before_any_history_or_credentials_change(self) -> None:
        group_id = "d" * 32
        record = self._queue(group_id)
        before = server._metadata_path(record["id"]).read_bytes()
        credentials = dict(server.QUEUED_API_KEYS)
        for payload in ({}, {"group_ids": []}, {"group_ids": "invalid"}, {"group_ids": None},
                        {"group_ids": [42]}, {"group_ids": [group_id, {}]},
                        {"group_ids": [group_id, ""]}, {"group_ids": [group_id, "   "]},
                        {"group_ids": [group_id, None]}):
            with self.subTest(payload=payload):
                response = self.client.post("/api/history/delete", json=payload)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(server._metadata_path(record["id"]).read_bytes(), before)
                self.assertEqual(server.QUEUED_API_KEYS, credentials)

    def test_missing_groups_are_reported_once_while_known_groups_are_deleted(self) -> None:
        known_group, missing_group, another_missing = "e" * 32, "f" * 32, "1" * 32
        record = self._queue(known_group)

        response = self._delete([missing_group, known_group, missing_group, another_missing])

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["affected"], 1)
        self.assertEqual(response.json()["deleted_group_ids"], [known_group])
        self.assertEqual(response.json()["missing_group_ids"], [missing_group, another_missing])
        self.assertFalse(server._conversion_dir(record["id"]).exists())

    def test_all_missing_groups_return_not_found_without_changing_other_history(self) -> None:
        record = self._queue("2" * 32)
        before = server._metadata_path(record["id"]).read_bytes()

        response = self._delete(["3" * 32, "3" * 32])

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(server._metadata_path(record["id"]).read_bytes(), before)
        self.assertIn(record["id"], server.QUEUED_API_KEYS)

    def test_deleting_every_study_set_preserves_settings_for_a_new_client(self) -> None:
        response = self.client.put("/api/settings", json={"concurrent_runs": 8})
        self.assertEqual(response.status_code, 200, response.text)
        first = self._queue("4" * 32)
        second = self._output(self._queue("5" * 32), status="completed")

        response = self._delete([first["source_group_id"], second["source_group_id"]])

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["affected"], 2)
        self.assertEqual(self.client.get("/api/history").json()["conversions"], [])
        self.assertEqual(list(self.history_dir.iterdir()), [self.history_dir / "settings.sqlite3"])
        with TestClient(server.app) as restarted_client:
            self.assertEqual(restarted_client.get("/api/settings").json()["concurrent_runs"], 8)

    def test_active_job_is_cancelled_and_removed_after_it_stops(self) -> None:
        active_group, finished_group, unrelated_group = "6" * 32, "7" * 32, "8" * 32
        active = self._queue(active_group)
        finished = self._output(self._queue(finished_group), status="completed")
        unrelated = self._queue(unrelated_group)
        started = threading.Event()
        cancellation_seen = threading.Event()
        release = threading.Event()
        responses = []

        def cancellable_generation(*, options, **kwargs):
            started.set()
            if not server.CANCEL_EVENTS[active["id"]].wait(timeout=5):
                raise RuntimeError("Test conversion was not cancelled")
            self.assertTrue(options.cancel_check())
            cancellation_seen.set()
            if not release.wait(timeout=5):
                raise RuntimeError("Test did not release the cancelled conversion")
            raise GenerationCancelled("Deleted by user")

        def continue_conversion():
            with TestClient(server.app) as client:
                responses.append(client.post(f"/api/history/{active['id']}/continue"))

        with patch("server.execute_generation", side_effect=cancellable_generation):
            worker = threading.Thread(target=continue_conversion)
            worker.start()
            try:
                self.assertTrue(started.wait(timeout=5))
                response = self._delete([finished_group, active_group])
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["affected"], 2)
                self.assertEqual(response.json()["deleted_group_ids"], [finished_group, active_group])
                self.assertEqual(response.json()["discarding"], 1)
                self.assertTrue(cancellation_seen.wait(timeout=5))
                self.assertEqual(server._read_record(active["id"])["status"], "discarding")
                self.assertTrue(server._conversion_dir(active["id"]).exists())
                self.assertFalse(server._conversion_dir(finished["id"]).exists())
                self.assertTrue(server._conversion_dir(unrelated["id"]).exists())
            finally:
                event = server.CANCEL_EVENTS.get(active["id"])
                if event is not None:
                    event.set()
                release.set()
                worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].status_code, 410)
        self.assertFalse(server._conversion_dir(active["id"]).exists())
        self.assertNotIn(active["id"], server.ACTIVE_CONVERSIONS)
        self.assertNotIn(active["id"], server.CANCEL_EVENTS)
        self.assertEqual([record["id"] for record in self.client.get("/api/history").json()["conversions"]],
                         [unrelated["id"]])

    def test_queue_cannot_reserve_selected_jobs_between_snapshot_and_deletion(self) -> None:
        selected = self._queue("9" * 32)
        unrelated = self._queue("a1" * 16)
        deletion_started = threading.Event()
        release_deletion = threading.Event()
        dispatcher_started = threading.Event()
        dispatcher_done = threading.Event()
        responses, dispatched, submitted = [], [], []
        real_discard = server.discard_conversion

        def paused_deletion(conversion_id):
            # The snapshot has already been taken, but discard_conversion has
            # not acquired its own lock yet. The bulk request must still hold it.
            deletion_started.set()
            if not release_deletion.wait(timeout=5):
                raise RuntimeError("Test did not release the selected deletion")
            return real_discard(conversion_id)

        def delete():
            with TestClient(server.app) as client:
                responses.append(client.post("/api/history/delete", json={"group_ids": [selected["source_group_id"]]}))

        def dispatch():
            dispatcher_started.set()
            dispatched.append(server._dispatch_queued_conversions())
            dispatcher_done.set()

        class RecordingExecutor:
            def submit(self, function, conversion_id):
                submitted.append(conversion_id)

        with patch.object(server, "discard_conversion", side_effect=paused_deletion), \
                patch.object(server, "QUEUE_EXECUTOR", RecordingExecutor()):
            delete_worker = threading.Thread(target=delete)
            dispatcher = threading.Thread(target=dispatch)
            delete_worker.start()
            try:
                self.assertTrue(deletion_started.wait(timeout=5))
                dispatcher.start()
                self.assertTrue(dispatcher_started.wait(timeout=5))
                self.assertFalse(dispatcher_done.wait(timeout=0.05))
                self.assertNotIn(selected["id"], server.DISPATCHED_CONVERSIONS)
            finally:
                release_deletion.set()
                delete_worker.join(timeout=5)
                if dispatcher.ident is not None:
                    dispatcher.join(timeout=5)

        self.assertFalse(delete_worker.is_alive())
        self.assertFalse(dispatcher.is_alive())
        self.assertEqual(responses[0].status_code, 200, responses[0].text)
        self.assertEqual(dispatched, [1])
        self.assertEqual(submitted, [unrelated["id"]])
        self.assertEqual(server.DISPATCHED_CONVERSIONS, {unrelated["id"]})
        self.assertFalse(server._conversion_dir(selected["id"]).exists())

    def test_active_cleanup_failure_remains_visible_and_allows_retrying_deletion(self) -> None:
        group_id = "e1" * 16
        record = self._queue(group_id)
        started = threading.Event()
        release = threading.Event()
        responses = []
        real_rmtree = server.shutil.rmtree

        def cancellable_generation(**kwargs):
            started.set()
            if not server.CANCEL_EVENTS[record["id"]].wait(timeout=5):
                raise RuntimeError("Test conversion was not cancelled")
            if not release.wait(timeout=5):
                raise RuntimeError("Test did not release the cancelled conversion")
            raise GenerationCancelled("Deleted by user")

        def fail_after_removing_metadata(directory, *args, **kwargs):
            if Path(directory) == server._conversion_dir(record["id"]):
                server._metadata_path(record["id"]).unlink()
                raise OSError("private-active-cleanup-detail")
            return real_rmtree(directory, *args, **kwargs)

        def continue_conversion():
            with TestClient(server.app) as client:
                responses.append(client.post(f"/api/history/{record['id']}/continue"))

        with patch("server.execute_generation", side_effect=cancellable_generation), \
                patch("server.shutil.rmtree", side_effect=fail_after_removing_metadata), \
                self.assertLogs("server", level="ERROR"):
            worker = threading.Thread(target=continue_conversion)
            worker.start()
            try:
                self.assertTrue(started.wait(timeout=5))
                response = self._delete([group_id])
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["discarding"], 1)
                self.assertEqual(response.json()["deleted_group_ids"], [group_id])
            finally:
                event = server.CANCEL_EVENTS.get(record["id"])
                if event is not None:
                    event.set()
                release.set()
                worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].status_code, 500)
        self.assertNotIn("private-active-cleanup-detail", responses[0].text)
        self.assertNotIn(record["id"], server.ACTIVE_CONVERSIONS)
        self.assertNotIn(record["id"], server.CANCEL_EVENTS)
        restored = server._read_record(record["id"])
        self.assertEqual(restored["status"], "failed")
        self.assertTrue(restored["deletion_error"])
        public = self.client.get("/api/history").json()["conversions"][0]
        self.assertEqual(public["id"], record["id"])
        self.assertEqual(public["status"], "failed")
        self.assertFalse(public["active"])
        self.assertFalse(public["can_continue"])
        self.assertTrue(public["can_discard"])
        with patch("server.execute_generation") as generation:
            continued = self.client.post(f"/api/history/{record['id']}/continue")
        self.assertEqual(continued.status_code, 409, continued.text)
        generation.assert_not_called()
        retried = self._delete([group_id])
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(retried.json()["deleted_group_ids"], [group_id])
        self.assertFalse(server._conversion_dir(record["id"]).exists())

    def test_history_recovers_failed_cleanup_of_discarding_job_after_restart(self) -> None:
        group_id = "f1" * 16
        record = self._queue(group_id)
        record["status"] = "discarding"
        server._write_record(record)
        real_rmtree = server.shutil.rmtree

        def fail_after_removing_metadata(directory, *args, **kwargs):
            if Path(directory) == server._conversion_dir(record["id"]):
                server._metadata_path(record["id"]).unlink()
                raise OSError("private-history-cleanup-detail")
            return real_rmtree(directory, *args, **kwargs)

        with patch("server.shutil.rmtree", side_effect=fail_after_removing_metadata), \
                self.assertLogs("server", level="ERROR"):
            response = self.client.get("/api/history")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("private-history-cleanup-detail", response.text)
        public = response.json()["conversions"][0]
        self.assertEqual(public["id"], record["id"])
        self.assertEqual(public["status"], "failed")
        self.assertFalse(public["can_continue"])
        self.assertTrue(public["can_discard"])
        self.assertTrue(server._read_record(record["id"])["deletion_error"])
        # Future refreshes retain the failed deletion until the user retries it.
        self.assertEqual(self.client.get("/api/history").json()["conversions"][0]["id"], record["id"])
        retried = self._delete([group_id])
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(retried.json()["deleted_group_ids"], [group_id])
        self.assertFalse(server._conversion_dir(record["id"]).exists())

    def test_partial_filesystem_failure_keeps_failed_group_retryable_and_deletes_other_groups(self) -> None:
        failed_group, first_group, last_group = "b1" * 16, "c1" * 16, "d1" * 16
        failed = self._queue(failed_group)
        # Continue deleting other jobs in the same group as well as other study sets.
        failed_group_other = self._queue(failed_group, action="create_quizzes")
        first = self._queue(first_group)
        last = self._output(self._queue(last_group), status="completed")
        real_rmtree = server.shutil.rmtree

        def fail_after_removing_metadata(directory, *args, **kwargs):
            if Path(directory) == server._conversion_dir(failed["id"]):
                server._metadata_path(failed["id"]).unlink()
                raise OSError("private-path-or-filesystem-detail")
            return real_rmtree(directory, *args, **kwargs)

        with patch("server.shutil.rmtree", side_effect=fail_after_removing_metadata):
            response = self._delete([first_group, failed_group, last_group])

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["affected"], 3)
        self.assertEqual(response.json()["deleted_group_ids"], [first_group, last_group])
        self.assertEqual(response.json()["missing_group_ids"], [])
        self.assertEqual(response.json()["discarding"], 0)
        failures = response.json()["failed_groups"]
        self.assertEqual([item["group_id"] for item in failures], [failed_group])
        self.assertTrue(failures[0]["error"])
        self.assertNotIn("private-path-or-filesystem-detail", response.text)
        restored = server._read_record(failed["id"])
        self.assertEqual(restored["id"], failed["id"])
        self.assertEqual(restored["status"], "failed")
        self.assertTrue(restored["deletion_error"])
        self.assertIn(failed["id"], server.QUEUED_API_KEYS)
        for deleted in (first, failed_group_other, last):
            self.assertFalse(server._conversion_dir(deleted["id"]).exists())
        self.assertEqual([record["id"] for record in self.client.get("/api/history").json()["conversions"]],
                         [failed["id"]])
        with patch.object(server, "QUEUE_EXECUTOR") as executor:
            self.assertEqual(server._dispatch_queued_conversions(), 0)
        executor.submit.assert_not_called()
        with patch("server.execute_generation") as generation:
            continued = self.client.post(f"/api/history/{failed['id']}/continue")
        self.assertEqual(continued.status_code, 409, continued.text)
        generation.assert_not_called()
        retried = self._delete([failed_group])
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(retried.json()["deleted_group_ids"], [failed_group])
        self.assertFalse(server._conversion_dir(failed["id"]).exists())


if __name__ == "__main__":
    unittest.main()
