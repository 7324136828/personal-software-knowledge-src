from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from global_settings import load_settings, save_settings


class GlobalSettingsStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "history" / "settings.sqlite3"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_first_load_persists_defaults_and_creates_parent_directory(self) -> None:
        self.assertEqual(load_settings(self.db_path), {
            "concurrent_runs": 3,
            "min_concurrent_runs": 1,
            "max_concurrent_runs": 32,
        })
        self.assertTrue(self.db_path.is_file())
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT id, concurrent_runs FROM global_settings").fetchall(),
                [(1, 3)],
            )

    def test_saved_setting_survives_a_new_process(self) -> None:
        saved = save_settings(self.db_path, 7)
        self.assertEqual(saved["concurrent_runs"], 7)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json, sys; from global_settings import load_settings; "
                "print(json.dumps(load_settings(sys.argv[1])))",
                str(self.db_path),
            ],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(result.stdout), saved)

    def test_invalid_updates_preserve_the_saved_setting(self) -> None:
        save_settings(self.db_path, 6)
        for invalid in (True, False, "3", 3.0, None, 0, -1, 33):
            with self.subTest(value=invalid):
                with self.assertRaises(ValueError):
                    save_settings(self.db_path, invalid)
                self.assertEqual(load_settings(self.db_path)["concurrent_runs"], 6)

    def test_boundary_values_are_saved(self) -> None:
        for concurrent_runs in (1, 32):
            with self.subTest(concurrent_runs=concurrent_runs):
                self.assertEqual(
                    save_settings(self.db_path, concurrent_runs)["concurrent_runs"],
                    concurrent_runs,
                )
                self.assertEqual(load_settings(self.db_path)["concurrent_runs"], concurrent_runs)

    def test_simultaneous_initialization_and_updates_keep_one_valid_row(self) -> None:
        values = list(range(1, 13))
        with ThreadPoolExecutor(max_workers=8) as executor:
            saved = list(executor.map(lambda value: save_settings(self.db_path, value), values))
        self.assertEqual([settings["concurrent_runs"] for settings in saved], values)
        with closing(sqlite3.connect(self.db_path)) as connection:
            rows = connection.execute("SELECT id, concurrent_runs FROM global_settings").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 1)
        self.assertIn(rows[0][1], values)

    def test_database_constraints_reject_invalid_rows(self) -> None:
        load_settings(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as connection:
            for statement, parameters in (
                ("INSERT INTO global_settings (id, concurrent_runs) VALUES (?, ?)", (2, 3)),
                ("UPDATE global_settings SET concurrent_runs = ? WHERE id = 1", (0,)),
                ("UPDATE global_settings SET concurrent_runs = ? WHERE id = 1", (33,)),
                ("UPDATE global_settings SET concurrent_runs = ? WHERE id = 1", (3.5,)),
                ("UPDATE global_settings SET concurrent_runs = ? WHERE id = 1", ("invalid",)),
            ):
                with self.subTest(parameters=parameters):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(statement, parameters)
        self.assertEqual(load_settings(self.db_path)["concurrent_runs"], 3)


if __name__ == "__main__":
    unittest.main()
