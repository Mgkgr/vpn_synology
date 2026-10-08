"""Route history must not grow the work done by a scheduled observation."""

import asyncio
from datetime import UTC, datetime
import threading
import time

import pytest
from sqlalchemy import event, select

from app.db import create_all, create_session_factory, create_sqlite_engine
from app.models import ProbeEvent, RouteEvent
from app.wgeasy import ContractStatus
from test_collectors import StubMihomo, StubWgEasy, make_collector


@pytest.fixture
def database(tmp_path):
    engine = create_sqlite_engine(tmp_path / "dashboard.sqlite3")
    create_all(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        session.add_all([
            ProbeEvent(observed_at=datetime(2026, 10, 8, tzinfo=UTC),
                       target="route-state:VPS-FALLBACK", succeeded=True,
                       outbound="WG-IMP", status="selected")
            for _ in range(100)
        ])
    try:
        yield engine, factory
    finally:
        engine.dispose()


def collector_for(factory, tmp_path):
    return make_collector(factory,
        wgeasy=StubWgEasy(ContractStatus(False, "not configured")),
        mihomo=StubMihomo(["HY2-USA"]), geodata_dir=tmp_path / "geodata")


def test_route_observation_loads_only_last_history_row(database, tmp_path):
    """Removing the SQL limit would hydrate all 100 old events again."""
    _engine, factory = database
    loaded = []

    def remember(row, _context):
        if row.target == "route-state:VPS-FALLBACK":
            loaded.append(row.id)

    event.listen(ProbeEvent, "load", remember)
    try:
        asyncio.run(collector_for(factory, tmp_path).run_probe_cycle())
    finally:
        event.remove(ProbeEvent, "load", remember)
    assert loaded == [100]
    with factory() as session:
        transitions = session.scalars(select(RouteEvent)).all()
        assert [(row.previous_outbound, row.new_outbound) for row in transitions] == [("WG-IMP", "HY2-USA")]


@pytest.mark.parametrize("method", ["run_minute", "run_probe_cycle"])
def test_route_transaction_never_runs_on_scheduler_thread(database, tmp_path, method):
    """Disk waits must not stop other scheduled control probes or HTTP requests."""
    engine, factory = database
    scheduler_thread = threading.get_ident()
    query_threads = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().startswith("SELECT") and "probe_events.target =" in statement:
            query_threads.append(threading.get_ident())

    event.listen(engine, "before_cursor_execute", observe)
    try:
        asyncio.run(getattr(collector_for(factory, tmp_path), method)())
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert query_threads
    assert all(thread != scheduler_thread for thread in query_threads)


def test_overlapping_observations_do_not_duplicate_route_transition(database, tmp_path):
    """Minute and manual checks may now reach the same transaction concurrently."""
    _engine, factory = database
    collector = collector_for(factory, tmp_path)

    def slow_row_materialization(_row, _context):
        # Model a slow disk/ORM read, not the persistence implementation.
        time.sleep(0.1)

    async def observe_twice():
        await asyncio.gather(*[
            asyncio.to_thread(collector._persist_selected_exit,
                              datetime(2026, 10, 8, 12, tzinfo=UTC),
                              "VPS-FALLBACK", "HY2-USA")
            for _ in range(2)
        ])

    event.listen(ProbeEvent, "load", slow_row_materialization)
    try:
        asyncio.run(observe_twice())
    finally:
        event.remove(ProbeEvent, "load", slow_row_materialization)
    with factory() as session:
        transitions = session.scalars(select(RouteEvent)).all()
    assert [(row.previous_outbound, row.new_outbound) for row in transitions] == [("WG-IMP", "HY2-USA")]
