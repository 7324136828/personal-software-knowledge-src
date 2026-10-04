"""Persist the application's global processing settings in SQLite."""

from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator, TypedDict


DEFAULT_CONCURRENT_RUNS = 3
MIN_CONCURRENT_RUNS = 1
MAX_CONCURRENT_RUNS = 32


class GlobalSettings(TypedDict):
    concurrent_runs: int
    min_concurrent_runs: int
    max_concurrent_runs: int


@contextmanager
def _settings_connection(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Initialize and use the singleton settings row in one transaction."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10)) as connection:
        with connection:
            # Serialize initialization and updates across threads and processes.
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS global_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    concurrent_runs INTEGER NOT NULL CHECK (
                        typeof(concurrent_runs) = 'integer'
                        AND concurrent_runs BETWEEN {MIN_CONCURRENT_RUNS} AND {MAX_CONCURRENT_RUNS}
                    )
                )
                """
            )
            yield connection


def _read_settings(connection: sqlite3.Connection) -> GlobalSettings:
    row = connection.execute(
        "SELECT concurrent_runs FROM global_settings WHERE id = 1"
    ).fetchone()
    return {
        "concurrent_runs": row[0],
        "min_concurrent_runs": MIN_CONCURRENT_RUNS,
        "max_concurrent_runs": MAX_CONCURRENT_RUNS,
    }


def load_settings(db_path: str | Path) -> GlobalSettings:
    """Read persisted settings, saving defaults on the first use."""
    with _settings_connection(db_path) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO global_settings (id, concurrent_runs) VALUES (1, ?)",
            (DEFAULT_CONCURRENT_RUNS,),
        )
        return _read_settings(connection)


def save_settings(db_path: str | Path, concurrent_runs: int) -> GlobalSettings:
    """Save a validated concurrency limit and return the resulting settings."""
    if (
        not isinstance(concurrent_runs, int)
        or isinstance(concurrent_runs, bool)
        or not MIN_CONCURRENT_RUNS <= concurrent_runs <= MAX_CONCURRENT_RUNS
    ):
        raise ValueError(
            f"concurrent_runs must be an integer from {MIN_CONCURRENT_RUNS} to {MAX_CONCURRENT_RUNS}."
        )
    with _settings_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO global_settings (id, concurrent_runs) VALUES (1, ?)
            ON CONFLICT (id) DO UPDATE SET concurrent_runs = excluded.concurrent_runs
            """,
            (concurrent_runs,),
        )
        return _read_settings(connection)
