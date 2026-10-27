"""``evo-agents hub loop adopt PLAN --goal G-n --use-cases ...`` (plan srs-backlog-loop, criterion a6): a plan that is on
the hub becomes a deliver of the Loop for use cases of the document a goal aims at. The use cases are being delivered
(in no backlog, so no build takes them) and delivered once the deliver is done; for a plan already completed they are
delivered at once. The command line runs without Postgres; the rest needs EVO_HUB_TEST_DSN. The sample is
``tests/hub/fixtures/srs/demo-shop-srs.md``.
"""

from __future__ import annotations

from functools import partial
from types import SimpleNamespace

import pytest

from evo_agents.hub.contract import commands
from tests.hub import pg

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
METRIC = {"key": "srs.backlog.open", "aggregate": "last", "comparator": "<=", "target": 0}
PLAN = "coding-activity"


def test_adopt_takes_a_plan_a_goal_and_use_cases_and_is_in_the_contract():
    printed = commands()["hub loop adopt"]
    flags = {flag for option in printed["options"] for flag in option["flags"]}
    assert {"--goal", "--use-cases", "--project"} <= flags


if pg.DSN:
    from sqlalchemy import func, select, update

    from evo_agents.hub import tables
    from evo_agents.hub.server.loop import backlog as hub_backlog
    from evo_agents.hub.server.loop import delivery
    from evo_agents.hub.server.loop.tick import tick
    from tests.hub.live import sql
    from tests.hub.test_ba_agent import (  # noqa: F401 (app_key, client, hub, members and srs_core are fixtures)
        CHI,
        PROJECT,
        client,
        hub,
        imported,
        members,
        srs_core,
        submitted,
    )
    from tests.hub.test_ba_agent import review as whole
    from tests.hub.test_credentials_api import app_key  # noqa: F401
    from tests.hub.test_loop_srs_backlog import goal, ids, run
    from tests.hub.test_loop_tick import policy

    ADOPT = f"/v1/projects/{PROJECT}/loop/adopt"

    def pushed(client, hub, plan_id: str = PLAN, status: str = "pending") -> None:
        step = {"id": 1, "title": "Coding", "repo": "evo-agents", "what": "the change", "status": status}
        body = {"id": plan_id, "goal": "Coding activity.", "repos": [{"repo": "evo-agents"}], "steps": [step]}
        answer = client.put(f"/v1/projects/{PROJECT}/plans/{plan_id}", json={"body": body}, headers=hub["owner"])
        assert answer.status_code == 200, answer.text

    def approved(client, hub, hub_db, **fields) -> tuple[int, int, int]:
        imported(client, hub["chi"])
        submitted(client, hub["chi"], 1)
        assert whole(client, hub["other"], 1).json()["state"] == "approved"
        assert goal(client, hub, **fields).status_code == 200
        return ids(hub_db)

    def adopt(client, headers, *codes, plan: str = PLAN, goal_: str = "G-1"):
        return client.post(ADOPT, json={"plan": plan, "goal": goal_, "use_cases": list(codes)}, headers=headers)

    def deliver(db) -> object:
        i = tables.loop_items
        return SimpleNamespace(**dict(zip(i.c.keys(), sql(db, select(i).where(i.c.kind == "deliver"))[0], strict=True)))

    def states(client, document: int) -> dict[str, str]:
        return {key: row.state for key, row in run(client, delivery.read, document).items()}


@needs_pg
def test_adopting_a_plan_makes_a_deliver_of_the_goal_and_holds_its_use_cases(client, hub, hub_db):
    project, document, goal_id = approved(client, hub, hub_db)
    pushed(client, hub)
    made = adopt(client, hub["owner"], "UC ORD-1", "uc ret-1")
    assert made.status_code == 200, made.text
    found = made.json()
    assert (found["goal"], found["plan"], found["state"], found["delivered"]) == ("G-1", PLAN, "queued", False)
    assert found["use_cases"] == ["UC ORD-1", "UC RET-1"]
    row = deliver(hub_db)
    assert (row.id, row.key, row.goal_id, row.plan_id, row.parent_id) == (
        found["item"],
        f"deliver:{PLAN}",
        goal_id,
        PLAN,
        None,
    )
    assert (row.srs_document_id, row.cycle, row.state, row.reason) == (document, "development", "queued", None)
    assert states(client, document) == {"uc ord-1": "delivering", "uc ret-1": "delivering"}
    ledger = run(client, delivery.read, document)
    assert ledger["uc ord-1"].item_id == row.id and ledger["uc ord-1"].goal_id == goal_id
    assert [c.ref for c in run(client, hub_backlog.backlog_of, goal_id)] == ["UC ORD-2"], "in no other build"
    policy(client, hub, {"enabled": True})
    ticked = client.portal.call(partial(tick, client.app.state.engine, project, dispatch=None))
    assert ticked is not None and deliver(hub_db).state == "queued", "a tick leaves an adopted deliver as it is"
    assert states(client, document)["uc ord-1"] == "delivering"
    # the deliver done: the use cases are delivered with the plan as evidence
    sql(
        hub_db,
        update(tables.loop_items).where(tables.loop_items.c.id == row.id).values(state="done", finished_at=func.now()),
    )
    client.portal.call(partial(tick, client.app.state.engine, project, dispatch=None))
    assert states(client, document) == {"uc ord-1": "delivered", "uc ret-1": "delivered"}
    assert run(client, delivery.read, document)["uc ord-1"].evidence["plan"] == PLAN


@needs_pg
def test_a_plan_already_completed_delivers_its_use_cases_at_once(client, hub, hub_db):
    _, document, goal_id = approved(client, hub, hub_db)
    pushed(client, hub, status="done")
    done = client.post(f"/v1/projects/{PROJECT}/plans/{PLAN}/complete", json={}, headers=hub["owner"])
    assert done.status_code == 200, done.text
    made = adopt(client, hub["owner"], "UC ORD-1")
    assert made.status_code == 200 and made.json()["delivered"] is True, made.text
    row = deliver(hub_db)
    assert (row.state, row.parent_id, row.plan_id) == ("done", None, PLAN) and row.finished_at is not None
    assert states(client, document) == {"uc ord-1": "delivered"}
    assert run(client, delivery.read, document)["uc ord-1"].evidence["deliver"] == row.id
    assert [c.ref for c in run(client, hub_backlog.backlog_of, goal_id)] == ["UC ORD-2", "UC RET-1"]


@needs_pg
def test_adopt_is_refused_for_what_it_cannot_take(client, hub, hub_db):
    _, document, _ = approved(client, hub, hub_db)
    pushed(client, hub)
    assert adopt(client, hub["owner"], "UC ORD-1", plan="no-such-plan").status_code == 404
    assert adopt(client, hub["owner"], "UC NOPE-9").status_code == 422
    assert adopt(client, hub["owner"], "UC ORD-1", goal_="G-9").status_code == 404
    assert adopt(client, hub["owner"]).status_code == 422, "no use cases"
    assert adopt(client, hub["other"], "UC ORD-1").status_code == 403, "a writer who does not own the goal"
    assert adopt(client, hub["reader"], "UC ORD-1").status_code == 403
    assert sql(hub_db, select(tables.loop_items.c.id)) == [] and states(client, document) == {}
    assert adopt(client, hub["owner"], "UC ORD-1").status_code == 200
    again = adopt(client, hub["owner"], "UC ORD-2")
    assert again.status_code == 409 and "is a deliver of the Loop already" in again.text
    pushed(client, hub, "second-plan")
    held = adopt(client, hub["owner"], "UC ORD-1", plan="second-plan")
    assert held.status_code == 409 and "being delivered already" in held.text
    assert len(sql(hub_db, select(tables.loop_items.c.id))) == 1


@needs_pg
def test_the_writer_who_owns_the_goal_may_adopt(client, hub, hub_db):
    _, document, _ = approved(client, hub, hub_db, owner=CHI)
    pushed(client, hub)
    assert adopt(client, hub["other"], "UC ORD-1").status_code == 403
    assert adopt(client, hub["chi"], "UC ORD-1").status_code == 200
    assert states(client, document) == {"uc ord-1": "delivering"}
