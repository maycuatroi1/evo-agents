"""Leases under a credential share (``evo_agents.hub.server.share_leases``), as criteria a3 and a4 of plan
credential-sharing name them: a run of a member a share covers (by login, by a unit holding their primary seat, or
all) on a project of the share is leased the shared secret after the member's own; a share that is past its usage
ceiling, its most running runs or its budget, or whose account its owner's runs need, lends nothing and the run's
events say why naming the share by id alone; revoking or ending a share takes back the leases out under it; each lease
records its principal, and an account's cost splits by principal, which its owner sees whole and a grantee for
themselves alone. Schema 0105 goes up and down.

Everything but the first tests needs the hub and skips without EVO_HUB_TEST_DSN."""

import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from evo_agents.hub.server.share_leases import ESTIMATE_USD, MIN_ESTIMATE_USD, estimate, peak_usage
from tests.hub import pg

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


# Without the hub


def test_credential_share_usage_peak_skips_the_windows_whose_reset_passed_and_unknown_usage_is_none():
    later, earlier = (NOW + timedelta(hours=1)).isoformat(), (NOW - timedelta(hours=1)).isoformat()
    usage = {
        "status": "allowed",
        "utilization": 40,
        "resets_at": later,
        "windows": {"five_hour": {"utilization": 97, "resets_at": earlier}, "seven_day": {"utilization": 62}},
    }
    assert peak_usage(usage, NOW) == 62
    assert (
        peak_usage({"status": "allowed", "windows": {"five_hour": {"utilization": 90, "resets_at": later}}}, NOW) == 90
    )
    assert peak_usage(None, NOW) is None
    assert peak_usage({"status": "allowed"}, NOW) is None
    assert peak_usage({"utilization": True, "windows": "?"}, NOW) is None


def test_credential_share_budget_estimate_is_the_mean_cost_else_the_default_and_never_zero():
    assert estimate(Decimal("9"), 3) == Decimal("3")
    assert estimate(Decimal("0"), 0) == ESTIMATE_USD
    assert estimate(Decimal("0"), 4) == MIN_ESTIMATE_USD
    assert estimate(Decimal("0.001"), 1) == MIN_ESTIMATE_USD


if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import delete, func, insert, select, update  # noqa: E402

from evo_agents.hub import tables  # noqa: E402
from evo_agents.hub.migrate import migrate  # noqa: E402
from tests.hub import test_credentials_api as credentials_api  # noqa: E402
from tests.hub.live import sql  # noqa: E402
from tests.hub.test_accounts_api import cool, events, taken_how  # noqa: E402
from tests.hub.test_credentials_api import (  # noqa: E402
    GITLAB_KB,
    OTHER,
    OWNER,
    PLAN,
    PROJECT,
    STEPS,
    by_name,
    env_secret,
    git_secret,
    leased,
    reap,
    registration,
    worker_of,
)
from tests.hub.test_migrate import ALL, INFO_COLUMNS, move_to  # noqa: E402
from tests.hub.test_runs import claim  # noqa: E402

# the hub and its client of test_credentials_api, as fixtures of this module
app_key, config, hub, client = (
    credentials_api.app_key,
    credentials_api.config,
    credentials_api.hub,
    credentials_api.client,
)

VAR = "ANTHROPIC_API_KEY"  # an API key: shared without the terms acknowledgement a subscription token needs
LAB = "lab"


def sample(prefix: str = "sk-lease-") -> str:
    return prefix + secrets.token_hex(16)


def share(client, headers, name: str, status: int = 201, **body) -> dict:
    body = {"grantee_kind": "user", "login": OTHER, "projects": [PROJECT], **body}
    if body["grantee_kind"] != "user":
        body.pop("login")
    response = client.post(f"/v1/secrets/{name}/shares", json=body, headers=headers)
    assert response.status_code == status, response.text
    return response.json()


def run_on(client, headers, worker: dict, repo: str) -> int:
    body = {"plan_id": PLAN, "steps": [STEPS[repo]], "worker_id": worker["id"]}
    response = client.post(f"/v1/projects/{PROJECT}/runs", json=body, headers=headers)
    assert response.status_code == 201, response.text
    run_id = response.json()[0]["id"]
    assert claim(client, worker)["id"] == run_id
    return run_id


def env_of(client, worker: dict, run_id: int) -> dict:
    """VAR's lease of the run: {name, value}; {} for none."""
    leases = [lease for lease in leased(client, worker, run_id)["leases"] if lease.get("env_var") == VAR]
    assert len(leases) <= 1
    return {"name": leases[0]["name"], "value": leases[0]["value"]} if leases else {}


def share_leases(db, run_id: int) -> list[tuple]:
    """(secret name, principal id, share id, revoked) of each secret lease of the run."""
    cl, s = tables.credential_leases, tables.secrets
    return sql(
        db,
        select(s.c.name, cl.c.principal_id, cl.c.share_id, cl.c.revoked_at.is_not(None))
        .join_from(cl, s, s.c.id == cl.c.secret_id)
        .where(cl.c.run_id == run_id)
        .order_by(cl.c.id),
    )


def user_id(db, login: str) -> int:
    return sql(db, select(tables.users.c.id).where(tables.users.c.login == login))[0][0]


def refusals(db, run_id: int) -> list[dict]:
    return [body for body in events(db, run_id) if "share_id" in body]


def set_share(db, share_id: int, **values) -> None:
    cs = tables.credential_shares
    sql(db, update(cs).where(cs.c.id == share_id).values(**values))


def set_usage(db, name: str, usage) -> None:
    (secret_id,) = sql(db, select(tables.secrets.c.id).where(tables.secrets.c.name == name))[0]
    a = tables.accounts
    sql(db, update(a).where(a.c.secret_id == secret_id).values(last_usage=usage, last_usage_at=func.now()))


def set_cost(db, run_id: int, usd: float) -> None:
    r = tables.runs
    sql(db, update(r).where(r.c.id == run_id).values(usage={"total_cost_usd": usd}))


def assert_named_by_id(body: dict, share_id: int, *hidden: str) -> None:
    assert body["share_id"] == share_id and f"share {share_id} " in body["text"]
    for word in hidden:
        assert word not in str(body)


@pytest.fixture
def shared(client, hub) -> dict:
    """owner's API key "team-key", shared with someone-else on the project; someone-else's worker desk."""
    value = sample()
    env_secret(client, hub["owner"], "team-key", VAR, value)
    found = share(client, hub["owner"], "team-key")
    return {"value": value, "share": found["id"], "desk": worker_of(client, hub["other"], "desk", slots=4)}


# Who is lent what (criterion a3)


def test_credential_share_lease_lends_the_shared_account_and_records_its_principal(client, hub, hub_db, shared):
    desk = shared["desk"]
    run_id = run_on(client, hub["other"], desk, "notes")
    assert env_of(client, desk, run_id) == {"name": "team-key", "value": shared["value"]}
    assert share_leases(hub_db, run_id) == [("team-key", user_id(hub_db, OTHER), shared["share"], False)]
    assert taken_how(hub_db, run_id) == [(VAR, "team-key", "shared")]
    # asked again: the same lease
    assert env_of(client, desk, run_id)["name"] == "team-key"
    assert len(share_leases(hub_db, run_id)) == 1
    # the owner's own runs record the owner and no share
    box = worker_of(client, hub["owner"], "box")
    own = run_on(client, hub["owner"], box, "lumen-kb-docs")
    assert env_of(client, box, own)["name"] == "team-key"
    assert share_leases(hub_db, own) == [("team-key", user_id(hub_db, OWNER), None, False)]


def test_credential_share_lease_priority_the_grantees_own_account_and_git_secret_come_first(
    client, hub, hub_db, shared
):
    desk = shared["desk"]
    first = run_on(client, hub["other"], desk, "notes")
    assert env_of(client, desk, first)["name"] == "team-key"
    mine = sample()
    env_secret(client, hub["other"], "my-key", VAR, mine)
    assert env_of(client, desk, first)["name"] == "team-key"  # a run keeps the account it took
    second = run_on(client, hub["other"], desk, "evo-agents")
    assert env_of(client, desk, second) == {"name": "my-key", "value": mine}
    # git: the shared secret covers an origin the grantee has nothing for, and gives way to theirs
    git_secret(client, hub["owner"], "team-gitlab", GITLAB_KB, sample("glpat-team-"))
    share(client, hub["owner"], "team-gitlab")
    kb = run_on(client, hub["other"], desk, "lumen-kb-docs")
    assert "team-gitlab" in by_name(leased(client, desk, kb))
    git_secret(client, hub["other"], "my-gitlab", GITLAB_KB, sample("glpat-mine-"))
    leases = by_name(leased(client, desk, kb))
    assert "my-gitlab" in leases and "team-gitlab" not in leases
    revoked = [row for row in share_leases(hub_db, kb) if row[0] == "team-gitlab"]
    assert revoked and all(row[3] for row in revoked)  # the shared lease it no longer needs is taken back


def test_credential_share_lease_covers_by_user_unit_or_all_on_the_shares_projects_until_it_ends(
    client, hub, hub_db, shared
):
    desk = shared["desk"]
    sid = shared["share"]
    set_share(hub_db, sid, grantee_user_id=user_id(hub_db, OWNER))  # a share for another member
    run_id = run_on(client, hub["other"], desk, "notes")
    assert env_of(client, desk, run_id) == {}
    # a unit holding the grantee's primary seat, or one above it
    unit = client.post(
        "/v1/org/units", json={"slug": LAB, "parent": "root", "kind": "team", "name": LAB}, headers=hub["admin"]
    )
    assert unit.status_code == 201, unit.text
    seat = {"login": OTHER, "unit": LAB, "role": "member", "primary": True}
    response = client.post("/v1/org/seats", json=seat, headers=hub["admin"])
    assert response.status_code in (200, 201), response.text
    root = sql(hub_db, select(tables.org_units.c.id).where(tables.org_units.c.parent_id.is_(None)))[0][0]
    set_share(hub_db, sid, grantee_kind="unit", grantee_user_id=None, grantee_unit_id=root)
    assert env_of(client, desk, run_id)["name"] == "team-key"
    set_share(hub_db, sid, grantee_kind="all", grantee_unit_id=None)
    assert env_of(client, desk, run_id)["name"] == "team-key"
    # another project's share lends nothing here, and the lease out under it is taken back at the next ask
    put = client.put("/v1/projects/elsewhere", json=registration(), headers=hub["admin"])
    assert put.status_code == 200, put.text
    elsewhere = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == "elsewhere"))[0][0]
    csp = tables.credential_share_projects
    sql(hub_db, insert(csp).values(share_id=sid, project_id=elsewhere))
    sql(hub_db, delete(csp).where(csp.c.share_id == sid, csp.c.project_id != elsewhere))
    assert env_of(client, desk, run_id) == {}
    assert [row[3] for row in share_leases(hub_db, run_id)] == [True]
    # nor a share past its end
    sql(
        hub_db,
        insert(csp).values(
            share_id=sid,
            project_id=sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0],
        ),
    )
    set_share(hub_db, sid, expires_at=func.now() - timedelta(minutes=1))
    assert env_of(client, desk, run_id) == {}


# When a share lends nothing (criterion a3)


def test_credential_share_usage_ceiling_counts_the_windows_not_reset_and_lends_unknown_usage(
    client, hub, hub_db, shared
):
    desk, sid = shared["desk"], shared["share"]
    set_share(hub_db, sid, usage_ceiling_pct=80)
    first = run_on(client, hub["other"], desk, "notes")
    assert env_of(client, desk, first)["name"] == "team-key"  # no usage known yet
    later = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    earlier = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    set_usage(
        hub_db, "team-key", {"status": "allowed", "windows": {"five_hour": {"utilization": 91, "resets_at": later}}}
    )
    second = run_on(client, hub["other"], desk, "evo-agents")
    assert env_of(client, desk, second) == {}
    [why] = refusals(hub_db, second)
    assert_named_by_id(why, sid, "team-key", shared["value"])
    assert "91%" in why["text"] and "80%" in why["text"] and why["env_var"] == VAR
    assert env_of(client, desk, first)["name"] == "team-key"  # a run holding it keeps it
    set_usage(
        hub_db, "team-key", {"status": "allowed", "windows": {"five_hour": {"utilization": 91, "resets_at": earlier}}}
    )
    assert env_of(client, desk, second)["name"] == "team-key"


def test_credential_share_lease_max_running_lends_to_that_many_runs_at_once(client, hub, hub_db, shared):
    desk, sid = shared["desk"], shared["share"]
    set_share(hub_db, sid, max_running=1)
    first = run_on(client, hub["other"], desk, "notes")
    assert env_of(client, desk, first)["name"] == "team-key"
    second = run_on(client, hub["other"], desk, "evo-agents")
    assert env_of(client, desk, second) == {}
    [why] = refusals(hub_db, second)
    assert_named_by_id(why, sid, "team-key", shared["value"])
    assert "1 runs hold it" in why["text"]


def test_credential_share_budget_counts_the_months_cost_and_an_estimate_never_zero(client, hub, hub_db, shared):
    desk, sid = shared["desk"], shared["share"]
    set_share(hub_db, sid, budget_usd=Decimal("0.50"))
    first = run_on(client, hub["other"], desk, "notes")
    assert env_of(client, desk, first) == {}  # nothing spent, but a run is estimated at ESTIMATE_USD
    [why] = refusals(hub_db, first)
    assert_named_by_id(why, sid, "team-key", shared["value"])
    assert "budget of 0.50 USD" in why["text"] and "1.00 USD each" in why["text"]
    set_share(hub_db, sid, budget_usd=Decimal("5"))
    assert env_of(client, desk, first)["name"] == "team-key"
    set_cost(hub_db, first, 4.5)
    second = run_on(client, hub["other"], desk, "evo-agents")
    assert env_of(client, desk, second) == {}  # 4.50 spent and 4.50 more estimated for this run
    assert "4.50 USD" in refusals(hub_db, second)[0]["text"]
    set_share(hub_db, sid, budget_usd=Decimal("10"))
    assert env_of(client, desk, second)["name"] == "team-key"


def test_credential_share_priority_the_owners_runs_go_first_while_the_account_cools(client, hub, hub_db, shared):
    desk, sid = shared["desk"], shared["share"]
    cool(hub_db, "team-key", 30)
    theirs = run_on(client, hub["other"], desk, "notes")
    assert env_of(client, desk, theirs) == {}
    [why] = refusals(hub_db, theirs)
    assert_named_by_id(why, sid, "team-key", shared["value"])
    assert "owner's runs go first" in why["text"]
    box = worker_of(client, hub["owner"], "box")
    own = run_on(client, hub["owner"], box, "evo-agents")
    assert env_of(client, box, own)["name"] == "team-key"
    assert taken_how(hub_db, own) == [(VAR, "team-key", "cooling")]


# Taking back (criterion a3)


def test_credential_share_revoke_takes_back_the_leases_at_once_and_an_ended_share_at_the_reaper(
    client, hub, hub_db, shared
):
    desk, sid = shared["desk"], shared["share"]
    first = run_on(client, hub["other"], desk, "notes")
    assert env_of(client, desk, first)["name"] == "team-key"
    response = client.delete(f"/v1/shares/{sid}", headers=hub["owner"])
    assert response.status_code == 200, response.text
    assert [row[3] for row in share_leases(hub_db, first)] == [True]
    assert env_of(client, desk, first) == {}
    # a share that ends: the reaper takes back what is out under it
    again = share(client, hub["owner"], "team-key")
    second = run_on(client, hub["other"], desk, "evo-agents")
    assert env_of(client, desk, second)["name"] == "team-key"
    set_share(hub_db, again["id"], expires_at=func.now() - timedelta(seconds=1))
    reap(client)
    assert [row[2:] for row in share_leases(hub_db, second)] == [(again["id"], True)]


# Cost by principal (criterion a4)


def test_credential_share_cost_by_principal_the_owner_sees_every_row_a_grantee_their_own(client, hub, hub_db, shared):
    desk = shared["desk"]
    theirs = run_on(client, hub["other"], desk, "notes")
    assert env_of(client, desk, theirs)["name"] == "team-key"
    set_cost(hub_db, theirs, 1.25)
    box = worker_of(client, hub["owner"], "box")
    own = run_on(client, hub["owner"], box, "evo-agents")
    assert env_of(client, box, own)["name"] == "team-key"
    set_cost(hub_db, own, 2.5)
    owner_view = client.get("/v1/accounts/team-key", headers=hub["owner"])
    assert owner_view.status_code == 200, owner_view.text
    rows = {row["principal"]: row for row in owner_view.json()["cost_by_principal"]}
    assert set(rows) == {OWNER, OTHER}
    assert rows[OTHER] == {
        "principal": OTHER,
        "runs_24h": 1,
        "runs_7d": 1,
        "runs_month": 1,
        "cost_24h_usd": 1.25,
        "cost_7d_usd": 1.25,
        "cost_month_usd": 1.25,
    }
    assert rows[OWNER]["cost_month_usd"] == 2.5
    assert owner_view.json()["runs_7d"] == 2
    grantee_view = client.get(f"/v1/accounts/team-key?owner={OWNER}", headers=hub["other"])
    assert grantee_view.status_code == 200, grantee_view.text
    seen = grantee_view.json()
    assert [row["principal"] for row in seen["cost_by_principal"]] == [OTHER]
    assert seen["runs_7d"] == 1 and seen["cost_7d_usd"] == 1.25
    assert [run["run_id"] for run in seen["held_by"]] == [theirs]
    assert [item["id"] for item in seen["shares"]] == [shared["share"]] and seen["workers"] == []
    assert shared["value"] not in grantee_view.text
    # without the account of their own, it is not theirs to see; the admin, whom it was not shared with, gets 404
    assert client.get("/v1/accounts/team-key", headers=hub["other"]).status_code == 404
    assert client.get(f"/v1/accounts/team-key?owner={OWNER}", headers=hub["admin"]).status_code == 404


# Schema 0105


def test_credential_share_lease_migration_adds_principal_and_share_and_going_back_drops_them(hub_db):
    def columns() -> set[str]:
        c = INFO_COLUMNS.c
        query = select(c.column_name).where(c.table_schema == "public", c.table_name == "credential_leases")
        return {row[0] for row in sql(hub_db, query)}

    assert {"principal_id", "share_id"} <= columns()
    move_to(hub_db, "0104", down=True)
    assert not {"principal_id", "share_id"} & columns()
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0105") :]
    assert {"principal_id", "share_id"} <= columns()
