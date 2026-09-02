from datetime import UTC, datetime

from sqlalchemy import select

from app.db import create_all, create_session_factory, create_sqlite_engine
from app.models import ManagedRulePolicy, ProbeEvent, RouteEvent


def test_create_all_migrates_legacy_hy2_references_to_usa(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "dashboard.sqlite3")
    sessions = create_session_factory(engine)
    create_all(engine)
    now = datetime.now(UTC)

    with sessions.begin() as session:
        session.add(
            ManagedRulePolicy(
                kind="GEOSITE",
                category="openai",
                action="HY2-NL",
                enabled=True,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            ProbeEvent(
                observed_at=now,
                target="HY2-NL",
                succeeded=True,
                latency_ms=12,
                endpoint="https://cp.cloudflare.com/generate_204",
                outbound="HY2-NL",
                status="ok",
                status_code=200,
                error_text=None,
                detail=None,
            )
        )
        session.add(
            RouteEvent(
                observed_at=now,
                route="VPS-FALLBACK",
                action="selected_outbound_changed",
                previous_outbound="HY2-NL",
                new_outbound="HY2-NL",
                detail=None,
            )
        )

    create_all(engine)

    with sessions() as session:
        policy = session.scalar(select(ManagedRulePolicy))
        probe = session.scalar(select(ProbeEvent))
        route = session.scalar(select(RouteEvent))

    assert policy is not None and policy.action == "HY2-USA"
    assert probe is not None and (probe.target, probe.outbound) == ("HY2-USA", "HY2-USA")
    assert route is not None and (route.previous_outbound, route.new_outbound) == ("HY2-USA", "HY2-USA")
