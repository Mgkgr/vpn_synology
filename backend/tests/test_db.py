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


def test_health_schema_is_additive_and_repeatable(tmp_path):
    from app.db import create_all
    from sqlalchemy import inspect

    engine = create_sqlite_engine(tmp_path / "old.sqlite3")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE unrelated_user_data (value TEXT NOT NULL)"))
            connection.execute(text("INSERT INTO unrelated_user_data VALUES ('keep')"))
        create_all(engine)
        create_all(engine)
        assert {"outbound_health_states", "outbound_control_cycles", "outbound_incidents"} <= set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert connection.execute(text("SELECT value FROM unrelated_user_data")).scalar_one() == "keep"
    finally:
        engine.dispose()
