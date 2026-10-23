"""The stop of autonomy S4 (``evo_agents.hub.server.autonomy_stops`` and ``autonomy_hold``), as criterion a11 of plan
autonomy-levels names it: ``hub stop --login | --unit | --all`` and ``--lift`` of the same scope; a person stops their
own runs, a lead their unit and those below it, an admin anyone and the hub; the stop cancels the runs of every
principal it covers as ``hub run cancel`` does, ends their leases (test_autonomy_level_ledger for the leases), and holds
every new run of theirs, those the hub makes itself included, with a 403 naming the stop until it is lifted; the audit
rows autonomy.stop and autonomy.stop.lift, and a notice to the people stopped."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi import Request
from sqlalchemy import select

from evo_agents.hub import cli_stop, tables
from evo_agents.hub.server import autonomy_hold, run_autonomy, security
from evo_agents.hub.server.runs.service.dispatch import insert_run
from tests.hub.live import ADMIN, sql
from tests.hub.test_autonomy_level_routing import COHORT, interns
from tests.hub.test_autonomy_level_runs import on_db, user_id
from tests.hub.test_migrate import columns, move_to
from tests.hub.test_runs import (  # noqa: F401 (client and hub are fixtures)
    OTHER,
    OWNER,
    PROJECT,
    add_worker,
    audit_rows,
    claim,
    client,
    dispatch,
    dispatched,
    hub,
    moved,
    of_run,
    state_of,
)


def stop(client, headers, status: int = 201, **body) -> dict:
    response = client.post("/v1/autonomy/stops", json=body, headers=headers)
    assert response.status_code == status, response.text
    return response.json()


def lift(client, headers, status: int = 200, **body) -> dict:
    response = client.post("/v1/autonomy/stops/lift", json=body, headers=headers)
    assert response.status_code == status, response.text
    return response.json()


def notices(client, headers) -> list[dict]:
    response = client.get("/v1/me/notifications", params={"kind": "notice"}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["notifications"]


def project_id(db) -> int:
    p = tables.projects
    return sql(db, select(p.c.id).where(p.c.name == PROJECT))[0][0]


def test_autonomy_level_stop_migration_0116_adds_autonomy_stops_and_going_back_drops_it(hub_db):
    move_to(hub_db, "0115", down=True)
    assert not columns(hub_db, "autonomy_stops")
    move_to(hub_db, "0116")
    assert {"scope", "user_id", "unit_id", "reason", "stopped_by", "lifted_at"} <= columns(hub_db, "autonomy_stops")


def test_autonomy_level_stop_of_my_own_login_cancels_my_runs_holds_new_ones_and_lift_lets_them_in(client, hub, hub_db):
    worker = add_worker(client, hub["owner"], "mac-mini")
    held = dispatched(client, hub["owner"], [2])[0]
    assert claim(client, worker)["id"] == held["id"]
    moved(client, worker, held["id"], "running")
    queued = dispatched(client, hub["owner"], [4])[0]
    theirs = dispatched(client, hub["other"], [5])[0]
    view = stop(client, hub["owner"], scope="login", login=OWNER, reason="runaway agent")
    assert (view["scope"], view["target"], view["reason"], view["stopped_by"]) == (
        "login",
        f"login:{OWNER}",
        "runaway agent",
        OWNER,
    )
    assert sorted(view["runs"]) == sorted([held["id"], queued["id"]]) and view["told"] == [OWNER]
    assert state_of(hub_db, queued["id"]) == "cancelled", "a queued run ends at once, as hub run cancel does"
    assert of_run(hub_db, held["id"], "cancel_requested_at")[0][0] is not None, "a held run stops at its heartbeat"
    assert state_of(hub_db, theirs["id"]) == "queued", "another person's run goes on"
    # a new run of mine is refused with the stop named; someone else's is not
    refused = dispatch(client, hub["owner"], [4])  # its run was cancelled: the step is ready again
    assert refused.status_code == 403, refused.text
    message = refused.json()["message"]
    assert f"stop #{view['id']} of login:{OWNER} (runaway agent)" in message
    assert f"evo-agents hub stop --login {OWNER} --lift" in message
    (target, login, _), *_ = [row[1:] for row in audit_rows(hub_db, "autonomy") if row[0] == "autonomy.stop"]
    assert target == f"stop:{view['id']} login:{OWNER} runs=2 leases=0" and login == OWNER
    (notice,) = [item for item in notices(client, hub["owner"]) if item["title"].startswith("Autonomy stopped")]
    assert "runaway agent" in notice["body"] and notice["notice_kind"] == "autonomy"
    # a second stop of the same scope is refused; the lift lets runs in again
    stop(client, hub["owner"], 409, scope="login", login=OWNER)
    lifted = lift(client, hub["owner"], scope="login", login=OWNER)
    assert (lifted["id"], lifted["lifted_by"]) == (view["id"], OWNER) and lifted["lifted_at"]
    assert [row[1] for row in audit_rows(hub_db, "autonomy") if row[0] == "autonomy.stop.lift"] == [
        f"stop:{view['id']} login:{OWNER}"
    ]
    assert dispatch(client, hub["owner"], [4]).status_code == 201
    lift(client, hub["owner"], 404, scope="login", login=OWNER)
    assert [item for item in notices(client, hub["owner"]) if item["title"].startswith("Autonomy stop lifted")]


def test_autonomy_level_stop_permissions_a_person_alone_a_lead_below_them_and_an_admin_anyone_and_the_hub(
    client, hub, hub_db
):
    interns(client, hub)  # owner and the admin lead lab; someone-else is an intern of its cohort
    stop(client, hub["other"], 403, scope="login", login=OWNER)  # not someone else's runs
    stop(client, hub["other"], 403, scope="unit", unit="lab")  # not a unit one does not lead
    stop(client, hub["other"], 403, scope="all")
    stop(client, hub["owner"], 403, scope="all")  # a lead does not stop the hub
    stop(client, hub["owner"], 404, scope="login", login="nobody-here")
    stop(client, hub["owner"], 404, scope="unit", unit="no-such-unit")
    stop(client, hub["owner"], 422, scope="login")
    # a lead stops someone of a unit below the one they lead, and lifts it
    view = stop(client, hub["owner"], scope="login", login=OTHER)
    assert view["told"] == [OTHER]
    lift(client, hub["other"], scope="login", login=OTHER)  # the person stopped may lift it
    stop(client, hub["admin"], scope="login", login=OWNER)
    lift(client, hub["admin"], scope="login", login=OWNER)
    everything = stop(client, hub["admin"], scope="all", reason="incident")
    assert everything["target"] == "all"
    lift(client, hub["owner"], 403, scope="all")
    lift(client, hub["admin"], scope="all")


def test_autonomy_level_stop_never_by_the_agent_of_a_run(client, hub, hub_db):
    run = dispatched(client, hub["owner"], [2])[0]
    scope = security.RunScope(run_id=run["id"], project=PROJECT, role="writer", max_level="internal")

    def scoped(request: Request) -> security.Principal:
        return replace(security.principal(request), scope=scope)

    client.app.dependency_overrides[security.principal] = scoped
    try:
        refused = client.post("/v1/autonomy/stops", json={"scope": "login", "login": OWNER}, headers=hub["owner"])
    finally:
        client.app.dependency_overrides.pop(security.principal, None)
    assert refused.status_code == 403 and "the agent of a run never stops" in refused.text
    assert state_of(hub_db, run["id"]) == "queued"


def test_autonomy_level_stop_of_a_unit_covers_everyone_with_a_seat_in_it_or_below_and_no_one_else(client, hub, hub_db):
    interns(client, hub)
    mine = dispatched(client, hub["owner"], [2])[0]
    theirs = dispatched(client, hub["other"], [4])[0]
    view = stop(client, hub["owner"], scope="unit", unit=COHORT)  # owner leads lab, above the cohort
    assert view["runs"] == [theirs["id"]] and view["told"] == [OTHER]
    assert state_of(hub_db, theirs["id"]) == "cancelled" and state_of(hub_db, mine["id"]) == "queued"
    refused = dispatch(client, hub["other"], [4])
    assert refused.status_code == 403 and f"of unit:{COHORT}" in refused.json()["message"]
    later = dispatch(client, hub["owner"], [5])
    assert later.status_code == 201, later.text
    lab = stop(client, hub["owner"], scope="unit", unit="lab")
    assert OWNER in lab["told"] and ADMIN in lab["told"], "the leads of lab hold seats in it"
    assert sorted(lab["runs"]) == sorted([mine["id"], later.json()[0]["id"]])
    assert dispatch(client, hub["owner"], [5]).status_code == 403


def test_autonomy_level_stop_holds_the_runs_the_hub_makes_itself(client, hub, hub_db):
    owner = user_id(hub_db, OWNER)
    profile = tables.agent_profiles
    profile_id = sql(hub_db, select(profile.c.id).where(profile.c.project_id == project_id(hub_db)))[0][0]
    values = {"kind": "review", "project_id": project_id(hub_db), "dispatched_by": owner, "profile_id": profile_id}
    stop(client, hub["owner"], scope="login", login=OWNER)

    def queue(who):
        async def go(conn):
            with pytest.raises(autonomy_hold.Stopped) as caught:
                await insert_run(conn, values, who)
            return caught.value

        return on_db(hub_db, go)

    curator = queue(None)  # a run of the Curator's night shift acts for the owner here: no sponsor
    assert curator.status_code == 403 and f"of login:{OWNER}" in curator.detail
    loop = queue(run_autonomy.For(user_id=owner, hub=True))  # the Loop, for the owner of the goal
    assert "nothing was queued" in loop.detail
    release = {**values, "kind": "release"}
    on_db(hub_db, lambda conn: _refused(conn, release))
    lift(client, hub["owner"], scope="login", login=OWNER)
    # a stop of the whole hub holds a run without a principal too
    stop(client, hub["admin"], scope="all")
    assert on_db(hub_db, lambda conn: autonomy_hold.held_by(conn, None)) is not None


async def _refused(conn, values: dict) -> None:
    with pytest.raises(autonomy_hold.Stopped):
        await insert_run(conn, values)


def test_autonomy_level_stop_cli_body_and_lines():
    args = SimpleNamespace(login=None, unit="lab", all=False, reason="why", lift=False)
    assert cli_stop.stop_body(args) == {"scope": "unit", "unit": "lab", "reason": "why"}
    assert cli_stop.stop_body(SimpleNamespace(login=None, unit=None, all=True, reason="x", lift=True)) == {
        "scope": "all"
    }
    view = {
        "id": 3,
        "target": "unit:lab",
        "reason": "why",
        "lifted_at": None,
        "lifted_by": None,
        "runs": [4, 5],
        "leases": 2,
        "told": ["ivan"],
    }
    lines = cli_stop.stop_lines(view)
    assert lines[0].startswith("Stop #3 of unit:lab (why) holds")
    assert lines[1] == "Runs cancelled or asked to stop: #4, #5; leases ended: 2." and lines[2] == "Told: ivan."
    lifted = cli_stop.stop_lines({**view, "lifted_at": "2026-10-23T00:00:00Z", "lifted_by": "ivan", "told": []})
    assert lifted == ["Stop #3 of unit:lab lifted by ivan: runs are queued again."]
