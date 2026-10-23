"""No borrowed level, the floor S5 (plan autonomy-levels, criterion a10): a run never acts above its principal, whoever
else is in the chain: an agent type's run_as bounds it but never raises it, an agent's run asked by another agent
stays within the asking run's snapshot, a worker another member shares gives the run its principal's autonomy and not
its owner's, and a shared credential (source 6, ``credential_share``) only ever lowers it."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import autonomy_levels, tables
from evo_agents.hub.autonomy_levels import CREDENTIAL_SHARE, Source
from evo_agents.hub.server import run_autonomy
from tests.hub.live import sql
from tests.hub.test_autonomy_level_runs import daemon, on_db, pref, snapshot_of, user_id
from tests.hub.test_runs import (  # noqa: F401 (client and hub are fixtures)
    OTHER,
    OWNER,
    PROJECT,
    add_worker,
    claim,
    client,
    dispatched,
    hub,
)


def project_id(db) -> int:
    p = tables.projects
    return sql(db, select(p.c.id).where(p.c.name == PROJECT))[0][0]


def stamped(db, values: dict, who: run_autonomy.For) -> dict:
    return on_db(db, lambda conn: run_autonomy.stamp(conn, values, who))


def test_autonomy_level_borrow_run_as_bounds_a_run_and_never_raises_it_above_its_principal(client, hub, hub_db):
    owner, other = user_id(hub_db, OWNER), user_id(hub_db, OTHER)
    values = {"kind": "chat", "project_id": project_id(hub_db), "dispatched_by": owner}
    pref(client, hub["owner"], "A1")
    # the agent type runs as someone at A5 (no preference): the principal's A1 holds
    higher = stamped(hub_db, values, run_autonomy.For(user_id=owner, also=(other,)))
    assert (higher["principal_id"], higher["autonomy"]["level"], higher["autonomy"]["also"]) == (owner, "A1", [OTHER])
    assert higher["autonomy"]["classes"]["K5"]["mode"] == "ask", "K5 at A1, not auto as at A5"
    # and someone lower than the principal lowers it
    pref(client, hub["owner"], "A4")
    pref(client, hub["other"], "A2", classes={"K10": "deny"})
    lower = stamped(hub_db, values, run_autonomy.For(user_id=owner, also=(other,)))
    assert lower["autonomy"]["level"] == "A2" and lower["autonomy"]["classes"]["K10"]["mode"] == "deny"


def test_autonomy_level_borrow_an_agent_asked_by_another_agent_stays_within_the_asking_runs_snapshot(
    client, hub, hub_db
):
    pref(client, hub["owner"], "A2")
    asking = dispatched(client, hub["owner"], [2])[0]
    assert snapshot_of(hub_db, asking["id"])["level"] == "A2"
    pref(client, hub["owner"], "A5")  # raised after the asking run was queued
    values = {"kind": "chat", "project_id": project_id(hub_db), "dispatched_by": user_id(hub_db, OTHER)}
    asked = stamped(hub_db, values, run_autonomy.For(within=asking["id"]))
    assert asked["principal_id"] == user_id(hub_db, OWNER), "it acts for the principal of the asking run"
    assert asked["autonomy"]["level"] == "A2", "never above the snapshot of the run that asked"
    # nor above the asked agent's own run_as
    pref(client, hub["other"], "A1")
    bounded = stamped(hub_db, values, run_autonomy.For(within=asking["id"], also=(user_id(hub_db, OTHER),)))
    assert bounded["autonomy"]["level"] == "A1"


def test_autonomy_level_borrow_a_shared_worker_runs_at_the_principals_level_not_its_owners(client, hub, hub_db):
    lab = daemon(client, add_worker(client, hub["owner"], "lab"))
    shared = client.patch(f"/v1/workers/{lab['id']}", json={"shared": True}, headers=hub["owner"])
    assert shared.status_code == 200, shared.text
    pref(client, hub["other"], "A1")  # the owner of the worker has no preference: A5
    run = dispatched(client, hub["other"], [5], runtime="any")[0]
    spec = claim(client, lab)
    assert spec["id"] == run["id"]
    assert spec["autonomy"]["principal"] == OTHER
    assert spec["autonomy"]["modes"]["K5"] == "ask", "the owner of the worker is at A5, where K5 is auto"
    held = snapshot_of(hub_db, run["id"])
    assert held["level"] == "A1" and held["level_source"] == f"pref:{OTHER}"


def test_autonomy_level_borrow_a_shared_credential_source_6_only_lowers_the_level():
    principal = autonomy_levels.pref_source("lena", "A1")
    wide = Source(CREDENTIAL_SHARE, "A5", {"K7": "auto", "K5": "auto"}, detail="a credential shared at A5")
    found = autonomy_levels.effective([principal, wide, autonomy_levels.instance_source()])
    assert (found.level, found.level_source) == ("A1", "pref:lena")
    assert found.cell("K5").mode == "ask" and found.classes["K7"].source == "pref:lena"
    narrow = Source(CREDENTIAL_SHARE, overrides={"K7": "deny"}, detail="a credential shared for reading")
    top = autonomy_levels.pref_source("lena", None)
    lowered = autonomy_levels.effective([top, narrow, autonomy_levels.instance_source()])
    assert lowered.cell("K7").mode == "deny" and lowered.classes["K7"].source == CREDENTIAL_SHARE
    plain = autonomy_levels.effective(
        [principal, autonomy_levels.credential_source(), autonomy_levels.instance_source()]
    )
    assert plain == autonomy_levels.effective([principal, autonomy_levels.instance_source()])
