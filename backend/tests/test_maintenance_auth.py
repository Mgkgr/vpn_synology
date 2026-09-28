import dataclasses
import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.auth import AuthService
from app.db import create_all, create_session_factory, create_sqlite_engine

PASSWORD = "correct horse battery staple"
HASH = "a" * 64
JOB = "b" * 32


@pytest.fixture
def auth_setup(tmp_path):
    assert (Path(__file__).resolve().parents[1] / "app/maintenance_auth.py").is_file(), "owner step-up is not implemented"
    module = importlib.import_module("app.maintenance_auth")
    engine = create_sqlite_engine(tmp_path / "auth.sqlite3")
    create_all(engine)
    sessions = create_session_factory(engine)
    auth = AuthService(sessions)
    owner = auth.authenticate(auth.bootstrap("owner", PASSWORD).token)
    auth.create_administrator("owner", "second-admin", PASSWORD)
    admin = auth.authenticate(auth.login("second-admin", PASSWORD).token)
    yield module, sessions, auth, owner, admin
    engine.dispose()


def test_shared_owner_id_does_not_grant_owner_privileges(auth_setup):
    m, sessions, auth, owner, admin = auth_setup
    assert owner.owner_id == admin.owner_id
    assert auth.verify_owner_password(owner, PASSWORD)
    assert not auth.verify_owner_password(admin, PASSWORD)
    grants = m.MaintenanceGrantService(sessions, auth)
    with pytest.raises(m.MaintenanceAuthorizationError):
        grants.issue(admin, HASH, PASSWORD, datetime.now(UTC))
    with pytest.raises(m.MaintenanceAuthorizationError):
        grants.issue(owner, HASH, "wrong", datetime.now(UTC))


def test_grant_is_hashed_session_bound_and_expires_at_300_seconds(auth_setup):
    m, sessions, auth, owner, _ = auth_setup
    grants = m.MaintenanceGrantService(sessions, auth)
    now = datetime.now(UTC)
    token = grants.issue(owner, HASH, PASSWORD, now)
    with sessions() as session:
        row = session.scalar(select(m.MaintenanceGrant))
        assert row.token_hash != token and len(row.token_hash) == 64
        assert row.expires_at == now + timedelta(seconds=300)
    another = auth.authenticate(auth.login("owner", PASSWORD).token)
    for principal, digest, at in ((another, HASH, now), (owner, "c" * 64, now), (owner, HASH, now + timedelta(seconds=300))):
        with pytest.raises(m.MaintenanceAuthorizationError):
            grants.consume(token, principal, digest, JOB, at)
    assert grants.consume(token, owner, HASH, JOB, now + timedelta(seconds=299)) is True
    assert grants.consume(token, owner, HASH, JOB, now + timedelta(seconds=301)) is False
    with pytest.raises(m.MaintenanceAuthorizationError):
        grants.consume(token, owner, HASH, "c" * 32, now + timedelta(seconds=301))


def test_logout_invalidates_grant_and_principal_cannot_be_forged(auth_setup):
    m, sessions, auth, owner, admin = auth_setup
    grants = m.MaintenanceGrantService(sessions, auth)
    now = datetime.now(UTC)
    forged = dataclasses.replace(admin, username="owner")
    assert not auth.verify_owner_password(forged, PASSWORD)
    token = grants.issue(owner, HASH, PASSWORD, now)
    auth.logout(owner)
    with pytest.raises(m.MaintenanceAuthorizationError):
        grants.consume(token, owner, HASH, JOB, now)


def test_five_failed_stepups_block_session_for_15_minutes(auth_setup):
    m, sessions, auth, owner, _ = auth_setup
    grants = m.MaintenanceGrantService(sessions, auth)
    now = datetime.now(UTC)
    for number in range(5):
        with pytest.raises(m.MaintenanceAuthorizationError):
            grants.issue(owner, HASH, "wrong", now + timedelta(seconds=number))
    with pytest.raises(m.MaintenanceStepUpThrottled):
        grants.issue(owner, HASH, PASSWORD, now + timedelta(seconds=10))
    assert grants.issue(owner, HASH, PASSWORD, now + timedelta(seconds=905))


def test_grant_consumption_is_atomic_under_double_click(auth_setup):
    from concurrent.futures import ThreadPoolExecutor
    m, sessions, auth, owner, _ = auth_setup
    grants = m.MaintenanceGrantService(sessions, auth)
    now = datetime.now(UTC)
    token = grants.issue(owner, HASH, PASSWORD, now)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: grants.consume(token, owner, HASH, JOB, now), range(2)))
    assert sorted(results) == [False, True]


def test_two_authorizations_cannot_submit_the_same_id_twice(auth_setup):
    from concurrent.futures import ThreadPoolExecutor
    m, sessions, auth, owner, _ = auth_setup
    grants = m.MaintenanceGrantService(sessions, auth)
    now = datetime.now(UTC)
    with ThreadPoolExecutor(max_workers=2) as pool:
        tokens = list(pool.map(lambda _: grants.issue(owner, HASH, PASSWORD, now), range(2)))
        claims = list(pool.map(lambda token: grants.consume(token, owner, HASH, JOB, now), tokens))
    assert sorted(claims) == [False, True]
    with sessions() as session:
        assert len(session.scalars(select(m.MaintenanceSubmitIntent)).all()) == 1
