"""The floor of a run's cap for an agent type (``task_runs.run_floor``), criteria a1, a2 and a3 of the plan
task-run-cost-floor: a run is handed only while what is left of the type's cap, after what its ended runs cost and what
each run not ended holds, is not under the floor; it gets that remainder as its cost cap; a next attempt of a task run
is counted by the same rule, not by the cap copied from the run before.
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from datetime import timedelta

from sqlalchemy import func, select

from evo_agents.hub import tables, task_runs
from evo_agents.hub.runs import TERMINAL_STATES
from tests.hub.live import sql
from tests.hub.test_runs import PROJECT, of_run, set_run
from tests.hub.test_task_runs import (  # noqa: F401  (fixtures)
    PLUGIN,
    app_key,
    client,
    enabled,
    hand,
    handed,
    hub,
    listed,
    plugin_key,
    put_type,
)

R = tables.runs


def task_runs_count(hub_db) -> int:
    return sql(hub_db, select(func.count()).select_from(R).where(R.c.kind == "task"))[0][0]


def project_id(hub_db) -> int:
    return sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]


def moved_to(hub_db, run_id: int, state: str, cost: float) -> None:
    """Run ``run_id`` in ``state`` having cost ``cost``, as a hub-runtime run a holder took (the checks of runs)."""
    over = state in TERMINAL_STATES
    taken = {"runtime": "hub", "requested_runtime": "hub", "leased_at": func.now()}
    if over:
        taken |= {"finished_at": func.now(), "error": "stopped" if state == "failed" else None}
    else:
        taken |= {"lease_expires_at": func.now() + timedelta(minutes=5)}
    set_run(hub_db, run_id, state=state, usage={"total_cost_usd": cost}, **taken)


def own_task(client, hub_db, task: str = "Fix it"):
    """hand_own_task of the plugin for its agent type triager, in a transaction of its own."""
    from evo_agents.hub.server import task_dispatch  # imported late: the hub modules import in a fixed order

    pid = project_id(hub_db)

    async def call():
        async with client.app.state.engine.begin() as conn:
            return await task_dispatch.hand_own_task(conn, PLUGIN, pid, "triager", task)

    return client.portal.call(call)


def again(client, run_id: int):
    from evo_agents.hub.server import task_dispatch

    async def call():
        async with client.app.state.engine.begin() as conn:
            return await task_dispatch.hand_again(conn, run_id, "its machine failed")

    return client.portal.call(call)


def test_a1_a2_the_case_of_2026_11_19_hands_nothing_with_a_cent_under_the_floor_left(client, hub, hub_db):
    enabled(client, hub["owner"], daily_usd=5)
    key = plugin_key(client, hub["plugin_id"])
    first = handed(client, key)["run_id"]  # cap 5.00 of a type that spent nothing yet
    moved_to(hub_db, first, "done", 4.99)
    assert task_runs_count(hub_db) == 1
    for _ in range(5):
        run_id, why = own_task(client, hub_db)
        assert run_id is None
        assert "cost $4.99 in the last 24 hours and runs not ended hold $0.00" in why, why
        assert "leaves $0.01 of its cap of $5.00, under the floor of $1.00" in why, why
        assert hand(client, key).status_code == 429
    assert task_runs_count(hub_db) == 1, "nothing was queued"


def test_a2_five_dispatches_hold_the_floor_each_and_the_sixth_is_refused(client, hub, hub_db):
    enabled(client, hub["owner"], daily_usd=5.01)
    key = plugin_key(client, hub["plugin_id"])
    caps = []
    for _ in range(5):
        run_id, why = own_task(client, hub_db)
        assert why is None, why
        caps.append(of_run(hub_db, run_id, "budget")[0][0]["max_usd"])
    assert caps == [5.01, 4.01, 3.01, 2.01, 1.01]
    run_id, why = own_task(client, hub_db)
    assert run_id is None and "hold $5.00, which leaves $0.01 of its cap of $5.01" in why, why
    assert hand(client, key).status_code == 429
    assert task_runs_count(hub_db) == 5


def test_a2_a_run_holds_what_it_spent_when_that_is_more_than_the_floor(client, hub, hub_db):
    enabled(client, hub["owner"], daily_usd=5)
    run_id, _ = own_task(client, hub_db)
    moved_to(hub_db, run_id, "running", 3.5)
    key = plugin_key(client, hub["plugin_id"])
    assert hand(client, key).status_code == 201  # 5 - 3.5 = 1.5, over the floor
    assert hand(client, key).status_code == 429  # 1.5 - 1 held by the run just handed: 0.5 left


def test_a2_the_floor_of_the_type_is_never_above_its_cap_and_a_cap_under_the_floor_still_hands(client, hub, hub_db):
    enabled(client, hub["owner"], daily_usd=0.5)  # the floor in effect is min($1.00, $0.50)
    run_id, why = own_task(client, hub_db)
    assert why is None and of_run(hub_db, run_id, "budget")[0][0]["max_usd"] == 0.5
    assert own_task(client, hub_db)[0] is None  # the first holds the whole cap


def test_a3_hand_again_counts_the_cap_of_the_attempt_by_the_rule_not_by_the_one_copied(client, hub, hub_db):
    enabled(client, hub["owner"], daily_usd=5)
    first, _ = own_task(client, hub_db)
    assert of_run(hub_db, first, "budget")[0][0]["max_usd"] == 5
    moved_to(hub_db, first, "lost", 2.0)
    second, why = again(client, first)
    assert why is None and second is not None
    assert of_run(hub_db, second, "budget")[0][0]["max_usd"] == 3  # 5 - 2 spent, the old cap 5 not copied
    # the attempt now holds the floor; another run costs the room under it
    moved_to(hub_db, second, "failed", 2.5)
    third, why = again(client, second)  # 5 - 2 - 2.5 = 0.5, under the floor
    assert third is None and "under the floor of $1.00" in why, why
    assert sql(hub_db, select(func.count()).select_from(R).where(R.c.parent_run_id == second)) == [(0,)]


# Setting the floor (step 3)


def test_put_without_a_floor_shows_the_hub_default_and_none_set(client, hub):
    view = enabled(client, hub["owner"], daily_usd=5)
    assert (view["min_run_usd"], view["effective_min_run_usd"]) == (None, task_runs.DEFAULT_MIN_RUN_USD)
    item = listed(client, hub["reader"])[(PLUGIN, "triager")]
    assert (item["min_run_usd"], item["effective_min_run_usd"]) == (None, 1.0)


def test_put_with_a_floor_sets_it_and_get_shows_set_and_effective(client, hub):
    view = enabled(client, hub["owner"], daily_usd=5, min_run_usd=2.5)
    assert (view["min_run_usd"], view["effective_min_run_usd"]) == (2.5, 2.5)
    item = listed(client, hub["reader"])[(PLUGIN, "triager")]
    assert (item["min_run_usd"], item["effective_min_run_usd"]) == (2.5, 2.5)


def test_the_default_floor_never_exceeds_the_daily_cap(client, hub):
    view = enabled(client, hub["owner"], daily_usd=0.5)
    assert (view["min_run_usd"], view["effective_min_run_usd"]) == (None, 0.5)


@pytest.mark.parametrize("floor", [0, -1, 5.01])
def test_a_floor_not_above_zero_or_above_the_daily_cap_is_422_and_changes_nothing(client, hub, floor):
    enabled(client, hub["owner"], daily_usd=5, min_run_usd=2)
    assert put_type(client, hub["owner"], daily_usd=5, min_run_usd=floor).status_code == 422
    item = listed(client, hub["reader"])[(PLUGIN, "triager")]
    assert (item["daily_usd"], item["min_run_usd"]) == (5, 2)


def test_changing_the_daily_cap_without_a_floor_keeps_the_floor_set(client, hub):
    enabled(client, hub["owner"], daily_usd=5, min_run_usd=2)
    view = enabled(client, hub["owner"], daily_usd=10)
    assert (view["daily_usd"], view["min_run_usd"], view["effective_min_run_usd"]) == (10, 2, 2)
    view = enabled(client, hub["owner"], daily_usd=1.5)
    assert (view["min_run_usd"], view["effective_min_run_usd"]) == (2, 1.5)


def test_a_new_floor_replaces_the_one_set(client, hub):
    enabled(client, hub["owner"], daily_usd=5, min_run_usd=2)
    view = enabled(client, hub["owner"], daily_usd=5, min_run_usd=3)
    assert (view["min_run_usd"], view["effective_min_run_usd"]) == (3, 3)
