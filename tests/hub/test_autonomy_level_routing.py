"""Who a decision and a notice go to, and the card of a claim (``evo_agents.hub.server.principals``), as criterion a12
of plan autonomy-levels names them: schema 0113 gives decisions asked_of and the categories merge, credential and
access; a decision of a run whose principal is an intern goes to the person they report to, and after 24 hours to the
leads of their unit too; an intern never answers a decision of their own run; a notice of a class in notify reaches
the principal and an intern's mentor; the claim's prompt opens with the run's card. The card itself:
test_autonomy_level_card."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, select, update

from evo_agents.hub import autonomy_card, runs, tables
from evo_agents.hub.server import autonomy_gates, principals, run_autonomy
from tests.hub.live import ADMIN, sql
from tests.hub.test_autonomy_level_runs import daemon, on_db, org, user_id
from tests.hub.test_decisions import (  # noqa: F401 (client and hub are fixtures)
    answer,
    answered,
    asked,
    client,
    decision,
    hub,
)
from tests.hub.test_migrate import columns, move_to
from tests.hub.test_plan_runs import dispatched_plan, fleet_worker
from tests.hub.test_runs import OTHER, OWNER, PROJECT, WRITER, claim, dispatched, moved, set_run

COHORT = "intern-2026"


def interns(client, hub) -> None:
    """Unit lab with owner and the admin its leads, the admin a writer of the project too, and someone-else an intern
    of its cohort who reports to owner."""
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{ADMIN}", json=WRITER, headers=hub["admin"])
    assert granted.status_code == 200, granted.text
    org(client, hub["admin"], "POST", "/org/units", {"slug": "lab", "parent": "root", "kind": "team"}, 201)
    for login in (OWNER, ADMIN):
        org(client, hub["admin"], "POST", "/org/seats", {"login": login, "unit": "lab", "role": "lead"}, 201)
    cohort = {"slug": COHORT, "parent": "lab", "kind": "cohort", "ends_on": "2099-12-31"}
    org(client, hub["admin"], "POST", "/org/units", cohort, 201)
    intern = {"login": OTHER, "unit": COHORT, "role": "intern", "reports_to": OWNER}
    org(client, hub["admin"], "POST", "/org/seats", intern, 201)


def intern_run(client, hub) -> tuple[dict, dict, dict]:
    """A plan run the intern dispatched, claimed by their worker and running; its claim."""
    worker = daemon(client, fleet_worker(client, hub["other"], "intern-box"))
    run = dispatched_plan(client, hub["other"], runtime="any")
    spec = claim(client, worker)
    assert spec["id"] == run["id"]
    moved(client, worker, run["id"], "running")
    return worker, run, spec


def notified(client, headers, decision_id: int) -> list[str]:
    mine = client.get("/v1/me/notifications", params={"kind": "decision"}, headers=headers).json()["notifications"]
    return [item["body"] for item in mine if item["decision_id"] == decision_id]


def test_autonomy_level_routing_migration_0108_adds_asked_of_and_the_categories_and_going_back_drops_them(
    client, hub, hub_db
):
    interns(client, hub)
    worker, run, _ = intern_run(client, hub)
    merge = asked(client, worker, run["id"], category="merge")
    kept = asked(client, worker, run["id"], category="architecture")
    d = tables.decisions
    assert sql(hub_db, select(d.c.category).where(d.c.asked_of.is_not(None)).order_by(d.c.id)) == [
        ("merge",),
        ("architecture",),
    ]
    move_to(hub_db, "0112", down=True)
    assert "asked_of" not in columns(hub_db, "decisions")
    left = sql(hub_db, select(d.c.id).where(d.c.run_id == run["id"]))
    assert left == [(kept["id"],)], f"decision {merge['id']} of category merge is gone"
    move_to(hub_db, "0113")
    assert "asked_of" in columns(hub_db, "decisions")


def test_autonomy_level_routing_of_an_interns_decision_to_their_mentor_and_after_a_day_to_the_leads_too(
    client, hub, hub_db
):
    interns(client, hub)
    worker, run, _ = intern_run(client, hub)
    found = asked(client, worker, run["id"], category="merge", question="May I merge pull request 7?")
    assert (found["owner"], found["asked_of"]) == (OTHER, OWNER)
    assert [body.endswith(f"asked of {OWNER}.") for body in notified(client, hub["owner"], found["id"])] == [True]
    assert notified(client, hub["other"], found["id"]) == [], "the intern is not asked"
    # the intern does not answer a K5 decision of their own run, nor a lead before a day has gone by
    refused = answer(client, hub["other"], found["id"], option="postgres")
    assert refused.status_code == 403 and f"only {OWNER}, whom it is asked of" in refused.text
    assert answer(client, hub["admin"], found["id"], option="postgres").status_code == 403
    engine = client.app.state.engine
    assert client.portal.call(principals.escalate, engine) == 0
    # a day later the hourly job tells the other lead of the unit, once, and they may answer
    d = tables.decisions
    later = update(d).where(d.c.id == found["id"]).values(asked_at=func.now() - timedelta(hours=25))
    sql(hub_db, later)
    assert client.portal.call(principals.escalate, engine) == 1
    assert client.portal.call(principals.escalate, engine) == 0, "each lead is told once"
    (told,) = notified(client, hub["admin"], found["id"])
    assert f"asked of {OWNER} more than 24 hours ago" in told and "you lead their unit" in told
    done = answered(client, hub["admin"], found["id"], option="postgres")
    assert (done["state"], done["answered_by"]) == ("answered", ADMIN)


def test_autonomy_level_routing_card_of_the_claim_opens_the_prompt_of_an_interns_plan_run_and_a_members_step_run(
    client, hub
):
    interns(client, hub)
    _, _, spec = intern_run(client, hub)
    card = spec["autonomy"]
    assert spec["prompt"].startswith(card["card"] + "\n\n") and card["card"].startswith(autonomy_card.HEADING)
    assert f"Principal: {OTHER}, intern in unit {COHORT}, reports to {OWNER}" in card["card"]
    assert "merge" in card["categories"] and "merging, deploying and releasing are the agent's" not in spec["prompt"]
    # a member's step run: the card at A5 is short
    worker = fleet_worker(client, hub["owner"], "owner-box")
    run = dispatched(client, hub["owner"], [2])[0]
    owned = claim(client, worker)
    assert owned["id"] == run["id"] and owned["prompt"].startswith(autonomy_card.HEADING)
    assert "Every class of action is yours to do." in owned["autonomy"]["card"]


def test_autonomy_level_routing_of_a_members_decision_to_the_principal_in_a_new_category(client, hub):
    worker = fleet_worker(client, hub["owner"], "owner-box")
    run = dispatched_plan(client, hub["owner"])
    assert claim(client, worker)["id"] == run["id"]
    moved(client, worker, run["id"], "running")
    found = asked(client, worker, run["id"], category="credential", question="Rotate the deploy key?")
    assert (found["category"], found["owner"], found["asked_of"]) == ("credential", OWNER, OWNER)
    assert answered(client, hub["owner"], found["id"], option="postgres")["answered_by"] == OWNER


def test_autonomy_level_routing_of_a_notice_to_the_principal_and_an_interns_mentor(client, hub, hub_db):
    interns(client, hub)
    _, run, _ = intern_run(client, hub)
    project_id = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    said = autonomy_gates.Verdict(True, "notify", None, False, user_id(hub_db, OTHER), project_id, run["id"], "K5")
    on_db(hub_db, lambda conn: autonomy_gates.told(conn, said, "pull:7", "Merged pull request 7"))
    for who in ("other", "owner"):
        notices = client.get("/v1/me/notifications", params={"kind": "notice"}, headers=hub[who]).json()
        titles = [
            item["title"] for item in notices["notifications"] if item["notice_kind"] == runs.AUTONOMY_DECISION_CATEGORY
        ]
        assert titles == ["Merged pull request 7"], who
    notices = client.get("/v1/me/notifications", params={"kind": "notice"}, headers=hub["admin"]).json()
    assert not [item for item in notices["notifications"] if item["run_id"] == run["id"]]


def test_autonomy_level_routing_of_a_decision_of_a_run_the_hub_makes_to_the_sponsor_of_its_agent(
    client, hub, hub_db, monkeypatch
):
    run = dispatched(client, hub["owner"], [2])[0]
    profile = tables.agent_profiles
    project_id = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    profile_id = sql(hub_db, select(profile.c.id).where(profile.c.project_id == project_id))[0][0]
    set_run(hub_db, run["id"], profile_id=profile_id)
    # a step run stands for a run the hub makes (review, release): their rows take more than this test needs
    monkeypatch.setattr(run_autonomy, "HUB_KINDS", ("step",))
    asked_of = lambda: on_db(hub_db, lambda conn: principals.asked_of(conn, run["id"]))  # noqa: E731
    assert asked_of() == user_id(hub_db, OWNER), "an agent without a seat: its principal"
    seat = {"agent": PROJECT, "project": PROJECT, "unit": "root", "role": "agent", "sponsor": OTHER}
    org(client, hub["admin"], "POST", "/org/seats", seat, 201)
    assert asked_of() == user_id(hub_db, OTHER)
