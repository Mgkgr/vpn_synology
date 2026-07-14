"""Synchronous SQLite database helpers for the dashboard backend."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Base class shared by all persistence models."""


def create_sqlite_engine(database_path: str | Path) -> Engine:
    """Create a synchronous SQLite engine with foreign keys enabled."""

    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite+pysqlite:///{path.as_posix()}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection: Connection, _record: object) -> None:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return sessions which keep loaded values available after commits."""

    return sessionmaker(bind=engine, expire_on_commit=False)


def create_all(engine: Engine) -> None:
    """Create the schema and apply idempotent additive SQLite migrations."""

    from app import models  # noqa: F401 - registers mapped tables on Base.

    Base.metadata.create_all(engine)
    _apply_additive_sqlite_migrations(engine)
    _seed_probe_targets(engine)


_SQLITE_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "peer_snapshots": {"latest_handshake_at": "VARCHAR(32)"},
    "probe_events": {
        "endpoint": "VARCHAR(255)",
        "outbound": "VARCHAR(255)",
        "status": "VARCHAR(64)",
        "status_code": "INTEGER",
        "error_text": "TEXT",
    },
    "route_events": {"previous_outbound": "VARCHAR(255)", "new_outbound": "VARCHAR(255)"},
    "audit_events": {
        "revision_number": "INTEGER",
        "succeeded": "BOOLEAN",
        "status_code": "INTEGER",
        "error_text": "TEXT",
        "restored": "BOOLEAN",
    },
    "dashboard_owners": {"singleton_marker": "INTEGER NOT NULL DEFAULT 1"},
    "dashboard_sessions": {
        "csrf_token_ciphertext": "TEXT NOT NULL DEFAULT ''",
        "actor_username": "VARCHAR(255)",
    },
    "geo_updates": {
        "operation": "VARCHAR(64)",
        "succeeded": "BOOLEAN",
        "status_code": "INTEGER",
        "error_text": "TEXT",
    },
}


def _apply_additive_sqlite_migrations(engine: Engine) -> None:
    """Upgrade databases created by Tasks 1--4 without destructive schema changes."""

    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        inspector = inspect(connection)
        for table, additions in _SQLITE_ADDITIVE_COLUMNS.items():
            existing = {column["name"] for column in inspector.get_columns(table)}
            for column, ddl in additions.items():
                if column not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        owner_unique_constraints = inspector.get_unique_constraints("dashboard_owners")
        owner_unique_indexes = inspector.get_indexes("dashboard_owners")
        singleton_is_unique = any(
            constraint.get("column_names") == ["singleton_marker"] for constraint in owner_unique_constraints
        ) or any(
            index.get("unique") and index.get("column_names") == ["singleton_marker"] for index in owner_unique_indexes
        )
        if not singleton_is_unique:
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "ix_dashboard_owners_singleton_marker_unique ON dashboard_owners (singleton_marker)"
                )
            )


def _seed_probe_targets(engine: Engine) -> None:
    """Idempotently materialize the fixed safe probe catalogue for a new DB."""

    if engine.dialect.name != "sqlite":
        return
    from app.probe_targets import APPROVED_PROBE_TARGETS

    with engine.begin() as connection:
        for item in APPROVED_PROBE_TARGETS:
            connection.execute(
                text(
                    "INSERT OR IGNORE INTO probe_targets "
                    "(key, label, url, enabled, position) "
                    "VALUES (:key, :label, :url, :enabled, :position)"
                ),
                {
                    "key": item.key,
                    "label": item.label,
                    "url": item.url,
                    "enabled": item.default_enabled,
                    "position": item.position,
                },
            )
