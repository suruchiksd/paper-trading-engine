"""SQLite connection and transaction management."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .schema import SCHEMA_SQL, SCHEMA_VERSION


class UnsupportedSchemaVersion(RuntimeError):
    pass


class SQLiteDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            version_table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            if version_table_exists:
                rows = connection.execute("SELECT version FROM schema_version").fetchall()
                if len(rows) != 1 or rows[0]["version"] != SCHEMA_VERSION:
                    versions = [row["version"] for row in rows]
                    raise UnsupportedSchemaVersion(
                        f"database schema version {versions} is unsupported; expected {SCHEMA_VERSION}"
                    )
            connection.executescript(SCHEMA_SQL)
            if not version_table_exists:
                connection.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
