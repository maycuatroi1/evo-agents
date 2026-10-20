"""Which triage chat a failed run opens its thread in (``evo_agents.hub.server.triage._chat``): the one chat of its
project, "Triage: <project>", for the runs of every plan and the runs with no plan; the chat of its plan for a plan
whose label is above the project's default label, so that nobody reads a failure of a plan they could not read; the
chat of its plan too when the project's chat cannot take the owner; the triage chats plans had before left as they
were; and a project's chat that its creator left still taking threads."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, insert, select

from evo_agents.hub import tables
from tests.hub.live import sql
from tests.hub.test_chat_runs import CHECKOUTS, chat_worker
from tests.hub.test_chats import agent
from tests.hub.test_plans import registration
from tests.hub.test_runs import OTHER, PLAN, PROJECT, claim, client, hub, plan_body  # noqa: F401
from tests.hub.test_triage import failed_step, root_of, row_of

SECOND = "lumen-docs"  # a second plan of the project
GUARDED = "northwind-billing"  # a plan labelled customer, above the project's default label internal
HARNESS = "evo-agents-harness"  # the harness path tests.hub.test_plans.registration gives the project
WITH_HARNESS = {**CHECKOUTS, f"{PROJECT}/{HARNESS}": {"path": f"/src/{HARNESS}", "branch": "main"}}
LUMEN = "lumen"  # another project, whose agent is invited into the project's chat
CUSTOMER_SINKS = [
    {"id": "claude-code@anthropic", "kind": "agent-session", "clearance": {"level": "customer"}},
    {"id": "hub", "kind": "hub", "clearance": {"level": "customer"}},
]


def pushed(client, hub, plan_id: str, **fields) -> None:
    body = {**plan_body(), "id": plan_id}
    response = client.put(
        f"/v1/projects/{PROJECT}/plans/{plan_id}", json={"body": body, **fields}, headers=hub["owner"]
    )
    assert response.status_code == 200, response.text


def granted(client, hub, project: str, login: str, role: str, level: str) -> None:
    grant = {"role": role, "max_level": level}
    response = client.put(f"/v1/admin/projects/{project}/grants/{login}", json=grant, headers=hub["admin"])
    assert response.status_code == 200, response.text


def chat_of(hub_db, run_id: int) -> int:
    ((chat_id, *_),) = row_of(hub_db, run_id)
    return chat_id


def triage_chats(hub_db) -> list[tuple]:
    """(plan_id, title) of each triage chat, the project's (plan_id None) first."""
    tc, c = tables.triage_chats, tables.chats
    query = select(tc.c.plan_id, c.c.title).join_from(tc, c, c.c.id == tc.c.chat_id)
    return sql(hub_db, query.order_by(tc.c.plan_id.is_not(None), tc.c.plan_id))


def failed_author_run(client, hub, hub_db, worker) -> int:
    """An author run with no plan on ``worker``, which fails at its claim (the hub holds no create-exec-plan skill);
    its id."""
    body = {"request": "Write the plan of the billing export.", "worker_id": worker["id"]}
    response = client.post(f"/v1/projects/{PROJECT}/author-runs", json=body, headers=hub["owner"])
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    r = tables.runs
    for _ in range(4):  # the chat runs earlier failures queued come first
        if sql(hub_db, select(r.c.state).where(r.c.id == run_id)) == [("failed",)]:
            break
        claim(client, worker)
    assert sql(hub_db, select(r.c.state, r.c.plan_id).where(r.c.id == run_id)) == [("failed", None)]
    return run_id


def test_the_failed_runs_of_two_plans_and_a_run_with_no_plan_open_three_threads_in_the_one_chat_of_the_project(
    client, hub, hub_db
):
    pushed(client, hub, SECOND)
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=6, checkouts=WITH_HARNESS)
    first = failed_step(client, hub, worker, 2)
    second = failed_step(client, hub, worker, 4, plan=SECOND, claims=3)
    third = failed_author_run(client, hub, hub_db, worker)
    rows = [row_of(hub_db, run_id)[0] for run_id in (first, second, third)]
    assert len({chat_id for chat_id, *_ in rows}) == 1 and len({root for _, root, *_ in rows}) == 3
    assert triage_chats(hub_db) == [(None, f"Triage: {PROJECT}")]
    bodies = [root_of(client, hub, chat_id, root)["body"] for chat_id, root, *_ in rows]
    assert f"Run #{first} of step 2 of {PLAN} failed." in bodies[0] and f"Where: plan {PLAN}, step 2" in bodies[0]
    assert f"Run #{second} of step 4 of {SECOND} failed." in bodies[1] and f"Where: plan {SECOND}, step 4" in bodies[1]
    assert f"Author run #{third} failed." in bodies[2] and f"Where: {PROJECT}." in bodies[2]
    m = tables.chat_members
    query = select(func.count()).where(m.c.chat_id == rows[0][0], m.c.user_id.is_not(None), m.c.removed_at.is_(None))
    assert sql(hub_db, query) == [(1,)]  # the owner, once


def test_a_plan_labelled_above_the_projects_default_keeps_a_chat_of_its_own_that_others_do_not_read(
    client, hub, hub_db
):
    registered = client.put(f"/v1/projects/{PROJECT}", json=registration(CUSTOMER_SINKS), headers=hub["admin"])
    assert registered.status_code == 200, registered.text
    granted(client, hub, PROJECT, "owner", "writer", "customer")
    pushed(client, hub, GUARDED, label={"level": "customer"})
    pushed(client, hub, SECOND, label={"level": "public"})  # below the default: the project's chat
    mine = chat_worker(client, hub["owner"], "mac-mini", slots=6)
    theirs = chat_worker(client, hub["other"], "studio", slots=4)
    guarded = failed_step(client, hub, mine, 2, plan=GUARDED)
    lower = failed_step(client, hub, mine, 2, plan=SECOND, claims=3)
    plain = failed_step(client, hub, theirs, 2, who="other")
    assert triage_chats(hub_db) == [(None, f"Triage: {PROJECT}"), (GUARDED, f"Triage: {GUARDED}")]
    tc = tables.triage_chats
    ((project_chat,),), ((own_chat,),) = (
        sql(hub_db, select(tc.c.chat_id).where(tc.c.plan_id.is_not_distinct_from(plan))) for plan in (None, GUARDED)
    )
    assert [chat_of(hub_db, run_id) for run_id in (lower, plain, guarded)] == [project_chat, project_chat, own_chat]
    # someone-else, whose grant stops at internal, is in the project's chat and not in the plan's: its failure is
    # the 404 of a chat they are not in, as the plan is the 404 of a plan they cannot read.
    assert client.get(f"/v1/projects/{PROJECT}/plans/{GUARDED}", headers=hub["other"]).status_code == 404
    assert client.get(f"/v1/chats/{own_chat}/messages", headers=hub["other"]).status_code == 404
    assert client.get(f"/v1/chats/{project_chat}/messages", headers=hub["other"]).status_code == 200


def test_an_owner_the_projects_chat_cannot_take_gets_the_thread_in_the_chat_of_the_plan(client, hub, hub_db):
    assert client.put(f"/v1/projects/{LUMEN}", json=registration(), headers=hub["admin"]).status_code == 200
    granted(client, hub, LUMEN, OTHER, "admin", "internal")
    agent(client, hub["other"], "lumen-bot", project=LUMEN)
    theirs = chat_worker(client, hub["other"], "studio", slots=4)
    first = failed_step(client, hub, theirs, 2, who="other")
    project_chat = chat_of(hub_db, first)
    added = client.put(f"/v1/chats/{project_chat}/agents/lumen-bot", headers=hub["other"])
    assert added.status_code == 200, added.text  # the chat's label now names lumen, which the owner holds no grant on
    mine = chat_worker(client, hub["owner"], "mac-mini", slots=4)
    second = failed_step(client, hub, mine, 4)
    ((chat_id, root_id, *_),) = row_of(hub_db, second)
    assert chat_id != project_chat
    assert triage_chats(hub_db) == [(None, f"Triage: {PROJECT}"), (PLAN, f"Triage: {PLAN}")]
    assert f"Run #{second} of step 4 of {PLAN} failed." in root_of(client, hub, chat_id, root_id)["body"]


def test_the_triage_chat_a_plan_had_before_stays_as_it_was_and_takes_no_new_thread(client, hub, hub_db):
    u, c, tc = tables.users, tables.chats, tables.triage_chats
    ((owner_id,),) = sql(hub_db, select(u.c.id).where(u.c.login == "owner"))
    ((project_id,),) = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))
    ((old,),) = sql(hub_db, insert(c).values(title=f"Triage: {PLAN}", created_by=owner_id).returning(c.c.id))
    sql(hub_db, insert(tables.chat_members).values(chat_id=old, user_id=owner_id, added_by=owner_id))
    sql(hub_db, insert(tc).values(project_id=project_id, plan_id=PLAN, chat_id=old))
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4)
    run_id = failed_step(client, hub, worker, 2)
    assert chat_of(hub_db, run_id) != old
    assert triage_chats(hub_db) == [(None, f"Triage: {PROJECT}"), (PLAN, f"Triage: {PLAN}")]
    m = tables.chat_messages
    assert sql(hub_db, select(func.count()).where(m.c.chat_id == old)) == [(0,)]


def test_the_projects_chat_still_takes_threads_after_its_creator_left_it(client, hub, hub_db):
    theirs = chat_worker(client, hub["other"], "studio", slots=4)
    mine = chat_worker(client, hub["owner"], "mac-mini", slots=4)
    first = failed_step(client, hub, theirs, 2, who="other")  # someone-else makes the chat
    chat_id = chat_of(hub_db, first)
    failed_step(client, hub, mine, 4)  # the owner joins it
    assert client.delete(f"/v1/chats/{chat_id}/people/{OTHER}", headers=hub["other"]).status_code == 204
    again = failed_step(client, hub, theirs, 5, who="other", claims=3)
    assert chat_of(hub_db, again) == chat_id
    assert triage_chats(hub_db) == [(None, f"Triage: {PROJECT}")]
    m, u = tables.chat_members, tables.users
    query = (
        select(u.c.login).join_from(m, u, u.c.id == m.c.user_id).where(m.c.chat_id == chat_id, m.c.removed_at.is_(None))
    )
    assert sorted(login for (login,) in sql(hub_db, query)) == ["owner", OTHER]
