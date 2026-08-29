from datetime import UTC, date, datetime
from pathlib import Path
from subprocess import run
from sys import executable

from paper_trading.portfolio import Portfolio
from paper_trading.storage import SQLiteDatabase, SQLiteStore, SessionService


def test_session_status_cli_inspects_temporary_database(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite"
    service = SessionService(SQLiteStore(SQLiteDatabase(database_path)))
    session = service.create(date(2026, 7, 31), "PAPER_REPLAY", 10_000)
    service.start(session.session_id)
    service.complete(session.session_id, Portfolio(10_000), datetime.now(UTC))
    project_root = Path(__file__).parents[1]
    result = run(
        [
            executable,
            str(project_root / "scripts/session_status.py"),
            "--session",
            session.session_id,
            "--database",
            str(database_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert f"Session: {session.session_id}" in result.stdout
    assert "Status: COMPLETED" in result.stdout
    assert "Orders: 0 | Active orders: 0 | Fills: 0" in result.stdout
    assert "Cash: 10000.00 | Equity: 10000.00" in result.stdout
