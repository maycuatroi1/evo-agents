"""Quota pacing, plan token-and-quota-economy, criterion a2: the strategy ``quota-pacing`` of
``evo_agents.hub.accounts.pick``, set on a member's policy for a project and on each account pool of a project.

``pick`` counts a window not reported, or whose reset has passed, as unused; it passes over an account whose five-hour
window is at ``five_hour_ceiling_pct`` or over while a cooler one exists, keeps the ``hard_reserve_pct`` of a weekly
window back while another account has more, and takes the account with the most of its weekly window left per hour
until it resets. Through the hub: a member's policy and an account pool take the strategy and its two thresholds (an
admin sets a pool's, a writer may not), each change audited, and a run of an agent whose pool paces takes the account
the rules name, env accounts and provider accounts through the gateway alike (``gateway_accounts.paced_accounts``).
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from evo_agents.hub.accounts import DEFAULT_POLICY, QUOTA_PACING, Candidate, Pacing, Window, pick, window_of
from tests.hub import pg

needs_db = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
if pg.DSN:
    from sqlalchemy import func, select, update
    from sqlalchemy.dialects.postgresql import insert
    from sqlalchemy.ext.asyncio import create_async_engine

    from evo_agents.hub import tables
    from tests.hub import test_credentials_api as credentials_api
    from tests.hub.live import sql
    from tests.hub.test_accounts_api import consecutive, set_policy, three
    from tests.hub.test_credentials_api import POOL, PROJECT, audit_rows, worker_of

    app_key, config, hub, client = (
        credentials_api.app_key,
        credentials_api.config,
        credentials_api.hub,
        credentials_api.client,
    )

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def week(used: float, hours: float) -> Window:
    return Window(used, NOW + timedelta(hours=hours))


def chosen(candidates, pacing: Pacing | None = None) -> str:
    found = pick(candidates, QUOTA_PACING, pacing, NOW)
    assert found is not None
    return found[0].name


# The pick itself


def test_quota_pacing_is_a_strategy_and_the_defaults_hold_its_thresholds():
    assert DEFAULT_POLICY["hard_reserve_pct"] == 10 and DEFAULT_POLICY["five_hour_ceiling_pct"] == 90
    assert pick([], QUOTA_PACING, now=NOW) is None


def test_a_window_not_reported_or_past_its_reset_counts_as_unused():
    fresh = Candidate(1, "fresh")  # nothing reported: a whole week left over a week
    stale = Candidate(2, "stale", seven_day=Window(0.95, NOW - timedelta(minutes=1)), five_hour=Window(1.0, NOW))
    worn = Candidate(3, "worn", seven_day=week(0.5, 24 * 6))
    assert chosen([fresh, worn]) == "fresh"
    # stale's week reset a minute ago and its five-hour window just now: both count as unused, as fresh's do
    assert chosen([stale, worn]) == "stale"
    assert chosen([fresh, stale]) in {"fresh", "stale"}


def test_an_account_over_the_five_hour_ceiling_waits_while_a_cooler_one_exists():
    hot = Candidate(1, "hot", five_hour=Window(0.9, NOW + timedelta(hours=2)), seven_day=week(0.0, 2))
    cool = Candidate(2, "cool", five_hour=Window(0.89, NOW + timedelta(hours=2)), seven_day=week(0.5, 24 * 6))
    assert chosen([hot, cool]) == "cool"  # hot's week resets soonest, but its five hours are at the ceiling
    assert chosen([hot, cool], Pacing(five_hour_ceiling_pct=95)) == "hot"
    hotter = Candidate(3, "hotter", five_hour=Window(0.99, NOW + timedelta(hours=1)), seven_day=week(0.0, 24 * 6))
    assert chosen([hot, hotter]) == "hot"  # all over it: the weekly pace decides


def test_the_hard_reserve_is_kept_while_another_account_has_more():
    low = Candidate(1, "low", seven_day=week(0.92, 1))  # 8% left within an hour: the best pace by far
    high = Candidate(2, "high", seven_day=week(0.3, 24 * 5))
    assert chosen([low, high]) == "high"
    assert chosen([low, high], Pacing(hard_reserve_pct=5)) == "low"
    assert chosen([low]) == "low"  # no other account: the reserve is spent


def test_the_weekly_quota_that_resets_soonest_per_hour_is_spent_first():
    soon = Candidate(1, "soon", seven_day=week(0.6, 10))  # 40% over 10 hours
    later = Candidate(2, "later", seven_day=week(0.1, 24 * 6))  # 90% over 144 hours
    blank = Candidate(3, "blank")  # 100% over a week
    assert chosen([later, blank, soon]) == "soon"
    assert chosen([later, blank]) == "later"


def test_equal_accounts_take_turns_by_rotation():
    a = Candidate(1, "a", rotation=0.0)
    b = Candidate(2, "b", rotation=0.0)
    first, rotation = pick([a, b], QUOTA_PACING, now=NOW)
    assert first.name == "a"
    assert chosen([Candidate(1, "a", rotation=rotation), b]) == "b"


def test_a_rejected_usage_counts_its_window_full():
    usage = {"status": "rejected", "rate_limit_type": "seven_day", "resets_at": (NOW + timedelta(days=2)).isoformat()}
    window = window_of(usage, "seven_day")
    assert window is not None and window.used == 1.0 and window.resets_at == NOW + timedelta(days=2)
    assert window_of({"status": "allowed"}, "seven_day") is None


# Through the hub

POOLS = f"/v1/projects/{PROJECT}/account-pools" if pg.DSN else ""


def secret_id(db, name: str) -> int:
    s = tables.secrets
    return sql(db, select(s.c.id).where(s.c.name == name, s.c.deleted_at.is_(None)))[0][0]


def report_usage(db, name: str, **windows: tuple[float, float]) -> None:
    """Account ``name`` reported ``windows``: each a share used and the hours until it resets."""
    usage = {
        "status": "allowed",
        "windows": {
            window: {"utilization": used, "resets_at": (datetime.now(UTC) + timedelta(hours=hours)).isoformat()}
            for window, (used, hours) in windows.items()
        },
    }
    a = tables.accounts
    values = {"last_usage": usage, "last_usage_at": func.now()}
    statement = insert(a).values(secret_id=secret_id(db, name), **values)
    sql(db, statement.on_conflict_do_update(index_elements=[a.c.secret_id], set_=values))


@needs_db
def test_a_members_policy_takes_quota_pacing_and_its_thresholds(client, hub, hub_db):
    found = client.get(f"/v1/account-policies/{PROJECT}", headers=hub["owner"]).json()
    assert (found["hard_reserve_pct"], found["five_hour_ceiling_pct"]) == (10, 90)
    policy = set_policy(client, hub["owner"], strategy=QUOTA_PACING, hard_reserve_pct=15, five_hour_ceiling_pct=80)
    assert (policy["strategy"], policy["hard_reserve_pct"], policy["five_hour_ceiling_pct"]) == (QUOTA_PACING, 15, 80)
    for wrong in ({"hard_reserve_pct": 101}, {"five_hour_ceiling_pct": 0}, {"strategy": "fastest"}):
        refused = client.put(f"/v1/account-policies/{PROJECT}", json=wrong, headers=hub["owner"])
        assert refused.status_code == 422, refused.text
    (target, login, project, _), *_ = audit_rows(hub_db, "account.policy-put")
    assert login == "owner" and project == PROJECT
    assert "strategy=quota-pacing" in target and "hard_reserve_pct=15 five_hour_ceiling_pct=80" in target


@needs_db
def test_an_admin_sets_the_policy_of_an_account_pool_and_a_writer_may_not(client, hub, hub_db):
    made = client.post(POOLS, json={"name": "paced", "strategy": QUOTA_PACING}, headers=hub["admin"])
    assert made.status_code == 201, made.text
    pool = made.json()
    assert (pool["strategy"], pool["hard_reserve_pct"], pool["five_hour_ceiling_pct"]) == (QUOTA_PACING, 10, 90)
    plain = client.post(POOLS, json={"name": "plain"}, headers=hub["admin"]).json()
    assert plain["strategy"] == "round-robin"
    refused = client.patch(f"{POOLS}/paced", json={"strategy": "fill-first"}, headers=hub["owner"])
    assert refused.status_code == 403
    assert client.patch(f"{POOLS}/paced", json={"hard_reserve_pct": 101}, headers=hub["admin"]).status_code == 422
    changed = client.patch(f"{POOLS}/paced", json={"five_hour_ceiling_pct": 70}, headers=hub["admin"])
    assert changed.status_code == 200, changed.text
    assert (changed.json()["strategy"], changed.json()["five_hour_ceiling_pct"]) == (QUOTA_PACING, 70)
    targets = [row[0] for row in audit_rows(hub_db, "account-pool.policy")]
    assert targets == [
        f"{PROJECT} account-pool:{pool['id']} strategy=quota-pacing hard_reserve_pct=10 five_hour_ceiling_pct=70"
    ]


@needs_db
def test_the_runs_of_a_pacing_pool_take_the_account_the_rules_name(client, hub, hub_db):
    """The pool's strategy, not DEFAULT_POLICY's round-robin: claude-c's week resets soonest, claude-a's five hours
    are at the ceiling, claude-b keeps its reserve; once claude-c's week is spent down to the reserve, claude-b."""
    three(client, hub["owner"])
    changed = client.patch(f"{POOLS}/{POOL}", json={"strategy": QUOTA_PACING}, headers=hub["admin"])
    assert changed.status_code == 200, changed.text
    report_usage(hub_db, "claude-a", five_hour=(0.95, 2), seven_day=(0.0, 3))
    report_usage(hub_db, "claude-b", seven_day=(0.2, 24 * 6))
    report_usage(hub_db, "claude-c", five_hour=(0.3, 2), seven_day=(0.5, 24))
    worker = worker_of(client, hub["owner"], "mac-mini")
    assert consecutive(client, hub["owner"], worker, 3) == ["claude-c"] * 3
    report_usage(hub_db, "claude-c", seven_day=(0.95, 24))
    assert consecutive(client, hub["owner"], worker, 2) == ["claude-b"] * 2


@needs_db
def test_the_gateway_narrows_a_runs_provider_accounts_to_the_one_quota_pacing_names(client, hub, hub_db):
    """``gateway_accounts.paced_accounts`` on a run of the member's own, under the member's policy: all accounts
    under another strategy, the best paced alone under quota-pacing, a cooling one never."""
    from evo_agents.hub.server.gateway_accounts import Account, Asker, paced_accounts
    from tests.hub.test_credentials_api import env_secret, sample

    for name in ("zai-a", "zai-b"):
        env_secret(client, hub["owner"], name, "ZAI_API_KEY", sample(f"{name}-"))
    report_usage(hub_db, "zai-a", seven_day=(0.1, 24 * 6))
    report_usage(hub_db, "zai-b", seven_day=(0.3, 12))
    u, p = tables.users, tables.projects
    owner = sql(hub_db, select(u.c.id).where(u.c.login == "owner"))[0][0]
    project = sql(hub_db, select(p.c.id).where(p.c.name == PROJECT))[0][0]
    asker = Asker(id=0, owner_id=owner, principal=owner, project_id=project)
    accounts = [Account(secret_id(hub_db, name), name) for name in ("zai-a", "zai-b")]

    def narrowed() -> list[str]:
        async def go():
            engine = create_async_engine(hub_db.dsn.replace("postgresql://", "postgresql+psycopg://", 1))
            try:
                async with engine.connect() as conn:
                    return [account.name for account in await paced_accounts(conn, asker, accounts)]
            finally:
                await engine.dispose()

        return asyncio.run(go())

    assert narrowed() == ["zai-a", "zai-b"]
    set_policy(client, hub["owner"], strategy=QUOTA_PACING)
    assert narrowed() == ["zai-b"]
    a = tables.accounts
    soon = datetime.now(UTC) + timedelta(minutes=30)
    sql(hub_db, update(a).where(a.c.secret_id == accounts[1].secret_id).values(cooldown_until=soon))
    assert narrowed() == ["zai-a"]
