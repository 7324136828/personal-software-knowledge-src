from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import server


class RecordingExecutor:
    """Record reserved jobs without starting background workers."""

    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.rejected: set[str] = set()

    def submit(self, function, conversion_id: str):
        if conversion_id not in server.DISPATCHED_CONVERSIONS:
            raise AssertionError("The job must be reserved before it is submitted")
        if conversion_id in self.rejected:
            raise RuntimeError("The executor rejected this job")
        self.submitted.append(conversion_id)


class GlobalSettingsServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        directory = self.resources.enter_context(tempfile.TemporaryDirectory())
        self.history_dir = Path(directory)
        self.executor = RecordingExecutor()
        for name, value in (
            ("HISTORY_DIR", self.history_dir),
            ("ACTIVE_CONVERSIONS", set()),
            ("DISPATCHED_CONVERSIONS", set()),
            ("QUEUED_API_KEYS", {}),
            ("CANCEL_EVENTS", {}),
            ("QUEUE_WAKE", threading.Event()),
            ("QUEUE_EXECUTOR", self.executor),
        ):
            self.resources.enter_context(patch.object(server, name, value))
        self.dispatcher = self.resources.enter_context(patch.object(server, "_ensure_queue_dispatcher"))
        self.client = self.resources.enter_context(TestClient(server.app))

    def queued_record(self, number: int, *, priority: int = 0,
                      created_at: str = "2026-10-04T00:00:00Z", status: str = "queued") -> str:
        conversion_id = f"{number:032x}"
        server._write_record({
            "id": conversion_id,
            "status": status,
            "priority": priority,
            "created_at": created_at,
            "source_group_id": "study-set",
            "import_id": "study-set-import",
        })
        return conversion_id

    def set_limit(self, limit: int) -> None:
        response = self.client.put("/api/settings", json={"concurrent_runs": limit})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["concurrent_runs"], limit)

    def test_default_settings_and_database_backed_save(self) -> None:
        response = self.client.get("/api/settings")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "concurrent_runs": 3, "min_concurrent_runs": 1, "max_concurrent_runs": 32,
        })

        self.set_limit(7)
        self.assertTrue(server.QUEUE_WAKE.is_set())
        database = self.history_dir / "settings.sqlite3"
        self.assertTrue(database.is_file())
        self.assertEqual(database.read_bytes()[:16], b"SQLite format 3\x00")
        with TestClient(server.app) as fresh_client:
            reloaded = fresh_client.get("/api/settings")
        self.assertEqual(reloaded.status_code, 200)
        self.assertEqual(reloaded.json()["concurrent_runs"], 7)
        self.dispatcher.assert_not_called()

    def test_deleting_all_file_history_preserves_saved_global_settings(self) -> None:
        self.set_limit(8)
        for number, status in enumerate(("completed", "failed", "queued"), 1):
            self.queued_record(number, status=status)
        response = self.client.delete("/api/history/groups/study-set?include_completed=true")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["affected"], 3)
        self.assertEqual(server._records_on_disk(), [])
        self.assertEqual(list(self.history_dir.iterdir()), [self.history_dir / "settings.sqlite3"])
        with TestClient(server.app) as fresh_client:
            settings = fresh_client.get("/api/settings")
        self.assertEqual(settings.status_code, 200)
        self.assertEqual(settings.json()["concurrent_runs"], 8)

    def test_invalid_values_do_not_change_saved_limit_or_wake_queue(self) -> None:
        self.set_limit(5)
        for value in (None, True, False, 2.5, 3.0, "3", 0, -1, 33):
            with self.subTest(value=value):
                server.QUEUE_WAKE.clear()
                response = self.client.put("/api/settings", json={"concurrent_runs": value})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertFalse(server.QUEUE_WAKE.is_set())
                self.assertEqual(self.client.get("/api/settings").json()["concurrent_runs"], 5)
        server.QUEUE_WAKE.clear()
        response = self.client.put("/api/settings", json={})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertFalse(server.QUEUE_WAKE.is_set())
        self.assertEqual(self.client.get("/api/settings").json()["concurrent_runs"], 5)

    def test_write_failure_does_not_change_saved_limit_or_queue(self) -> None:
        self.set_limit(4)
        queued = self.queued_record(1)
        active = self.queued_record(2, status="in_progress")
        server.ACTIVE_CONVERSIONS.add(active)
        for error in (OSError("Disk is unavailable"), sqlite3.OperationalError("Database is read only")):
            with self.subTest(error=type(error).__name__):
                server.QUEUE_WAKE.clear()
                with patch.object(server, "save_settings", side_effect=error), self.assertLogs("server", level="ERROR"):
                    response = self.client.put("/api/settings", json={"concurrent_runs": 10})
                self.assertEqual(response.status_code, 500, response.text)
                self.assertFalse(server.QUEUE_WAKE.is_set())
                self.assertEqual(self.client.get("/api/settings").json()["concurrent_runs"], 4)
                self.assertEqual(server.ACTIVE_CONVERSIONS, {active})
                self.assertEqual(server.DISPATCHED_CONVERSIONS, set())
                self.assertEqual(server._read_record(queued)["status"], "queued")
                self.assertEqual(self.executor.submitted, [])

    def test_unavailable_settings_fail_closed(self) -> None:
        self.queued_record(1)
        with patch.object(server, "load_settings", side_effect=sqlite3.OperationalError("Locked")):
            with self.assertLogs("server", level="ERROR"):
                response = self.client.get("/api/settings")
            self.assertEqual(response.status_code, 500, response.text)
            with self.assertRaises(sqlite3.OperationalError):
                server._dispatch_queued_conversions()
        self.assertEqual(server.DISPATCHED_CONVERSIONS, set())
        self.assertEqual(self.executor.submitted, [])

    def test_default_limit_reserves_three_jobs_and_counts_reservations(self) -> None:
        jobs = [self.queued_record(number) for number in range(1, 6)]
        self.assertEqual(server._dispatch_queued_conversions(), 3)
        self.assertEqual(self.executor.submitted, jobs[:3])
        self.assertEqual(server.DISPATCHED_CONVERSIONS, set(jobs[:3]))
        self.assertEqual(server._dispatch_queued_conversions(), 0)
        self.assertEqual(self.executor.submitted, jobs[:3])

    def test_increased_limit_dispatches_more_than_three_jobs(self) -> None:
        jobs = [self.queued_record(number) for number in range(1, 9)]
        self.assertEqual(server._dispatch_queued_conversions(), 3)
        self.set_limit(6)
        self.assertEqual(server._dispatch_queued_conversions(), 3)
        self.assertEqual(self.executor.submitted, jobs[:6])
        self.assertEqual(server.DISPATCHED_CONVERSIONS, set(jobs[:6]))

    def test_lower_limit_preserves_active_work_and_waits_for_free_slots(self) -> None:
        active = {self.queued_record(number, status="in_progress") for number in range(1, 6)}
        queued = self.queued_record(6)
        server.ACTIVE_CONVERSIONS.update(active)
        cancellation = threading.Event()
        server.CANCEL_EVENTS[next(iter(active))] = cancellation
        self.set_limit(2)

        self.assertEqual(server._dispatch_queued_conversions(), 0)
        self.assertEqual(server.ACTIVE_CONVERSIONS, active)
        self.assertFalse(cancellation.is_set())
        self.assertEqual(server._read_record(queued)["status"], "queued")

        survivor = next(iter(active))
        server.ACTIVE_CONVERSIONS.intersection_update({survivor})
        self.assertEqual(server._dispatch_queued_conversions(), 1)
        self.assertEqual(self.executor.submitted, [queued])
        self.assertEqual(server.ACTIVE_CONVERSIONS, {survivor})

    def test_active_and_reserved_jobs_share_the_limit_without_double_counting(self) -> None:
        transitioning = self.queued_record(1, status="in_progress")
        reserved = self.queued_record(2)
        queued = self.queued_record(3)
        server.ACTIVE_CONVERSIONS.add(transitioning)
        server.DISPATCHED_CONVERSIONS.update({transitioning, reserved})
        self.assertEqual(server._dispatch_queued_conversions(), 1)
        self.assertEqual(self.executor.submitted, [queued])
        self.assertEqual(server.DISPATCHED_CONVERSIONS, {transitioning, reserved, queued})

    def test_dispatch_preserves_priority_then_creation_order(self) -> None:
        later = self.queued_record(1, priority=5, created_at="2026-10-04T02:00:00Z")
        earlier = self.queued_record(2, priority=5, created_at="2026-10-04T01:00:00Z")
        first = self.queued_record(3, priority=1)
        self.queued_record(4, priority=0, status="completed")
        self.queued_record(5, priority=0, status="failed")
        self.assertEqual(server._dispatch_queued_conversions(), 3)
        self.assertEqual(self.executor.submitted, [first, earlier, later])

    def test_rejected_submission_releases_reservation_and_remains_retryable(self) -> None:
        first = self.queued_record(1)
        second = self.queued_record(2)
        self.executor.rejected.add(first)
        with self.assertLogs("server", level="ERROR"):
            self.assertEqual(server._dispatch_queued_conversions(), 1)
        self.assertEqual(server.DISPATCHED_CONVERSIONS, {second})
        self.assertEqual(server._read_record(first)["status"], "queued")
        self.executor.rejected.clear()
        self.assertEqual(server._dispatch_queued_conversions(), 1)
        self.assertEqual(self.executor.submitted, [second, first])
        self.assertEqual(server.DISPATCHED_CONVERSIONS, {first, second})


if __name__ == "__main__":
    unittest.main()
