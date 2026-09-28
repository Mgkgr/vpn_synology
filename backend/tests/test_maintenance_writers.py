import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.collectors import Collector
from app.db import create_all, create_session_factory, create_sqlite_engine
from app.maintenance_client import MaintenanceBusy, MaintenanceClient
from app.models import GeoUpdate, ManagedRulePolicy
from app.policy_rules import ManagedRuleService
from app.rules import RuleService
from app.settings import Settings
from app.wgeasy import WgEasyAdapter

JOB = "a" * 32


@pytest.fixture
def writer_parts(tmp_path):
    engine = create_sqlite_engine(tmp_path / "state.sqlite3")
    create_all(engine)
    sessions = create_session_factory(engine)
    frames = []
    async def busy(frame):
        frames.append(frame)
        return {"ok": False, "error": "busy", "job_id": JOB}
    yield sessions, MaintenanceClient(enabled=True, transport=busy), frames
    engine.dispose()


def test_direct_and_managed_writers_share_worker_lock_before_any_change(tmp_path, writer_parts):
    sessions, client, frames = writer_parts
    path = tmp_path / "direct.txt"
    path.write_text("DOMAIN,example.org,DIRECT\n", encoding="utf-8")
    direct = RuleService(path, tmp_path / "revisions", SimpleNamespace(), maintenance_client=client)
    managed = ManagedRuleService(tmp_path, SimpleNamespace(), sessions, maintenance_client=client)
    operations = (
        lambda: direct.apply_direct_rules_async("DOMAIN,example.com,DIRECT\n", "owner"),
        lambda: managed.ensure_default_openai_fallback(),
        lambda: managed.create(kind="GEOSITE", category="openai", action="DIRECT", enabled=True),
        lambda: managed.update(1, kind="GEOSITE", category="openai", action="DIRECT", enabled=True),
        lambda: managed.delete(1),
    )
    async def check():
        for operation in operations:
            with pytest.raises(MaintenanceBusy):
                await operation()
    asyncio.run(check())
    with pytest.raises(MaintenanceBusy):
        direct.apply_direct_rules("DOMAIN,example.com,DIRECT\n", "owner")
    assert path.read_text(encoding="utf-8") == "DOMAIN,example.org,DIRECT\n"
    assert not (tmp_path / "revisions").exists()
    with sessions() as session:
        assert session.scalar(select(ManagedRulePolicy)) is None
    assert len(frames) == 6 and all(f["params"]["resource"] == "rules" for f in frames)


@pytest.mark.parametrize("method,args", [("create_client", ("pc",)), ("disable_client", (1,)), ("enable_client", (1,)),
    ("rename_client", (1, "name")), ("delete_client", (1,)), ("configure_credentials", ("user", "password"))])
def test_every_wg_write_is_guarded_before_http(writer_parts, method, args):
    _, client, frames = writer_parts
    def unexpected(_request):
        pytest.fail("no upstream request is allowed when maintenance is busy")
    settings = Settings(dashboard_encryption_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
    adapter = WgEasyAdapter(settings, transport=httpx.MockTransport(unexpected), maintenance_client=client)
    async def check():
        with pytest.raises(MaintenanceBusy):
            await getattr(adapter, method)(*args)
    asyncio.run(check())
    assert frames[0]["method"] == "writer_acquire"


@pytest.mark.parametrize("method,args", [("manual_geo_upgrade", ("owner",)), ("scheduled_geo_upgrade", ()), ("catch_up_geo_upgrade", ())])
def test_manual_scheduled_and_catchup_geodata_share_guard(tmp_path, writer_parts, method, args):
    sessions, client, frames = writer_parts
    collector = Collector(sessions, wgeasy=None, mihomo=None, service_probe=None, geodata_dir=tmp_path, maintenance_client=client)
    async def check():
        with pytest.raises(MaintenanceBusy):
            await getattr(collector, method)(*args)
    asyncio.run(check())
    with sessions() as session:
        assert session.scalar(select(GeoUpdate)) is None
    assert frames[0]["params"]["resource"] == "geodata"


def test_cancelled_lock_wait_does_not_acquire_and_orphan_the_lock():
    from app import maintenance_client as m
    assert hasattr(m, "acquire_thread_lock"), "cancellable local lock wait is not implemented"
    lock = threading.Lock()
    lock.acquire()
    async def check():
        waiter = asyncio.create_task(m.acquire_thread_lock(lock))
        await asyncio.sleep(0.01)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        lock.release()
        await asyncio.sleep(0.06)
        assert lock.acquire(blocking=False)
        lock.release()
    asyncio.run(check())


def test_validation_failure_releases_clean_but_partial_write_requires_reconciliation(tmp_path):
    from app.rules import DirectRuleValidationError
    frames = []
    async def transport(frame):
        frames.append(frame)
        return {"ok": True, "result": {"lease_id": JOB} if frame["method"] == "writer_acquire" else {"released": True}}
    client = MaintenanceClient(enabled=True, transport=transport)
    async def failed_reload():
        raise TimeoutError()
    service = RuleService(tmp_path / "direct.txt", tmp_path / "revisions", SimpleNamespace(reload=failed_reload), maintenance_client=client)
    async def check():
        with pytest.raises(DirectRuleValidationError):
            await service.apply_direct_rules_async("MATCH,DIRECT\n", "owner")
        assert frames[-1]["params"]["outcome"] == "complete"
        with pytest.raises(TimeoutError):
            await service.apply_direct_rules_async("DOMAIN,example.org,DIRECT\n", "owner")
        assert frames[-1]["params"]["outcome"] == "uncertain"
    asyncio.run(check())
