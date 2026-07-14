from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.db import create_all, create_session_factory, create_sqlite_engine
from app.models import AuditEvent
from app.rules import DirectRuleValidationError, RuleService


class ReloadingMihomo:
    def __init__(self, revision_dir, *, fail: bool = False) -> None:
        self.revision_dir = revision_dir
        self.fail = fail
        self.calls = 0

    def reload(self) -> None:
        self.calls += 1
        assert list(self.revision_dir.glob("*.txt")), "revision must exist before reload"
        if self.fail:
            raise RuntimeError("controller reload failed")


@pytest.fixture
def rule_paths(tmp_path):
    direct_rules_path = tmp_path / "gateway" / "rules" / "direct.txt"
    direct_rules_path.parent.mkdir(parents=True)
    direct_rules_path.write_text("DOMAIN,old.example,DIRECT\n", encoding="utf-8")
    return direct_rules_path, tmp_path / "data" / "rule-revisions"


@pytest.fixture
def session_factory(tmp_path):
    engine = create_sqlite_engine(tmp_path / "dashboard.sqlite3")
    create_all(engine)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()


def test_rejects_an_invalid_direct_rule(rule_paths) -> None:
    direct_rules_path, revision_dir = rule_paths
    service = RuleService(direct_rules_path, revision_dir, ReloadingMihomo(revision_dir))

    with pytest.raises(DirectRuleValidationError):
        service.preview_direct_rules("NOT-A-RULE bad domain")


@pytest.mark.parametrize(
    "rule",
    [
        "DOMAIN-SUFFIX,bad domain,DIRECT",
        "IP-CIDR,not-a-network,DIRECT",
        "DOMAIN,example.org,WG-IMP",
        "MATCH,DIRECT",
        "DOMAIN,example.org,DIRECT,no-resolve",
    ],
)
def test_rejects_non_allowlisted_or_nonterminal_direct_forms(rule_paths, rule: str) -> None:
    direct_rules_path, revision_dir = rule_paths
    service = RuleService(direct_rules_path, revision_dir, ReloadingMihomo(revision_dir))

    with pytest.raises(DirectRuleValidationError):
        service.preview_direct_rules(rule)


def test_preview_accepts_comments_blanks_and_validated_direct_rules(rule_paths) -> None:
    direct_rules_path, revision_dir = rule_paths
    service = RuleService(direct_rules_path, revision_dir, ReloadingMihomo(revision_dir))

    preview = service.preview_direct_rules(
        "# local exceptions\n\nDOMAIN,example.org,DIRECT\nIP-CIDR,192.0.2.0/24,DIRECT\nGEOIP,private,DIRECT\n"
    )

    assert preview.rule_count == 3
    assert preview.rules == (
        "DOMAIN,example.org,DIRECT",
        "IP-CIDR,192.0.2.0/24,DIRECT",
        "GEOIP,private,DIRECT",
    )


def test_apply_creates_revision_before_mihomo_reload(rule_paths, session_factory) -> None:
    direct_rules_path, revision_dir = rule_paths
    mihomo = ReloadingMihomo(revision_dir)
    service = RuleService(
        direct_rules_path,
        revision_dir,
        mihomo,
        audit_session_factory=session_factory,
        now=lambda: datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
    )
    text = "DOMAIN-SUFFIX,example.org,DIRECT\n"

    revision = service.apply_direct_rules(text, actor="admin")

    assert revision.number == 1
    assert revision.path.read_text(encoding="utf-8") == text
    assert direct_rules_path.read_text(encoding="utf-8") == text
    assert mihomo.calls == 1
    with session_factory() as session:
        audit = session.scalars(select(AuditEvent)).one()
    assert audit.action == "direct_rules_apply"
    assert audit.actor == "admin"
    assert audit.revision_number == 1
    assert audit.succeeded is True
    assert audit.error_text is None
    assert audit.detail is None


def test_apply_restores_the_previous_file_when_reload_fails(rule_paths, session_factory) -> None:
    direct_rules_path, revision_dir = rule_paths
    previous = direct_rules_path.read_text(encoding="utf-8")
    mihomo = ReloadingMihomo(revision_dir, fail=True)
    service = RuleService(direct_rules_path, revision_dir, mihomo, audit_session_factory=session_factory)

    with pytest.raises(RuntimeError, match="controller reload failed"):
        service.apply_direct_rules("DOMAIN,new.example,DIRECT\n", actor="admin")

    assert direct_rules_path.read_text(encoding="utf-8") == previous
    assert [path.read_text(encoding="utf-8") for path in revision_dir.glob("*.txt")] == [
        "DOMAIN,new.example,DIRECT\n"
    ]
    with session_factory() as session:
        audit = session.scalars(select(AuditEvent)).one()
    assert audit.action == "direct_rules_apply_failed"
    assert audit.revision_number == 1
    assert audit.succeeded is False
    assert audit.error_text == "Mihomo reload failed"
    assert audit.detail is None


def test_async_applications_for_one_rules_file_do_not_overlap(rule_paths) -> None:
    direct_rules_path, revision_dir = rule_paths

    class BlockingMihomo:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.first_entered = asyncio.Event()
            self.release_first = asyncio.Event()

        async def reload(self) -> None:
            self.calls.append(direct_rules_path.read_text(encoding="utf-8"))
            if len(self.calls) == 1:
                self.first_entered.set()
                await self.release_first.wait()

    async def apply_concurrently() -> tuple[RuleService, BlockingMihomo]:
        mihomo = BlockingMihomo()
        service = RuleService(direct_rules_path, revision_dir, mihomo)
        first = asyncio.create_task(service.apply_direct_rules_async("DOMAIN,first.example,DIRECT\n", "admin"))
        await mihomo.first_entered.wait()
        second = asyncio.create_task(service.apply_direct_rules_async("DOMAIN,second.example,DIRECT\n", "admin"))
        await asyncio.sleep(0)
        assert mihomo.calls == ["DOMAIN,first.example,DIRECT\n"]
        mihomo.release_first.set()
        await asyncio.gather(first, second)
        return service, mihomo

    _service, mihomo = asyncio.run(apply_concurrently())

    assert mihomo.calls == ["DOMAIN,first.example,DIRECT\n", "DOMAIN,second.example,DIRECT\n"]
    assert direct_rules_path.read_text(encoding="utf-8") == "DOMAIN,second.example,DIRECT\n"


def test_failed_reload_never_replaces_a_later_direct_rules_write(rule_paths, session_factory) -> None:
    direct_rules_path, revision_dir = rule_paths
    later_content = "DOMAIN,later.example,DIRECT\n"

    class ExternalWriteThenFailMihomo:
        def reload(self) -> None:
            direct_rules_path.write_text(later_content, encoding="utf-8")
            raise RuntimeError("controller reload failed")

    service = RuleService(
        direct_rules_path,
        revision_dir,
        ExternalWriteThenFailMihomo(),
        audit_session_factory=session_factory,
    )

    with pytest.raises(RuntimeError, match="controller reload failed"):
        service.apply_direct_rules("DOMAIN,new.example,DIRECT\n", actor="admin")

    assert direct_rules_path.read_text(encoding="utf-8") == later_content
    with session_factory() as session:
        audit = session.scalars(select(AuditEvent)).one()
    assert audit.restored is False
