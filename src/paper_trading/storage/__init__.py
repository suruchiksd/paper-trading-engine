"""SQLite persistence, session lifecycle, and recovery."""

from .database import SQLiteDatabase, UnsupportedSchemaVersion
from .repositories import DuplicateFillError, SessionNotFoundError, SQLiteStore
from .service import (
    InvalidSessionTransition,
    RecoveredSessionRuntime,
    RecoveryConsistencyError,
    SessionRecorder,
    SessionService,
)

__all__ = [
    "DuplicateFillError",
    "InvalidSessionTransition",
    "RecoveredSessionRuntime",
    "RecoveryConsistencyError",
    "SQLiteDatabase",
    "SQLiteStore",
    "SessionNotFoundError",
    "SessionRecorder",
    "SessionService",
    "UnsupportedSchemaVersion",
]
