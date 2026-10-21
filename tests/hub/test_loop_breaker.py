"""The breaker of a project's Loop (``evo_agents.hub.server.loop.breaker``, ``routes``; ``evo-agents hub loop``), as
criterion a7 of plan loop-engine names it, and schema 0095, on the hub with Postgres.

More work items made in an hour than the policy's loop.max_items_per_hour trip the breaker: the Loop of the project
reads no event past its cursor and hands out no run, each admin of the project gets the notice loop_paused, and the
trip is audited. Only an admin resets it, and the work items of the hour count from the reset. The chain is
``tests.hub.test_loop_tick``'s.
"""

from __future__ import annotations

from functools import partial

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import psycopg
from sqlalchemy import func, insert, select

from evo_agents.hub import tables
from evo_agents.hub.migrate import migrate
from evo_agents.hub.server.loop.tick import tick
from evo_agents.hub.server.runs.service.dispatch import insert_run
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import sql
from tests.hub.test_ba_agent import members  # noqa: F401 (a fixture)
from tests.hub.test_loop_tick import (  # noqa: F401 (client, hub and loop_core are fixtures)
    client,
    hub,
    items,
    loop_core,
    policy,
    project_id,
    push_baseline,
    state,
)
from tests.hub.test_migrate import ALL, move_to
from tests.hub.test_runs import PROJECT, audit_rows
from tests.hub.test_strategist_agent import CARD, OWNER, SLUG, edit

app_key = credentials_api.app_key  # a fixture
LOOP = f"/v1/projects/{PROJECT}/loop"


def ticked(client, db) -> dict:
    return client.portal.call(partial(tick, client.app.state.engine, project_id(db)))


def notices(db) -> list[tuple]:
    n, u = tables.notifications, tables.users
    query = (
        select(u.c.login, n.c.link, n.c.details["items"].as_integer(), n.c.read_at.is_not(None))
        .join_from(n, u, u.c.id == n.c.user_id)
        .where(n.c.notice_kind == "loop_paused")
        .order_by(n.c.id)
    )
    return sql(db, query)


def test_loop_breaker_trips_on_too_many_work_items_in_an_hour_and_only_an_admin_resets_it(client, hub, hub_db):
    policy(client, hub, {"enabled": True, "max_items_per_hour": 1})
    assert "tripped" not in ticked(client, hub_db)  # one work item: the goal's brainstorm
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 2, "status": "testing", "test_card": CARD})
    push_baseline(client, hub, "CL-1")

    # the second in the hour trips it: nothing is handed out, and each admin of the project is told
    found = ticked(client, hub_db)
    assert (found["changed"], found["tripped"]) == (1, 1)
    assert [row[2:] for row in items(hub_db)] == [("queued", "breaker"), ("queued", "breaker")]
    s = tables.loop_states
    ((breaker, reason),) = sql(hub_db, select(s.c.breaker, s.c.breaker_reason))
    assert (breaker, reason) == ("tripped", "2 work items in an hour, over loop.max_items_per_hour 1")
    assert notices(hub_db) == [(OWNER, f"/p/{PROJECT}/loop", 2, False)]
    a = tables.audit
    trips = sql(hub_db, select(a.c.action, a.c.target, a.c.actor_id).where(a.c.action == "loop.breaker"))
    assert trips == [("loop.breaker", f"{PROJECT} items=2 max=1", None)]
    shown = client.get(LOOP, headers=hub["reader"])
    assert shown.status_code == 200, shown.text
    assert {key: shown.json()[key] for key in ("enabled", "breaker", "breaker_reason", "items")} == {
        "enabled": True,
        "breaker": "tripped",
        "breaker_reason": reason,
        "items": {"queued": 2},
    }

    # tripped: the events wait past the cursor, and it trips no second time
    held = state(hub_db)[1]
    edit(client, hub["chi"], {"cell": "solution", "text": "A reorder button"})
    assert ticked(client, hub_db) == {"events": 0, "changed": 0, "breaker": 1}
    assert state(hub_db)[1] == held and len(notices(hub_db)) == 1

    # a writer may not reset it; an admin does, and the hour counts from then
    refused = client.post(f"{LOOP}/resume", headers=hub["chi"])
    assert refused.status_code == 403, refused.text
    reset = client.post(f"{LOOP}/resume", headers=hub["owner"])
    assert reset.status_code == 200, reset.text
    assert (reset.json()["breaker"], reset.json()["resumed_by"]) == ("armed", OWNER)
    assert notices(hub_db) == [(OWNER, f"/p/{PROJECT}/loop", 2, True)]
    assert [row[:3] for row in audit_rows(hub_db, "loop")] == [("loop.resume", PROJECT, OWNER)]
    assert client.post(f"{LOOP}/resume", headers=hub["owner"]).status_code == 409, "it has not tripped"
    found = ticked(client, hub_db)
    assert found["events"] >= 1 and "tripped" not in found and state(hub_db)[1] > held
    assert [row[3] for row in items(hub_db)] == ["type_refused", "type_refused"], "admitted, their types off"


def test_loop_breaker_schema_0093_takes_the_loop_decision_and_its_notice_and_going_back_deletes_them(
    client, hub, hub_db
):
    policy(client, hub, {"enabled": True, "max_items_per_hour": 1})
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 2, "status": "testing", "test_card": CARD})
    push_baseline(client, hub, "CL-1")
    ticked(client, hub_db)
    assert len(notices(hub_db)) == 1
    u = tables.users
    ((owner_id,),) = sql(hub_db, select(u.c.id).where(u.c.login == OWNER))
    pid = project_id(hub_db)

    async def a_run() -> int:
        values = {"kind": "author", "project_id": pid, "title": f"Loop {SLUG}", "dispatched_by": owner_id}
        values |= {"requested_runtime": "claude-code", "runtime": "claude-code", "mode": "headless"}
        values |= {"approval": "auto", "timeout_s": 3900, "request": "Write the plan"}
        values["repos"] = [{"repo": "evo-agents-harness", "branch": None}]
        async with client.app.state.engine.begin() as conn:
            return await insert_run(conn, values)

    run_id = client.portal.call(a_run)
    d = tables.decisions
    asked = {"run_id": run_id, "project_id": pid, "category": "loop", "question": "Run it again?"}
    asked["options"] = [{"key": "retry", "label": "Run it again"}, {"key": "drop", "label": "Drop it"}]
    sql(hub_db, insert(d).values(asked))
    n = tables.notifications
    sql(hub_db, insert(n).values(user_id=owner_id, kind="notice", notice_kind="pm_escalation", title="Blocked"))

    move_to(hub_db, "0094", down=True)
    assert notices(hub_db) == [] and sql(hub_db, select(func.count()).select_from(d))[0][0] == 0
    kept = select(n.c.notice_kind).where(n.c.notice_kind.like("pm_%"))
    assert sql(hub_db, kept) == [("pm_escalation",)], "going back keeps the notices of the plugin pm (0086)"
    with pytest.raises(psycopg.errors.CheckViolation, match="decisions_category_check"):
        sql(hub_db, insert(d).values(asked))
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0095") :]
    sql(hub_db, insert(d).values(asked))
    assert sql(hub_db, select(d.c.category))[0][0] == "loop"
