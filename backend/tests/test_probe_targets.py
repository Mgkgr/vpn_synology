from __future__ import annotations

import ipaddress

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.probe_targets import APPROVED_PROBE_TARGETS, ProbeTargetService, ProbeTargetValidationError


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'dashboard.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def public_resolver(_hostname: str) -> tuple[ipaddress.IPv4Address, ...]:
    return (ipaddress.ip_address("93.184.216.34"),)


def test_russian_services_are_available_as_opt_in_probe_targets(session_factory) -> None:
    service = ProbeTargetService(session_factory, resolver=public_resolver)

    targets = {target.key: target for target in service.list_targets()}

    assert [item.position for item in APPROVED_PROBE_TARGETS] == sorted(item.position for item in APPROVED_PROBE_TARGETS)
    assert targets["ozon"].url == "https://www.ozon.ru/"
    assert targets["wildberries"].url == "https://www.wildberries.ru/"
    assert targets["sber"].url == "https://www.sberbank.ru/"
    assert targets["tbank"].url == "https://www.tbank.ru/"
    assert targets["gosuslugi"].url == "https://www.gosuslugi.ru/"
    assert all(not targets[key].enabled for key in ("ozon", "wildberries", "sber", "tbank", "gosuslugi"))


def test_creates_and_updates_a_custom_public_https_probe_target(session_factory) -> None:
    service = ProbeTargetService(session_factory, resolver=public_resolver)

    created = service.create_custom_target(
        label="Мой сайт",
        url="https://Example.COM/health",
    )

    assert created.key.startswith("custom:")
    assert created.is_custom is True
    assert created.url == "https://example.com/health"
    assert created.enabled is True

    updated = service.update_custom_target(
        created.key,
        label="Главная проверка",
        url="https://example.com/ready",
        enabled=False,
    )

    assert updated.label == "Главная проверка"
    assert updated.url == "https://example.com/ready"
    assert updated.enabled is False
    assert updated.key not in {target.key for target in service.list_targets() if target.enabled}


@pytest.mark.parametrize(
    ("url", "resolver"),
    [
        ("http://example.com/", public_resolver),
        ("https://example.com:8443/", public_resolver),
        ("https://user:password@example.com/", public_resolver),
        ("https://127.0.0.1/", public_resolver),
        (
            "https://example.com/",
            lambda _hostname: (ipaddress.ip_address("192.168.2.1"),),
        ),
    ],
)
def test_rejects_probe_target_that_can_reach_non_public_or_unexpected_address(
    session_factory, url, resolver
) -> None:
    service = ProbeTargetService(session_factory, resolver=resolver)

    with pytest.raises(ProbeTargetValidationError):
        service.create_custom_target(label="Unsafe", url=url)
