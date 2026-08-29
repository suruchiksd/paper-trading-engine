import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from paper_trading.models import SessionStatus
from paper_trading.storage import (
    InvalidSessionTransition,
    SQLiteDatabase,
    SQLiteStore,
    SessionService,
    UnsupportedSchemaVersion,
)


def test_database_initializes_version_and_reopens(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    database = SQLiteDatabase(path)
    database.initialize()
    database.initialize()
    with database.connect() as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_unsupported_schema_version_fails_loudly(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "state.sqlite")
    database.initialize()
    with database.connect() as connection:
        connection.execute("UPDATE schema_version SET version=999")
    with pytest.raises(UnsupportedSchemaVersion):
        database.initialize()


def test_session_lifecycle_and_invalid_transition(tmp_path: Path) -> None:
    service = SessionService(SQLiteStore(SQLiteDatabase(tmp_path / "state.sqlite")))
    now = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)
    session = service.create(date(2026, 7, 31), "PAPER_REPLAY", 10_000, timestamp=now)
    assert session.status is SessionStatus.CREATED
    running = service.start(session.session_id, now)
    assert running.status is SessionStatus.RUNNING
    stopped = service.stop(session.session_id, now)
    assert stopped.status is SessionStatus.STOPPED
    with pytest.raises(InvalidSessionTransition):
        service.stop(session.session_id, now)

