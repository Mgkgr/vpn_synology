import asyncio
import importlib
from pathlib import Path

import pytest

JOB = "a" * 32


def module():
    assert (Path(__file__).resolve().parents[1] / "app/maintenance_client.py").is_file(), "maintenance client is not implemented"
    return importlib.import_module("app.maintenance_client")


def test_lost_submit_response_does_not_automatically_resubmit():
    m = module()
    calls = []
    async def transport(frame):
        calls.append(frame)
        raise TimeoutError()
    client = m.MaintenanceClient(enabled=True, transport=transport)
    async def check():
        with pytest.raises(m.MaintenanceUnavailable):
            await client.submit(JOB, {"action": "restart"}, "admin")
    asyncio.run(check())
    assert len(calls) == 1


def test_write_guard_disallows_body_if_busy_and_disabled_is_noop():
    m = module()
    async def transport(frame):
        return {"ok": False, "error": "busy", "job_id": JOB}
    async def check():
        client = m.MaintenanceClient(enabled=True, transport=transport)
        with pytest.raises(m.MaintenanceBusy):
            async with client.write_guard("clients"):
                pytest.fail("writer must not run when maintenance has the lock")
        client = m.MaintenanceClient(enabled=False, transport=transport)
        async with client.write_guard("clients"):
            pass
    asyncio.run(check())


def test_lost_lease_cancels_writer_without_claiming_clean_release():
    m = module()
    frames = []
    async def transport(frame):
        frames.append(frame)
        if frame["method"] == "writer_acquire":
            return {"ok": True, "result": {"lease_id": JOB}}
        raise TimeoutError()
    async def check():
        client = m.MaintenanceClient(enabled=True, transport=transport, renew_seconds=0.01)
        with pytest.raises(m.MaintenanceUnavailable):
            async with client.write_guard("clients"):
                await asyncio.sleep(1)
        assert not any(item["method"] == "writer_release" and item["params"].get("outcome") == "complete" for item in frames)
    asyncio.run(check())


def test_release_requiring_reconciliation_is_not_reported_clean():
    m = module()
    async def transport(frame):
        return {"ok": True, "result": {"lease_id": JOB} if frame["method"] == "writer_acquire" else {"released": False}}
    async def check():
        client = m.MaintenanceClient(enabled=True, transport=transport)
        with pytest.raises(m.MaintenanceUnavailable):
            async with client.write_guard("rules"):
                pass
    asyncio.run(check())


def test_writer_cannot_swallow_lease_loss_and_report_success():
    m = module()
    async def transport(frame):
        if frame["method"] == "writer_acquire":
            return {"ok": True, "result": {"lease_id": JOB}}
        raise TimeoutError()
    async def check():
        client = m.MaintenanceClient(enabled=True, transport=transport, renew_seconds=0.01)
        with pytest.raises(m.MaintenanceUnavailable):
            async with client.write_guard("geodata"):
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    pass
    asyncio.run(check())
