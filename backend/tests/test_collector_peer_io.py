"""Slow NAS counter writes must not stall HTTP/health or partially commit a batch."""
import asyncio
from datetime import UTC, datetime, timedelta
import threading

import pytest
from sqlalchemy import event, func, select

from app.collectors import Collector
from app.db import create_all, create_session_factory, create_sqlite_engine
from app.models import PeerBaseline, PeerSnapshotRecord
from app.wgeasy import ContractStatus, WireGuardClient


class WgSnapshot:
    async def verify_contract(self):
        return ContractStatus(True, None, tuple(
            WireGuardClient(i, "synthetic-" + str(i), True, "10.66.0." + str(i), None, 100, 200)
            for i in range(2, 15)
        ))


@pytest.fixture
def collector_db(tmp_path):
    engine = create_sqlite_engine(tmp_path / "dashboard.sqlite3")
    create_all(engine)
    sessions = create_session_factory(engine)
    collector = Collector(sessions, wgeasy=WgSnapshot(), mihomo=None,
                          service_probe=None, geodata_dir=tmp_path)
    try:
        yield collector, engine, sessions
    finally:
        engine.dispose()


@pytest.mark.parametrize("existing", [False, True])
def test_counter_writes_do_not_run_on_event_loop(collector_db, existing):
    collector, engine, sessions = collector_db
    now = datetime(2026, 10, 8, tzinfo=UTC)
    if existing:
        asyncio.run(collector._collect_peers(now - timedelta(minutes=1)))
    scheduler_thread = threading.get_ident()
    write_threads = []

    def record_thread(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().startswith("INSERT INTO peer_"):
            write_threads.append(threading.get_ident())

    event.listen(engine, "before_cursor_execute", record_thread)
    try:
        asyncio.run(collector._collect_peers(now))
    finally:
        event.remove(engine, "before_cursor_execute", record_thread)
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(PeerSnapshotRecord)) == (26 if existing else 13)
    assert write_threads
    assert all(thread != scheduler_thread for thread in write_threads)


def test_counter_batch_rolls_back_if_a_later_peer_write_fails(collector_db):
    collector, engine, sessions = collector_db
    inserts = 0

    def fail_second_snapshot(_connection, _cursor, statement, _parameters, _context, _many):
        nonlocal inserts
        if statement.startswith("INSERT INTO peer_snapshots"):
            inserts += 1
            if inserts == 2:
                raise RuntimeError("simulated disk failure")

    event.listen(engine, "before_cursor_execute", fail_second_snapshot)
    try:
        with pytest.raises(RuntimeError, match="simulated disk failure"):
            asyncio.run(collector._collect_peers(datetime(2026, 10, 8, tzinfo=UTC)))
    finally:
        event.remove(engine, "before_cursor_execute", fail_second_snapshot)
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(PeerSnapshotRecord)) == 0
        assert session.scalar(select(func.count()).select_from(PeerBaseline)) == 0
