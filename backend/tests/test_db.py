from __future__ import annotations

from sqlalchemy import text

from app.db import create_sqlite_engine


def test_sqlite_engine_enables_wal_and_busy_timeout(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "dashboard.sqlite3")
    try:
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA journal_mode")).scalar_one().lower() == "wal"
            assert connection.execute(text("PRAGMA busy_timeout")).scalar_one() == 5000
            assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    finally:
        engine.dispose()
